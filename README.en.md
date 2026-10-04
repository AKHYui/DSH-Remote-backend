# DSH Remote Bridge Relay (backend)
[简体中文](README.md) | **English**

A FastAPI + SQLite relay: the desktop plugin dials **out** to it over WSS, the phone sends commands over HTTPS + WSS. It authenticates, rate-limits, forwards and audits — nothing else.

## What it is

- A **dumb pipe**: it never parses or stores session content or model output. The database holds credential hashes, device rows and audit digests only.
- The desktop **opens no inbound port**: the plugin is the dialling side, so a home NAT or a machine without a public IP works fine.
- Two channels: plugin ↔ relay over WSS (`/api/v1/attach`); phone ↔ relay over HTTPS + SSE + WS. Forwarding is an **allowlist** (11 ops) and anything else gets `501 op_not_supported`; there is no HTTP admin surface — issuing and revoking tokens requires the CLI on the relay host.

## Architecture

```
Phone app ──HTTPS / WSS──▶ Relay (FastAPI + SQLite) ◀──outbound WSS── Desktop plugin (DSH) ──▶ DSH Host
                                 └─ var/relay.db: credential hashes, device rows, audit digests (no session content)
```
The relay forwards and keeps accounts; it does not read what it carries, and it never connects to the desktop. The wire protocol, HTTP/event API and op allowlist are in [docs/PROTOCOL.md](docs/PROTOCOL.md).

## Requirements

| Item | Requirement |
|---|---|
| OS | Debian 12 / Ubuntu 22.04 or newer (`deploy.sh` targets Debian-family only), systemd |
| Python | 3.11+ (`deploy.sh` uses `python3.11`) |
| Tools | `curl`, `openssl`, `ss` (`iproute2`), `git` |
| Network | A public IP (or a reverse proxy); one public port (example `58443`), with `22` restricted to your own IP |

> Windows / macOS run it too (`python -m uvicorn app.main:app`), but there is no systemd; the steps below are Linux.

## Deployment

Two paths, same result: **A** work through steps 1–8 by hand (each has commands and the output to expect); **B** run `deploy.sh` from step 9 — it performs steps 2–8 in one go and is safe to re-run, and `accept.sh` then runs the server-side acceptance checks.

> Both scripts hard-code `/opt/dsh-backend`. If you install elsewhere, change `APP_DIR` in them as well.

### 1. Prerequisites
```bash
sudo apt-get update && sudo apt-get install -y python3.11 python3.11-venv curl openssl iproute2 git ca-certificates
python3.11 -V                        # expect Python 3.11.x
sudo ufw allow 58443/tcp             # on a cloud VM, allow the same port in the security group
```

### 2. Service account and directories
The relay terminates TLS and parses untrusted input, so it does not run as root.
```bash
sudo useradd --system --home-dir /opt/dsh-backend --shell /usr/sbin/nologin dshrelay
sudo mkdir -p /opt/dsh-backend && sudo chown dshrelay:dshrelay /opt/dsh-backend
```

### 3. Get the code and install dependencies
`git clone` needs the install directory to be empty; if code is already there, use `rsync -a --delete ./ user@your-server:/opt/dsh-backend/` instead.
```bash
sudo -u dshrelay git clone https://github.com/AKHYui/DSH-Remote-backend.git /opt/dsh-backend
cd /opt/dsh-backend
sudo -u dshrelay mkdir -p var certs && sudo -u dshrelay chmod 700 var    # var: service account only
sudo -u dshrelay python3.11 -m venv .venv
sudo -u dshrelay .venv/bin/python -m pip install -U pip setuptools
sudo -u dshrelay .venv/bin/pip install -e .              # runtime dependencies
sudo -u dshrelay .venv/bin/pip install -e ".[dev]"       # add this to run the tests
.venv/bin/python -V
```

### 4. Initialise the database
```bash
sudo -u dshrelay .venv/bin/python -m app.cli init-db     # {"ok": true, "db": ".../var/relay.db"}
sudo chmod 600 /opt/dsh-backend/var/relay.db
```

