# DeepSeek-V4-Flash-0731 on 2x RTX PRO 6000 with vLLM + DSpark

A reproducible vLLM recipe for serving `deepseek-ai/DeepSeek-V4-Flash-0731`
on two 96 GB RTX PRO 6000 Blackwell Workstation GPUs (SM120). The measured
configuration uses tensor parallelism, FP8 KV cache, the Marlin MoE backend,
and the checkpoint's DSpark draft model.

## Measured result

These are real OpenAI-compatible streaming requests, not engine log rates.
Each request used an agentic coding/operations prompt of about 1,023 input
tokens, 512 output tokens, temperature 0, and thinking enabled.

| Load | Requests | P50 decode tok/s per stream | P05 decode tok/s | Aggregate output tok/s | P50 TTFT | Success |
|---|---:|---:|---:|---:|---:|---:|
| C1 | 4 sequential | 255.41 | 242.07 | 202.85 | 138 ms | 100% |
| C8 | 8 simultaneous | 85.82 | 68.01 | 532.11 | 233 ms | 100% |
| C16 | 16 simultaneous | 86.68 | 68.76 | 1,005.22 | 2,031 ms | 100% |
| C24 | 24 simultaneous | 71.02 | 66.90 | 1,366.08 | 1,341 ms | 100% |

A second full replicate measured 255.93 tok/s at C1 and 72.40 tok/s per
stream at C24, with 1,435.16 aggregate output tok/s. Across both C24
replicates, the slowest individual stream was 65.23 tok/s. Peak temperatures
were 69 C and 62 C.

Decode tok/s is `(completion_tokens - 1) / (last_token_time - first_token_time)`.
Aggregate output tok/s is total completion tokens divided by phase wall time.
The complete per-request traces and one-second GPU telemetry are committed in
[`bench/metrics.json`](bench/metrics.json) and
[`bench/gpu-samples.csv`](bench/gpu-samples.csv).

## Exact configuration

- Hardware: 2x NVIDIA RTX PRO 6000 Blackwell Workstation Edition, 97,887 MiB
  each, PCIe, 450 W power limit per card
- Model: `deepseek-ai/DeepSeek-V4-Flash-0731`
- Revision: `7872f01b1d1fe23eabc4c98b48bffcef5a386062`
- vLLM: `v0.25.1`
- FlashInfer: `0.6.14`
- Tensor parallel size: 2
- KV cache: FP8
- MoE backend: Marlin
- DSpark speculative decoding: 5 tokens
- Maximum sequences: 24
- Maximum batched tokens: 16,384
- Maximum model length: 12,288 tokens
- Custom all-reduce: disabled for this PCIe topology

The 12,288-token model-length cap is intentional: it is the capacity/performance
point measured here. This recipe is not evidence for 512k or 1M context at the
same concurrency or speed.

## Build and run

The model weights must already be present in the host Hugging Face cache. The
default paths in the scripts match the benchmark host and can be overridden
with environment variables.

```bash
docker build -t vllm-ds4-sm120:0.25.1-fi0.6.14 .
./scripts/start.sh
curl -fsS http://127.0.0.1:30000/v1/models
```

Important overrides:

```bash
HOST_HF_CACHE=/path/to/huggingface/cache \
PORT=30000 \
CONTAINER_NAME=deepseek-v4-flash-vllm-prod \
./scripts/start.sh
```

The launch script also mounts persistent vLLM, TileLang, DeepGEMM, FlashInfer,
and Triton caches to avoid repeating kernel compilation after every container
replacement.

## Production cutover

`scripts/deploy.sh` performs a rollback-safe replacement:

1. Verify the current container, endpoint, two GPUs, and 450 W power limits.
2. Stop and rename the current container; it is not deleted.
3. Start the vLLM container and wait for health plus model identity.
4. Restore the old container automatically if startup or verification fails.

On success it prints the exact rollback container name. To roll back manually:

```bash
docker stop deepseek-v4-flash-vllm-prod
docker start <rollback-container-name>
```

If the previous container was renamed from the canonical name, rename it back
before starting it.

## Re-run the workload

The benchmark client is read-only with respect to containers. It only calls the
OpenAI-compatible endpoint and samples local GPUs.

```bash
python3 -m pip install -r bench/requirements.txt
python3 bench/benchmark.py \
  --endpoint http://127.0.0.1:30000 \
  --model deepseek-v4-flash-0731 \
  --output bench/latest.json
```

By default it runs C1, C8, C16, and C24 and aborts requests if a locally visible
GPU reaches 85 C. Use `--thermal-limit 0` to disable that benchmark-only guard.
The production service itself does not install or enable a thermal trigger.

## SM120 compatibility patch

[`patches/sparse_swa_sm120.py`](patches/sparse_swa_sm120.py) is the vLLM
`sparse_swa.py` from the benchmarked image with a narrow FlashInfer 0.6.14
compatibility backport: DSpark's raw 256-wide non-causal index is padded to 512
because that FlashInfer SM120 decode build instantiates 128, 512, and 1,024,
but not 256.

This compatibility idea is not claimed as novel. A similar public workaround
was already available in
[MykhailoTamarin/vllm-starter](https://github.com/MykhailoTamarin/vllm-starter/blob/9b7a244bafe26882819201ff619729c69ebe74a8/images/vllm-0_26_0-exl3/patch_sparse_swa_sm12x.py),
and a more complete fix landed upstream in
[vLLM PR #51538](https://github.com/vllm-project/vllm/pull/51538).
Adaptive DSpark support landed separately in
[vLLM PR #47808](https://github.com/vllm-project/vllm/pull/47808).

Treat this file as a pinned backport for the exact vLLM 0.25.1 / FlashInfer
0.6.14 image. Prefer current upstream once your SM120 build includes the fixes.

## Artifact integrity

```text
c9f924da718e75ad6ed48ea1a9196cc36e9ebc6920073fd01612d30a9840f34c  config/candidate.json
c8366ab92cc89e100711575329addffa2aa517cf2336d57590ab7dc4cee0b1c0  bench/metrics.json
7db8ff14b1583e994019be1625096621ac4edfd1946b8955fea550640e63e3c1  bench/gpu-samples.csv
```

The winning experiment commit was
`b051bd71d356a75c9002af6b25abdf4da2712f68`.

## License

Apache-2.0. The compatibility file derives from vLLM and retains its original
SPDX header and license.
