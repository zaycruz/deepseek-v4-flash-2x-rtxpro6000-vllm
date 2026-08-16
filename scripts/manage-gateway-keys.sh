#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
REGISTRY=${DS4_GATEWAY_API_KEYS_FILE:-${HOME}/.config/ds4-gateway/keys.json}

exec python3 "${REPO_ROOT}/gateway/keys.py" --registry "${REGISTRY}" "$@"
