#!/usr/bin/env python3
"""Small authenticated streaming proxy for the local vLLM endpoint."""

from __future__ import annotations

import http.client
import json
import os
import re
import signal
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

try:
    from gateway.keys import KeyRegistry
except ModuleNotFoundError:  # Installed gateway.py and keys.py live side by side.
    from keys import KeyRegistry


HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


@dataclass(frozen=True)
class GatewayConfig:
    listen_host: str
    listen_port: int
    upstream_url: str
    key_registry: KeyRegistry
    max_body_bytes: int = 32 * 1024 * 1024
    upstream_timeout_seconds: int = 900


def config_from_environment() -> GatewayConfig:
    registry_path = Path(
        os.environ.get(
            "DS4_GATEWAY_API_KEYS_FILE",
            "~/.config/ds4-gateway/keys.json",
        )
    ).expanduser()
    return GatewayConfig(
        listen_host=os.environ.get("DS4_GATEWAY_LISTEN_HOST", "127.0.0.1"),
        listen_port=int(os.environ.get("DS4_GATEWAY_PORT", "4000")),
        upstream_url=os.environ.get(
            "DS4_GATEWAY_UPSTREAM", "http://127.0.0.1:30000"
        ).rstrip("/"),
        key_registry=KeyRegistry(registry_path),
        max_body_bytes=int(
            os.environ.get("DS4_GATEWAY_MAX_BODY_BYTES", str(32 * 1024 * 1024))
        ),
        upstream_timeout_seconds=int(
            os.environ.get("DS4_GATEWAY_UPSTREAM_TIMEOUT_SECONDS", "900")
        ),
    )


class GatewayServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, config: GatewayConfig):
        self.config = config
        super().__init__((config.listen_host, config.listen_port), GatewayHandler)


class GatewayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ds4-gateway/1"

    @property
    def gateway(self) -> GatewayServer:
        return self.server  # type: ignore[return-value]

    def do_GET(self) -> None:
        self._handle_request()

    def do_POST(self) -> None:
        self._handle_request()

    def do_PUT(self) -> None:
        self._handle_request()

    def do_PATCH(self) -> None:
        self._handle_request()

    def do_DELETE(self) -> None:
        self._handle_request()

    def do_HEAD(self) -> None:
        self._handle_request()

    def _handle_request(self) -> None:
        started = time.monotonic()
        status = 500
        principal = "-"
        supplied_request_id = self.headers.get("X-Request-ID", "")
        request_id = (
            supplied_request_id
            if REQUEST_ID_PATTERN.fullmatch(supplied_request_id)
            else uuid.uuid4().hex
        )
        try:
            path = urlsplit(self.path).path
            if path == "/healthz":
                status = 200
                self._send_json(status, {"status": "ok"}, request_id)
                return
            if path == "/readyz":
                status = self._ready_status()
                self._send_json(
                    status,
                    {"status": "ready" if status == 200 else "upstream_unavailable"},
                    request_id,
                )
                return
            if path != "/v1" and not path.startswith("/v1/"):
                status = 404
                self._send_json(status, {"error": "not_found"}, request_id)
                return
            try:
                principal = (
                    self.gateway.config.key_registry.authenticate(
                        self.headers.get("Authorization", "")
                    )
                    or "-"
                )
            except (OSError, ValueError, json.JSONDecodeError) as error:
                print(
                    f"credential_store_error request_id={request_id} "
                    f"type={type(error).__name__}",
                    file=sys.stderr,
                    flush=True,
                )
                status = 503
                self._send_json(
                    status, {"error": "credential_store_unavailable"}, request_id
                )
                return
            if principal == "-":
                status = 401
                self._send_json(
                    status,
                    {"error": "unauthorized"},
                    request_id,
                    {"WWW-Authenticate": "Bearer"},
                )
                return
            status = self._proxy(request_id, principal)
        except BodyTooLarge:
            status = 413
            self._send_json(status, {"error": "request_too_large"}, request_id)
        except (BrokenPipeError, ConnectionResetError):
            status = 499
        except Exception as error:  # noqa: BLE001 - fail closed at the gateway boundary
            print(
                f"gateway_error request_id={request_id} type={type(error).__name__}",
                file=sys.stderr,
                flush=True,
            )
            status = 502
            if not self.wfile.closed:
                self._send_json(status, {"error": "upstream_unavailable"}, request_id)
        finally:
            elapsed_ms = round((time.monotonic() - started) * 1000)
            path = urlsplit(self.path).path
            print(
                f"request request_id={request_id} method={self.command} "
                f"path={path} principal={principal} status={status} "
                f"duration_ms={elapsed_ms}",
                flush=True,
            )

    def _ready_status(self) -> int:
        upstream = urlsplit(self.gateway.config.upstream_url)
        connection = http.client.HTTPConnection(
            upstream.hostname,
            upstream.port or 80,
            timeout=5,
        )
        try:
            connection.request("GET", f"{upstream.path.rstrip('/')}/health")
            response = connection.getresponse()
            response.read()
            return 200 if 200 <= response.status < 300 else 503
        except OSError:
            return 503
        finally:
            connection.close()

    def _proxy(self, request_id: str, principal: str) -> int:
        body = self._read_body()
        upstream = urlsplit(self.gateway.config.upstream_url)
        upstream_path = f"{upstream.path.rstrip('/')}{self.path}"
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in HOP_BY_HOP_HEADERS
            and key.lower()
            not in {
                "authorization",
                "host",
                "content-length",
                "x-ds4-principal",
            }
        }
        headers["Host"] = upstream.netloc
        headers["X-Request-ID"] = request_id
        headers["X-DS4-Principal"] = principal
        headers["X-Forwarded-Proto"] = "https"
        if body:
            headers["Content-Length"] = str(len(body))

        connection = http.client.HTTPConnection(
            upstream.hostname,
            upstream.port or 80,
            timeout=self.gateway.config.upstream_timeout_seconds,
        )
        try:
            connection.request(
                self.command, upstream_path, body=body or None, headers=headers
            )
            response = connection.getresponse()
            response_headers = [
                (key, value)
                for key, value in response.getheaders()
                if key.lower() not in HOP_BY_HOP_HEADERS
            ]
            content_length = next(
                (
                    value
                    for key, value in response_headers
                    if key.lower() == "content-length"
                ),
                None,
            )
            has_body = self.command != "HEAD" and response.status not in {204, 304}

            self.send_response(response.status)
            for key, value in response_headers:
                self.send_header(key, value)
            self.send_header("X-Request-ID", request_id)
            if has_body and content_length is None:
                self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()

            if not has_body:
                response.read()
                return response.status

            while True:
                chunk = response.read1(64 * 1024)
                if not chunk:
                    break
                if content_length is None:
                    self.wfile.write(f"{len(chunk):X}\r\n".encode("ascii"))
                    self.wfile.write(chunk)
                    self.wfile.write(b"\r\n")
                else:
                    self.wfile.write(chunk)
                self.wfile.flush()
            if content_length is None:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            return response.status
        finally:
            connection.close()

    def _read_body(self) -> bytes:
        content_length = self.headers.get("Content-Length")
        if content_length is not None:
            length = int(content_length)
            if length > self.gateway.config.max_body_bytes:
                raise BodyTooLarge
            return self.rfile.read(length)
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            return self._read_chunked_body()
        return b""

    def _read_chunked_body(self) -> bytes:
        body = bytearray()
        while True:
            line = self.rfile.readline(128)
            size = int(line.split(b";", 1)[0].strip(), 16)
            if size == 0:
                while self.rfile.readline(8192) not in {b"\r\n", b"\n", b""}:
                    pass
                break
            if len(body) + size > self.gateway.config.max_body_bytes:
                raise BodyTooLarge
            body.extend(self.rfile.read(size))
            if self.rfile.read(2) != b"\r\n":
                raise ValueError("invalid chunk framing")
        return bytes(body)

    def _send_json(
        self,
        status: int,
        payload: dict[str, str],
        request_id: str,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Request-ID", request_id)
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def log_message(self, _format: str, *args: object) -> None:
        return


class BodyTooLarge(Exception):
    pass


def main() -> int:
    config = config_from_environment()
    server = GatewayServer(config)

    def stop_server(_signum: int, _frame: object) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop_server)
    signal.signal(signal.SIGINT, stop_server)
    print(
        f"gateway_listening address={config.listen_host}:{config.listen_port} "
        f"upstream={config.upstream_url}",
        flush=True,
    )
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
