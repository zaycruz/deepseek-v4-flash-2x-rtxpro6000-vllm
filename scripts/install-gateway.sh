#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
INSTALL_ROOT=${INSTALL_ROOT:-${HOME}/.local/share/ds4-gateway}
CONFIG_ROOT=${CONFIG_ROOT:-${HOME}/.config/ds4-gateway}
UNIT_ROOT=${UNIT_ROOT:-${HOME}/.config/systemd/user}
LEGACY_KEY_FILE=${DS4_GATEWAY_API_KEY_FILE:-${CONFIG_ROOT}/api-key}
KEYS_FILE=${DS4_GATEWAY_API_KEYS_FILE:-${CONFIG_ROOT}/keys.json}
PROVISION_KEY_FILE=${PROVISION_KEY_FILE:-}

for command in python3 systemctl curl loginctl; do
  command -v "${command}" >/dev/null || {
    echo "missing required command: ${command}" >&2
    exit 2
  }
done

install -d -m 700 "${INSTALL_ROOT}" "${CONFIG_ROOT}" "${UNIT_ROOT}"
if [[ -n "${PROVISION_KEY_FILE}" ]]; then
  install -m 600 "${PROVISION_KEY_FILE}" "${LEGACY_KEY_FILE}"
fi

if [[ ! -f "${KEYS_FILE}" ]]; then
  if [[ ! -f "${LEGACY_KEY_FILE}" ]]; then
    echo "missing key registry and migration key: ${KEYS_FILE}" >&2
    echo "provision a protected key with PROVISION_KEY_FILE for the initial owner" >&2
    exit 3
  fi
  python3 "${REPO_ROOT}/gateway/keys.py" --registry "${KEYS_FILE}" \
    import-file owner "${LEGACY_KEY_FILE}"
  echo "migrated existing key as member=owner"
fi

python3 "${REPO_ROOT}/gateway/keys.py" --registry "${KEYS_FILE}" list >/dev/null

install -m 755 "${REPO_ROOT}/gateway/gateway.py" "${INSTALL_ROOT}/gateway.py"
install -m 755 "${REPO_ROOT}/gateway/keys.py" "${INSTALL_ROOT}/keys.py"
install -m 644 \
  "${REPO_ROOT}/gateway/ds4-gateway.service" \
  "${UNIT_ROOT}/ds4-gateway.service"

systemctl --user daemon-reload
systemctl --user enable ds4-gateway.service
systemctl --user restart ds4-gateway.service

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
