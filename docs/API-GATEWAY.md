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

Each person or automation identity receives an independently revocable key.
The gateway stores only SHA-256 digests in a mode-600 registry; a newly issued
secret is displayed once. Never paste keys into chat, shell history, Git, or an
issue.

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

On upgrade, the installer migrates the existing single key to the `owner`
identity, preserving existing clients. The scripts preserve prior Tailscale status under
`~/.local/state/ds4-gateway/`. The fail-closed rollback removes the private
route instead of restoring a previously public Funnel.

## Team key management

Run these commands on the TRX50 host. Member names may contain letters,
numbers, dot, dash, and underscore.

```bash
# Display a new secret once. Put it directly into your secret manager.
bash scripts/manage-gateway-keys.sh add alice

# List member, non-secret key ID, and creation time.
bash scripts/manage-gateway-keys.sh list

# Revoke immediately by member name or key ID; no service restart is needed.
bash scripts/manage-gateway-keys.sh revoke alice
```

Use separate identities for scheduled jobs, for example `ops-nightly` and
`coding-swarm`, rather than sharing a human key. Existing in-flight streams are
not interrupted by a revocation, but the key cannot start another request.

## OpenAI-compatible clients

Use the private HTTPS base URL ending in `/v1`, the protected bearer key, and
the exact served model ID:

```text
base URL: https://<tailnet-hostname>/v1
model: deepseek-v4-flash-0731
API key: read from the client's secret store
```

Generic OpenAI-compatible clients can use:

```bash
export OPENAI_BASE_URL=https://<tailnet-hostname>/v1
export OPENAI_API_KEY="$(security find-generic-password -w -s ds4-gateway)"
```

On Linux, inject `OPENAI_API_KEY` from the team's secret manager instead of a
shell profile. Configure coding agents with the same base URL, key, and exact
model ID. Do not point clients at raw port 30000.

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

Issue a replacement key under a temporary member name, update that client's
secret store, verify a real generation, and revoke the old member. Registry
changes are read on every request, so no gateway restart is required.

To restart the service after a binary or configuration update:

```bash
systemctl --user restart ds4-gateway.service
```

To remove the gateway, clear Tailscale Serve and disable the user service:

```bash
tailscale serve reset
systemctl --user disable --now ds4-gateway.service
```

The current vLLM container publishes port 30000 on the host. The authenticated
HTTPS gateway is the supported client path, but host firewall or container-bind
hardening is still required if untrusted devices can reach the LAN interface.
