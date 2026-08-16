#!/usr/bin/env bash
set -euo pipefail

PUBLIC_PATH=${PUBLIC_PATH:-/v1}
HTTPS_PORT=${HTTPS_PORT:-443}
TARGET=${TARGET:-http://127.0.0.1:4000/v1}
KEY_FILE=${DS4_GATEWAY_API_KEY_FILE:-${HOME}/.config/ds4-gateway/api-key}
STATE_ROOT=${STATE_ROOT:-${HOME}/.local/state/ds4-gateway}
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP=${STATE_ROOT}/tailscale-serve-${STAMP}.json
STATUS_FILE=$(mktemp)
trap 'rm -f "${STATUS_FILE}"' EXIT

for command in tailscale python3 curl; do
  command -v "${command}" >/dev/null || {
    echo "missing required command: ${command}" >&2
    exit 2
  }
done

curl --fail --silent --max-time 5 http://127.0.0.1:4000/readyz >/dev/null || {
  echo "gateway is not ready on port 4000" >&2
  exit 3
}
[[ -f "${KEY_FILE}" ]] || {
  echo "missing protected API key file: ${KEY_FILE}" >&2
  exit 4
}

install -d -m 700 "${STATE_ROOT}"
tailscale serve get-config "${BACKUP}" --all

# Fail closed: remove any public Funnel route before creating private Serve.
tailscale funnel reset
tailscale serve --bg --yes --https="${HTTPS_PORT}" --set-path="${PUBLIC_PATH}" "${TARGET}"
tailscale serve status --json >"${STATUS_FILE}"

python3 - "${STATUS_FILE}" "${TARGET}" "${PUBLIC_PATH}" <<'PY'
import json
import sys

path, target, public_path = sys.argv[1:]
data = json.load(open(path, encoding="utf-8"))
if any(data.get("AllowFunnel", {}).values()):
    raise SystemExit("Funnel is still enabled; refusing to report success")
handlers = [
    handler
    for site in data.get("Web", {}).values()
    for route, handler in site.get("Handlers", {}).items()
    if route == public_path
]
if not any(handler.get("Proxy") == target for handler in handlers):
    raise SystemExit(f"private Serve route does not point to {target}")
PY

python3 - "${KEY_FILE}" "${PUBLIC_PATH}" <<'PY'
import json
import ssl
import sys
import urllib.request

key_path, public_path = sys.argv[1:]
status = json.loads(__import__("subprocess").check_output(["tailscale", "status", "--json"]))
host = status["Self"]["DNSName"].rstrip(".")
key = open(key_path, encoding="utf-8").read().strip()
request = urllib.request.Request(
    f"https://{host}{public_path}/models",
    headers={"Authorization": f"Bearer {key}"},
)
with urllib.request.urlopen(request, timeout=10, context=ssl.create_default_context()) as response:
    payload = json.load(response)
if not any(model.get("id") == "deepseek-v4-flash-0731" for model in payload.get("data", [])):
    raise SystemExit("gateway did not return the production model")
print(f"private gateway ready: https://{host}{public_path}")
PY

echo "previous Tailscale configuration: ${BACKUP}"
echo "rollback: tailscale serve set-config ${BACKUP} --all"
