#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
INSTALL_ROOT=${INSTALL_ROOT:-${HOME}/.local/share/ds4-gateway}
CONFIG_ROOT=${CONFIG_ROOT:-${HOME}/.config/ds4-gateway}
UNIT_ROOT=${UNIT_ROOT:-${HOME}/.config/systemd/user}
KEY_FILE=${DS4_GATEWAY_API_KEY_FILE:-${CONFIG_ROOT}/api-key}
PROVISION_KEY_FILE=${PROVISION_KEY_FILE:-}

for command in python3 systemctl curl loginctl; do
  command -v "${command}" >/dev/null || {
    echo "missing required command: ${command}" >&2
    exit 2
  }
done

install -d -m 700 "${INSTALL_ROOT}" "${CONFIG_ROOT}" "${UNIT_ROOT}"
if [[ -n "${PROVISION_KEY_FILE}" ]]; then
  install -m 600 "${PROVISION_KEY_FILE}" "${KEY_FILE}"
fi

if [[ ! -f "${KEY_FILE}" ]]; then
  echo "missing protected API key file: ${KEY_FILE}" >&2
  echo "create or provision it out of band with mode 600; never commit the key" >&2
  exit 3
fi

python3 - "${KEY_FILE}" <<'PY'
import os
import stat
import sys

path = sys.argv[1]
mode = stat.S_IMODE(os.stat(path).st_mode)
if mode & 0o077:
    raise SystemExit(f"API key file must have mode 600 or stricter: {path}")
PY

install -m 755 "${REPO_ROOT}/gateway/gateway.py" "${INSTALL_ROOT}/gateway.py"
install -m 644 \
  "${REPO_ROOT}/gateway/ds4-gateway.service" \
  "${UNIT_ROOT}/ds4-gateway.service"

systemctl --user daemon-reload
systemctl --user enable --now ds4-gateway.service

for _ in $(seq 1 30); do
  if curl --fail --silent --max-time 3 http://127.0.0.1:4000/readyz >/dev/null; then
    echo "gateway ready on http://127.0.0.1:4000"
    break
  fi
  sleep 1
done

curl --fail --silent --max-time 3 http://127.0.0.1:4000/readyz >/dev/null || {
  systemctl --user status --no-pager ds4-gateway.service >&2 || true
  exit 4
}

if [[ "$(loginctl show-user "${USER}" -p Linger --value)" != "yes" ]]; then
  echo "warning: user lingering is disabled; enable it so the gateway survives logout" >&2
fi
