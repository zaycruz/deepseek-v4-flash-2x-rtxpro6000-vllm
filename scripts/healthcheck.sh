#!/usr/bin/env bash
set -euo pipefail

PORT=${PORT:-30000}
CONTAINER_NAME=${CONTAINER_NAME:-deepseek-v4-flash-vllm-prod}

docker inspect "${CONTAINER_NAME}" --format '{{.Id}} {{.Config.Image}} {{.State.Status}} {{.HostConfig.RestartPolicy.Name}}'
curl --fail --silent "http://127.0.0.1:${PORT}/v1/models"
echo
nvidia-smi --query-gpu=index,name,temperature.gpu,power.draw,power.limit,memory.used,memory.total --format=csv,noheader,nounits
