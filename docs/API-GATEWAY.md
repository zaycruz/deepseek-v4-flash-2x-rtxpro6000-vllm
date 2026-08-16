# Private production API gateway

The production model stays on the host-only vLLM service while a small gateway
provides bearer authentication and streaming-safe forwarding:

```text
Codex / coding CLI
  -> private Tailscale HTTPS
  -> 127.0.0.1:4000 authenticated gateway
  -> 127.0.0.1:30000 vLLM
```

Tailscale Funnel must remain disabled. Tailnet identity and the bearer key are
independent controls; possessing only one is insufficient. The gateway accepts
only `/v1` routes, strips the client credential before forwarding, never logs
request bodies, and relays SSE responses without buffering the completed
generation.

## Install

Provision the same random key to the host and authorized clients through a
secret manager. Never paste it into chat, shell history, Git, or an issue.

```bash
install -d -m 700 ~/.config/ds4-gateway
install -m 600 /secure/input/api-key ~/.config/ds4-gateway/api-key
loginctl enable-linger "$USER"
./scripts/install-gateway.sh
./scripts/configure-tailscale-gateway.sh
```

For automated provisioning, pass a protected source path without putting the
key itself in an argument:

```bash
PROVISION_KEY_FILE=/secure/input/api-key ./scripts/install-gateway.sh
```

The scripts preserve the prior Tailscale configuration under
`~/.local/state/ds4-gateway/` and print its exact rollback command.

## OpenAI-compatible clients

Use the private HTTPS base URL ending in `/v1`, the protected bearer key, and
the exact served model ID:

```text
base URL: https://<tailnet-hostname>/v1
model: deepseek-v4-flash-0731
API key: read from the client's secret store
```

For Codex Router, keep the stable user-facing slug if desired, but map its
`upstreamModel` to `deepseek-v4-flash-0731` and advertise a 12,288-token context
window. A larger catalog value causes clients to send requests the production
profile cannot accept.

## Verification

```bash
python3 -m unittest -v gateway/test_gateway.py
systemctl --user is-active ds4-gateway.service
curl -fsS http://127.0.0.1:4000/readyz
tailscale serve status --json
```

Then run real response, streaming, tool-call, and compaction checks through the
client router. A successful `/v1/models` request alone does not prove coding
agent compatibility.

## Rotation and rollback

Replace the protected key on the host and every authorized client, then run:

```bash
systemctl --user restart ds4-gateway.service
```

To remove the gateway, restore the Tailscale backup printed during setup and
disable the user service:

```bash
tailscale serve set-config ~/.local/state/ds4-gateway/<backup>.json
systemctl --user disable --now ds4-gateway.service
```

The current vLLM container publishes port 30000 on the host. The authenticated
HTTPS gateway is the supported client path, but host firewall or container-bind
hardening is still required if untrusted devices can reach the LAN interface.
