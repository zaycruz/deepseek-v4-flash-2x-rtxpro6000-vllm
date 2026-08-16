#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
CURRENT_CONTAINER=${CURRENT_CONTAINER:-sglang-ds4-flash-0731-dspark-sm120}
NEW_CONTAINER=${NEW_CONTAINER:-deepseek-v4-flash-vllm-prod}
PORT=${PORT:-30000}
ROLLBACK_CONTAINER=${ROLLBACK_CONTAINER:-${CURRENT_CONTAINER}-rollback-$(date -u +%Y%m%dT%H%M%SZ)}

restore_previous() {
  echo "vLLM verification failed; restoring ${ROLLBACK_CONTAINER}" >&2
  docker stop "${NEW_CONTAINER}" >/dev/null 2>&1 || true
  docker rename "${NEW_CONTAINER}" "${NEW_CONTAINER}-failed-$(date -u +%Y%m%dT%H%M%SZ)" >/dev/null 2>&1 || true
  docker rename "${ROLLBACK_CONTAINER}" "${CURRENT_CONTAINER}"
  docker start "${CURRENT_CONTAINER}" >/dev/null
  for _ in $(seq 1 480); do
    if curl --fail --silent --max-time 5 "http://127.0.0.1:${PORT}/health" >/dev/null; then
      echo "restored ${CURRENT_CONTAINER}" >&2
      return 0
    fi
    sleep 5
  done
  echo "rollback container did not become healthy" >&2
  return 1
}

state=$(docker inspect -f '{{.State.Running}}' "${CURRENT_CONTAINER}" 2>/dev/null || true)
if [[ "${state}" != true ]]; then
  echo "expected running current container: ${CURRENT_CONTAINER}" >&2
  exit 2
fi
if docker inspect "${NEW_CONTAINER}" >/dev/null 2>&1; then
  echo "new container name already exists: ${NEW_CONTAINER}" >&2
  exit 3
fi
if docker inspect "${ROLLBACK_CONTAINER}" >/dev/null 2>&1; then
  echo "rollback container name already exists: ${ROLLBACK_CONTAINER}" >&2
  exit 4
fi
if ! curl --fail --silent --max-time 10 "http://127.0.0.1:${PORT}/v1/models" >/dev/null; then
  echo "current endpoint is not healthy" >&2
  exit 5
fi
if ! nvidia-smi --query-gpu=power.limit --format=csv,noheader,nounits |
  awk 'BEGIN {count=0; ok=1} {count++; if (($1 + 0) < 450) ok=0} END {exit !(count == 2 && ok)}'; then
  echo "expected exactly two GPUs with power limits of at least 450 W" >&2
  exit 6
fi

docker stop "${CURRENT_CONTAINER}" >/dev/null
docker rename "${CURRENT_CONTAINER}" "${ROLLBACK_CONTAINER}"

if ! CONTAINER_NAME="${NEW_CONTAINER}" PORT="${PORT}" "${REPO_ROOT}/scripts/start.sh"; then
  restore_previous
  exit 7
fi

models=$(curl --fail --silent --max-time 10 "http://127.0.0.1:${PORT}/v1/models" || true)
if [[ "${models}" != *'"id":"deepseek-v4-flash-0731"'* ]] ||
   [[ "${models}" != *'"max_model_len":12288'* ]]; then
  restore_previous
  exit 8
fi

echo "production: ${NEW_CONTAINER}"
echo "rollback: ${ROLLBACK_CONTAINER}"
echo "endpoint: http://127.0.0.1:${PORT}"
