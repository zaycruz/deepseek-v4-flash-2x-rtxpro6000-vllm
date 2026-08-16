#!/usr/bin/env python3
"""Read-only streaming benchmark for an OpenAI-compatible endpoint."""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import aiohttp


PROMPT_TEMPLATES = (
    "Debug this failing agent task. Identify the likely root cause, propose the smallest safe fix, and give exact verification commands.",
    "Implement this coding change as an agent. State the files to inspect, the boundary to preserve, and the focused tests to run.",
    "Review this patch as a senior engineer. Rank correctness, reliability, and scope risks and identify the evidence needed for each.",
    "Coordinate this operations workflow. Produce a concise diagnosis, execution, rollback, and verification plan.",
)


def prompt_context() -> str:
    block = (
        "Repository context: this service is part of a multi-agent coding and operations "
        "system. A worker receives an issue, inspects a TypeScript or Python service, "
        "changes one narrow module, runs focused tests, and reports evidence. The codebase "
        "uses explicit boundaries, cancellation-safe retries, structured JSON logs, and "
        "errors that distinguish product defects from environment blockers. Existing "
        "behavior must remain stable outside the requested slice. "
    )
    return (block * 12).strip()


def make_prompt(index: int) -> str:
    return (
        f"Agent request {index}: {PROMPT_TEMPLATES[index % len(PROMPT_TEMPLATES)]}\n\n"
        f"{prompt_context()}\n\n"
        "Issue-specific context: inspect the retry path, preserve cancellation semantics, "
        "avoid a framework rewrite, include a small patch sketch, and finish with tests."
    )


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = (len(ordered) - 1) * p / 100.0
    low, high = math.floor(rank), math.ceil(rank)
    if low == high:
        return ordered[low]
    return ordered[low] * (high - rank) + ordered[high] * (rank - low)


async def request_one(
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    endpoint: str,
    model: str,
    index: int,
    max_tokens: int,
    temperature: float,
    benchmark_start: float,
) -> dict[str, Any]:
    body = {
        "model": model,
        "messages": [{"role": "user", "content": make_prompt(index)}],
        "temperature": temperature,
        "seed": 70_000 + index,
        "max_tokens": max_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},
        "chat_template_kwargs": {"enable_thinking": True},
    }
    async with semaphore:
        started = time.perf_counter()
        first_token: float | None = None
        usage: dict[str, Any] = {}
        try:
            async with session.post(
                f"{endpoint.rstrip('/')}/v1/chat/completions", json=body
            ) as response:
                if response.status != 200:
                    return {
                        "success": False,
                        "index": index,
                        "status": response.status,
                        "error": (await response.text())[:1000],
                    }
                while True:
                    raw = await response.content.readline()
                    if not raw:
                        break
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if not payload or payload == "[DONE]":
                        continue
                    event = json.loads(payload)
                    if event.get("usage"):
                        usage = event["usage"]
                    choices = event.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    if any(
                        delta.get(key) not in (None, "", [], {})
                        for key in ("content", "reasoning", "reasoning_content", "tool_calls")
                    ):
                        first_token = first_token or time.perf_counter()
        except Exception as exc:
            return {
                "success": False,
                "index": index,
                "status": None,
                "error": f"{type(exc).__name__}: {exc}",
            }

        ended = time.perf_counter()
        completion_tokens = int(usage.get("completion_tokens") or 0)
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        if first_token is None or completion_tokens <= 1:
            return {
                "success": False,
                "index": index,
                "status": 200,
                "error": "stream completed without authoritative token usage",
            }
        decode_seconds = max(ended - first_token, 1e-9)
        emitted_after_first = completion_tokens - 1
        return {
            "success": True,
            "index": index,
            "started_offset_seconds": started - benchmark_start,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "ttft_ms": (first_token - started) * 1000,
            "e2e_ms": (ended - started) * 1000,
            "tpot_ms": decode_seconds * 1000 / emitted_after_first,
            "tok_s": emitted_after_first / decode_seconds,
        }