### 5. Issue tokens
A token is **shown once**, at issue time; only its `SHA-256` is stored. Lost one? Issue a new one. `issue-connector` prints a block you can paste into the plugin config (`serverUrl` / `connectorToken` / `deviceId`).
```bash
sudo -u dshrelay .venv/bin/python -m app.cli issue-connector --name home-pc   # for the desktop plugin
sudo -u dshrelay .venv/bin/python -m app.cli issue-device --name my-phone     # for the phone
sudo -u dshrelay .venv/bin/python -m app.cli pair-start --name "Pixel 8"      # or pair with a code
sudo -u dshrelay .venv/bin/python -m app.cli pair-approve 123456 --name "Pixel 8"
```

- The plugin side then reads `serverUrl: wss://<your-relay-host>:58443/api/v1/attach`, with `deviceId` set to the `--name` you passed.
- `revoke-device <deviceId>` / `revoke-connector <connectorId>` take effect **immediately** (the phone token gets 401 on its next call, the connector is refused on its next reconnect with close code `4401`); phones and desktops are revoked independently.

### 6. TLS
Put the files in `certs/`: `deploy.sh` enables HTTPS automatically whenever `server.crt` + `server.key` are present.
```bash
cd /opt/dsh-backend/certs
# 1) An internal CA (self-signed, 10 years)
sudo openssl req -x509 -newkey rsa:4096 -nodes -keyout ca.key -sha256 -days 3650 \
  -subj "/CN=DSH Relay Internal CA" -out ca.crt
# 2) The server certificate: the SAN must list the addresses you actually use
sudo openssl req -new -newkey rsa:2048 -nodes -keyout server.key -subj "/CN=dsh-relay" \
  -addext "subjectAltName=IP:203.0.113.10,IP:127.0.0.1,DNS:localhost" -out server.csr
sudo openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
  -days 3400 -sha256 -copy_extensions copy -out server.crt
sudo chmod 600 ca.key server.key && sudo chmod 644 ca.crt server.crt
```

