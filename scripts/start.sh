#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
IMAGE=${IMAGE:-vllm-ds4-sm120:0.25.1-fi0.6.14}
CONTAINER_NAME=${CONTAINER_NAME:-deepseek-v4-flash-vllm-prod}
PORT=${PORT:-30000}
HOST_HF_CACHE=${HOST_HF_CACHE:-/home/zay/.cache/huggingface}
HOST_CACHE_ROOT=${HOST_CACHE_ROOT:-/home/zay/.cache/vllm-docker}
MODEL_REVISION=${MODEL_REVISION:-7872f01b1d1fe23eabc4c98b48bffcef5a386062}
MODEL_PATH="/root/.cache/huggingface/hub/models--deepseek-ai--DeepSeek-V4-Flash-0731/snapshots/${MODEL_REVISION}"
PATCH_FILE=${PATCH_FILE:-${REPO_ROOT}/patches/sparse_swa_sm120.py}
READY_TIMEOUT_SECONDS=${READY_TIMEOUT_SECONDS:-1800}

if [[ ! -f "${PATCH_FILE}" ]]; then
  echo "missing compatibility patch: ${PATCH_FILE}" >&2
  exit 2
fi
if docker inspect "${CONTAINER_NAME}" >/dev/null 2>&1; then
  echo "container already exists: ${CONTAINER_NAME}" >&2
  exit 3
fi
if ! docker image inspect "${IMAGE}" >/dev/null 2>&1; then
  docker build -t "${IMAGE}" "${REPO_ROOT}"
fi

mkdir -p \
  "${HOST_CACHE_ROOT}/vllm" \
  "${HOST_CACHE_ROOT}/tilelang" \
  "${HOST_CACHE_ROOT}/deep_gemm" \
  "${HOST_CACHE_ROOT}/flashinfer" \
  "${HOST_CACHE_ROOT}/dot-tilelang" \
  "${HOST_CACHE_ROOT}/dot-deep_gemm" \
  "${HOST_CACHE_ROOT}/dot-triton"

docker run -d \
  --name "${CONTAINER_NAME}" \
  --restart unless-stopped \
  --gpus all \
  --ipc=host \
  --network=host \
  --shm-size=64g \
  --security-opt label=disable \
  --ulimit memlock=-1 \
  --ulimit stack=67108864 \
  -e NCCL_P2P_DISABLE=1 \
  -e NCCL_CUMEM_ENABLE=0 \
  -e FLASHINFER_DISABLE_VERSION_CHECK=1 \
  -e PYTHONDONTWRITEBYTECODE=1 \
  -v "${PATCH_FILE}:/usr/local/lib/python3.12/dist-packages/vllm/v1/attention/backends/mla/sparse_swa.py:ro" \
  -v "${HOST_HF_CACHE}:/root/.cache/huggingface:rw" \
  -v "${HOST_CACHE_ROOT}/vllm:/root/.cache/vllm:rw" \
  -v "${HOST_CACHE_ROOT}/tilelang:/root/.cache/tilelang:rw" \
  -v "${HOST_CACHE_ROOT}/deep_gemm:/root/.cache/deep_gemm:rw" \
  -v "${HOST_CACHE_ROOT}/flashinfer:/root/.cache/flashinfer:rw" \
  -v "${HOST_CACHE_ROOT}/dot-tilelang:/root/.tilelang:rw" \
  -v "${HOST_CACHE_ROOT}/dot-deep_gemm:/root/.deep_gemm:rw" \
  -v "${HOST_CACHE_ROOT}/dot-triton:/root/.triton:rw" \
  --entrypoint /usr/local/bin/vllm \
  "${IMAGE}" \
  serve "${MODEL_PATH}" \
  --trust-remote-code \
  --served-model-name deepseek-v4-flash-0731 \
  --tensor-parallel-size 2 \
  --gpu-memory-utilization 0.95 \
  --max-model-len 12288 \
  --max-num-seqs 24 \
  --max-num-batched-tokens 16384 \
  --kv-cache-dtype fp8 \
  --kernel-config '{"moe_backend":"marlin","enable_flashinfer_autotune":false}' \
  --tokenizer-mode deepseek_v4 \
  --enable-auto-tool-choice \
  --reasoning-parser deepseek_v4 \
  --tool-call-parser deepseek_v4 \
  --host 0.0.0.0 \
  --port "${PORT}" \
  --disable-custom-all-reduce \
  --speculative-config '{"method":"dspark","num_speculative_tokens":5}'

deadline=$((SECONDS + READY_TIMEOUT_SECONDS))
while (( SECONDS < deadline )); do
  if curl --fail --silent --max-time 5 "http://127.0.0.1:${PORT}/health" >/dev/null; then
    models=$(curl --fail --silent --max-time 10 "http://127.0.0.1:${PORT}/v1/models")
    if [[ "${models}" == *'"id":"deepseek-v4-flash-0731"'* ]]; then
      echo "${CONTAINER_NAME} is healthy on port ${PORT}"
      exit 0
    fi
  fi
  if [[ $(docker inspect -f '{{.State.Running}}' "${CONTAINER_NAME}" 2>/dev/null || true) != true ]]; then
    docker logs --tail 160 "${CONTAINER_NAME}" >&2 || true
    echo "${CONTAINER_NAME} exited during startup" >&2
    exit 4
  fi
  sleep 5
done

docker logs --tail 160 "${CONTAINER_NAME}" >&2 || true
echo "${CONTAINER_NAME} did not become healthy within ${READY_TIMEOUT_SECONDS}s" >&2
exit 5