async def run_phase(
    endpoint: str,
    model: str,
    concurrency: int,
    requests: int,
    max_tokens: int,
    temperature: float,
    abort: threading.Event,
) -> dict[str, Any]:
    timeout = aiohttp.ClientTimeout(total=1800)
    connector = aiohttp.TCPConnector(limit=max(concurrency * 2, 32))
    semaphore = asyncio.Semaphore(concurrency)
    started = time.perf_counter()
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        tasks = [
            asyncio.create_task(
                request_one(
                    session, semaphore, endpoint, model, index, max_tokens,
                    temperature, started
                )
            )
            for index in range(requests)
        ]

        async def cancel_on_abort() -> None:
            while not abort.is_set():
                await asyncio.sleep(0.1)
            for task in tasks:
                if not task.done():
                    task.cancel()

        monitor = asyncio.create_task(cancel_on_abort())
        try:
            raw = await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            monitor.cancel()
            try:
                await monitor
            except asyncio.CancelledError:
                pass

    wall = time.perf_counter() - started
    results: list[dict[str, Any]] = []
    for index, result in enumerate(raw):
        if isinstance(result, BaseException):
            results.append({
                "success": False,
                "index": index,
                "error": "cancelled by thermal guard" if abort.is_set()
                else f"{type(result).__name__}: {result}",
            })
        else:
            results.append(result)
    successful = [row for row in results if row.get("success")]
    rates = [float(row["tok_s"]) for row in successful]
    completion = sum(int(row["completion_tokens"]) for row in successful)
    return {
        "concurrency": concurrency,
        "requests": requests,
        "successful_requests": len(successful),
        "failed_requests": requests - len(successful),
        "success_rate": len(successful) / requests,
        "wall_seconds": wall,
        "prompt_tokens": sum(int(row["prompt_tokens"]) for row in successful),
        "completion_tokens": completion,
        "aggregate_output_tok_s": completion / wall if wall else 0.0,
        "p50_per_stream_tok_s": percentile(rates, 50),
        "p05_per_stream_tok_s": percentile(rates, 5),
        "p50_ttft_ms": percentile(
            [float(row["ttft_ms"]) for row in successful], 50
        ),
        "p95_ttft_ms": percentile(
            [float(row["ttft_ms"]) for row in successful], 95
        ),
        "requests_detail": results,
    }


def sample_gpus(path: Path, stop: threading.Event, abort: threading.Event, limit: float) -> None:
    if not shutil.which("nvidia-smi"):
        return
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["timestamp", "index", "temperature_c", "power_w", "utilization_pct"])
        while not stop.is_set():
            result = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=timestamp,index,temperature.gpu,power.draw,utilization.gpu",
                    "--format=csv,noheader,nounits",
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            for line in result.stdout.splitlines():
                fields = [field.strip() for field in line.split(",")]
                if len(fields) != 5:
                    continue
                writer.writerow(fields)
                if limit > 0 and float(fields[2]) >= limit:
                    abort.set()
            handle.flush()
            stop.wait(1)


def parse_phases(value: str) -> list[tuple[str, int, int]]:
    phases = []
    for item in value.split(","):
        label, requests = item.split(":", 1)
        concurrency = int(label.removeprefix("c"))
        phases.append((label, concurrency, int(requests)))
    return phases


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", default="http://127.0.0.1:30000")
    parser.add_argument("--model", default="deepseek-v4-flash-0731")
    parser.add_argument("--output", type=Path, default=Path("bench/latest.json"))
    parser.add_argument("--phases", default="c1:4,c8:8,c16:16,c24:24")
    parser.add_argument("--max-tokens", type=int, default=512)
    parser.add_argument("--temperature", type=float, default=0)
    parser.add_argument("--thermal-limit", type=float, default=85)
    args = parser.parse_args()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    telemetry = args.output.with_suffix(".gpu.csv")
    stop = threading.Event()
    abort = threading.Event()
    thread = threading.Thread(
        target=sample_gpus,
        args=(telemetry, stop, abort, args.thermal_limit),
        daemon=True,
    )
    thread.start()
    result: dict[str, Any] = {
        "endpoint": args.endpoint,
        "model": args.model,
        "max_output_tokens": args.max_tokens,
        "temperature": args.temperature,
        "thermal_limit_c": args.thermal_limit,
        "phases": {},
    }
    try:
        for label, concurrency, requests in parse_phases(args.phases):
            phase = await run_phase(
                args.endpoint, args.model, concurrency, requests,
                args.max_tokens, args.temperature, abort
            )
            result["phases"][label] = phase
            print(json.dumps({label: phase}, indent=2))
            if abort.is_set():
                result["thermal_abort"] = True
                break
    finally:
        stop.set()
        thread.join(timeout=5)
        args.output.write_text(json.dumps(result, indent=2) + "\n")
    return 2 if abort.is_set() else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