- Clients must trust `ca.crt`: the plugin takes it as `tlsCaFile`, the phone app ships it inside the APK. **`ca.key` stays on this server** — never commit it, never send it anywhere.
- With a public certificate (Let's Encrypt and friends): put the **full chain** in `certs/server.crt` and the key in `certs/server.key`, and do **not** place a `certs/ca.crt` (otherwise `deploy.sh` appends the chain twice).

### 7. systemd
**The address actually listened on comes from `uvicorn --host/--port`**; this is a working minimum, while `deploy.sh` writes a fuller unit with more sandboxing.
```ini
[Unit]
Description=DSH Remote Bridge relay
After=network-online.target
[Service]
Type=simple
User=dshrelay
WorkingDirectory=/opt/dsh-backend
Environment=DSH_RELAY_DB=/opt/dsh-backend/var/relay.db
ExecStart=/opt/dsh-backend/.venv/bin/python -m uvicorn app.main:app \
  --host 0.0.0.0 --port 58443 --log-level info --no-server-header \
  --ws-max-size 8388608 --timeout-keep-alive 75 --ssl-certfile /opt/dsh-backend/certs/server-fullchain.crt --ssl-keyfile /opt/dsh-backend/certs/server.key
Restart=always
RestartSec=3
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/opt/dsh-backend/var
CapabilityBoundingSet=
MemoryMax=768M
[Install]
WantedBy=multi-user.target
```
```bash
sudo systemctl daemon-reload && sudo systemctl enable --now dsh-relay
systemctl is-active dsh-relay        # active
```
Keep both `--ws-max-size 8388608` (whether a session snapshot fits in one frame) and `--timeout-keep-alive 75` (it must exceed the 15-second connection reuse of the mobile client). Starting by hand instead: add `--ssl-certfile certs/server-fullchain.crt --ssl-keyfile certs/server.key`; `deploy.sh` generates the fullchain file.

### 8. Health check
```bash
curl -fsS --cacert /opt/dsh-backend/certs/ca.crt https://127.0.0.1:58443/healthz
# {"status":"ok","version":"0.1.0","protocol":1,"devices":{"total":1,"online":1},"phones":1,...}
sudo ss -ltnp | grep ':58443'
```
`devices.online` tells you whether a desktop is connected; `phones` is the number of paired phones.

### 9. Idempotent deploy and acceptance
For every later update — new code, changed settings, reinstalled dependencies — run these two (both scripts are written for root, so use `sudo`):
```bash
sudo bash /opt/dsh-backend/deploy.sh     # covers steps 2–8; last line: deploy ok (https on port 58443)
sudo bash /opt/dsh-backend/accept.sh     # 11 acceptance groups; last line like: 26 passed, 0 failed
```
- `deploy.sh` is safe to re-run; override the defaults with `PORT=58443 HOST=0.0.0.0 MIRROR=<pypi mirror>`; with no certificate it warns loudly and falls back to plaintext.
- `accept.sh` checks what is only visible from the host: systemd state and sandboxing, the TLS version floor, `server.key` permissions, whether `/docs` is a 404, access-log redaction, immediate revocation, digest-only audit rows, `relay.db`/`var/` permissions, the local pytest run, and whether the link is flapping.

### 10. Logs
```bash
journalctl -u dsh-relay -n 80 --no-pager      # last 80 lines; add -f to follow
journalctl -u dsh-relay --since '10 min ago' | grep attach    # plugin attaches only
```
To see every event the plugin pushes (chatty — leave it off normally), set `DSH_RELAY_DEBUG_EVENTS` to `1` and `systemctl restart dsh-relay`; set it back to empty and restart when you are done.

## Configuration

Everything is an environment variable with a working default; set them with `Environment=` in the systemd unit.

| Variable | Default | Meaning |
|---|---|---|
| `DSH_RELAY_DB` | `backend/var/relay.db` | SQLite path (directory 0700, file 0600) |
| `DSH_RELAY_HOST` | `127.0.0.1` | Reserved: **the listen address comes from `uvicorn --host`** |
| `DSH_RELAY_PORT` | `8787` | Only feeds the connection hint the CLI prints; **the real port comes from `uvicorn --port`** |
| `DSH_RELAY_PUBLIC_URL` | empty | Reserved; not used by any code path |
| `DSH_RELAY_LOG_LEVEL` | `info` | Reserved; the effective level comes from `uvicorn --log-level` |
| `DSH_RELAY_PAIR_TTL_SECONDS` | `300` | Pairing-code lifetime (seconds) |
| `DSH_RELAY_PAIR_ATTEMPTS_PER_HOUR` | `20` | Pairing rate limit (sliding window, per hour) |
| `DSH_RELAY_OP_REQUESTS_PER_MINUTE` | `240` | op and stream rate limit (sliding window, per minute) |
| `DSH_RELAY_REQUEST_TIMEOUT_SECONDS` | `60` | Timeout for one op forwarded to the desktop |
| `DSH_RELAY_STREAM_IDLE_TIMEOUT_SECONDS` | `600` | Reserved; not used by any code path |
| `DSH_RELAY_APPROVAL_TTL_SECONDS` | `120` | How long an approval/question waits on the phone |
| `DSH_RELAY_HEARTBEAT_SECONDS` | `30` | Event WebSocket heartbeat interval (minimum 5) |
| `DSH_RELAY_MAX_PENDING_PER_DEVICE` | `64` | Requests in flight per desktop |
| `DSH_RELAY_EVENT_QUEUE_SIZE` | `512` | Buffered frames per stream channel |
| `DSH_RELAY_MAX_REQUEST_BYTES` | `4194304` | Request body cap (4 MiB); larger ones get 413 |
| `DSH_RELAY_ENABLE_DOCS` | `false` | Serve `/docs` and `/openapi.json` (404 by default) |
| `DSH_RELAY_REDACT_ACCESS_LOG` | `true` | Rewrite `token=` to `<redacted>` in the access log |
| `DSH_RELAY_TRUST_PROXY_HEADERS` | `false` | Enable behind a trusted reverse proxy to read the real client IP |
| `DSH_RELAY_DEBUG_EVENTS` | `false` | Log every event the plugin pushes (troubleshooting) |

> Keys marked *Reserved* are parsed correctly but currently drive no behaviour; they are listed so that finding the variable name does not mislead you into thinking it does something.

CLI subcommands (`python -m app.cli <subcommand>`; add `--db <path>` to override the database path):

| Subcommand | Purpose |
|---|---|
| `init-db` | Create the schema and exit (every other command does this too) |
| `issue-connector --name home-pc` / `issue-device --name my-phone` | Mint a desktop plugin token / a phone token |
| `pair-start --name "Pixel 8"` / `pair-approve <code> [--name] [--by]` | Generate / approve a pairing code |
| `devices` / `connectors` / `desktops` | List paired phones / connectors / DSH hosts that have dialled in |
| `pending` | List pairing codes not yet claimed |
| `revoke-device <deviceId>` / `revoke-connector <connectorId>` | Revoke immediately (soft delete; audit rows stay) |
| `remove-desktop <id…> [--simulators] [--dry-run]` | Forget desktop rows — see below |
| `audit [--limit 50]` | Print recent audit rows |

## Operations and troubleshooting

| Symptom | What to do |
|---|---|
| The phone cannot connect: certificate error | The client must trust the CA that signed the server certificate: `tlsCaFile` for the plugin, the bundled `ca.crt` for the app. Compare fingerprints: `openssl x509 -in certs/ca.crt -noout -fingerprint -sha256` |
| The phone cannot connect: refused / timeout | Try locally first: `curl -fsS --cacert certs/ca.crt https://127.0.0.1:58443/healthz`. Works locally but not from outside means security group / `ufw` / reverse proxy |
| The plugin cannot connect, close code `4401` | The connector token is invalid or revoked: issue a new one and update the plugin config |
| The plugin cannot connect, close code `4001` | Another machine took over the same `deviceId`: give every desktop its own id |
| The plugin cannot connect, close code `4400` | Protocol version mismatch, or an empty `deviceId`: run the same version on both sides |
| The plugin refuses plaintext | The plugin rejects `ws://` by default: deploy TLS, or enable the plugin's `allowInsecure` for local development only |
| Token in the access log | It should read `token=<redacted>`; if you see the value, check whether `DSH_RELAY_REDACT_ACCESS_LOG` was set to `false` |
| `/docs` returns 404 | Expected; to read the schema temporarily, set `DSH_RELAY_ENABLE_DOCS=1` and restart |
| Requests return 413 / 429 / 504 | Body over 4 MiB / rate limit hit / the desktop did not answer in time; see `MAX_REQUEST_BYTES`, `OP_REQUESTS_PER_MINUTE`, `REQUEST_TIMEOUT_SECONDS` |
| Offline devices such as `remote-sim` appear in the list | Rows the acceptance simulators left in the `desktops` table: inspect with `... -m app.cli desktops`, preview with `remove-desktop --simulators --dry-run`, then drop `--dry-run` |

**Security boundary**: running this on the public internet puts anyone holding a phone token directly in front of that DSH instance (which can execute commands on the desktop). Therefore: TLS only, tokens stored as hashes and revoked individually, approvals never auto-approved, `/docs` off by default.

## Tests
```bash
cd /opt/dsh-backend
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m pytest -q                    # 98 tests
DSH_PLUGIN_DIR=/path/to/DSH-Remote-plugin .venv/bin/python tools/e2e_smoke.py   # cross-language end to end
```
`e2e_smoke.py` drives a real relay with the real plugin, so it needs the plugin checkout (`../plugin` by default). `pytest` reads its configuration from `pyproject.toml`. On Windows, replace `.venv/bin/python` with `.venv\Scripts\python.exe`.

## Repository layout

```
app/                 The relay: settings, protocol constants, store, auth, core, HTTP/WS API, CLI
tests/               98 tests (including the cross-language allowlist comparison)
tools/e2e_smoke.py   End-to-end smoke test: local relay + the real plugin
docs/PROTOCOL.md     Wire protocol, HTTP/event API and op allowlist (kept in step with the plugin repo)
deploy.sh / accept.sh  Idempotent deploy (steps 2–8) / 11 server-side acceptance groups
pyproject.toml       Dependencies, Python version and pytest configuration; conftest.py holds the fixtures
```

## Related repositories

- Desktop plugin: [AKHYui/DSH-Remote-plugin](https://github.com/AKHYui/DSH-Remote-plugin)
- Relay backend: this repository, [AKHYui/DSH-Remote-backend](https://github.com/AKHYui/DSH-Remote-backend)
- Phone app: [AKHYui/DSH-Remote-app](https://github.com/AKHYui/DSH-Remote-app)

## License

MIT — see [LICENSE](LICENSE).
