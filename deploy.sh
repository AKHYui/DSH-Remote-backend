#!/usr/bin/env bash
# Idempotent deploy for the DSH Remote Bridge relay (Debian/Ubuntu).
#
# Run on the server as root:
#     bash /opt/dsh-backend/deploy.sh
#
# Environment overrides:
#     PORT=58443 HOST=0.0.0.0 MIRROR=https://pypi.tuna.tsinghua.edu.cn/simple
#
# TLS is enabled automatically when certs/server.crt and certs/server.key exist;
# the leaf is concatenated with certs/ca.crt so clients receive the full chain.
# Without them the service falls back to plaintext and says so loudly.
#
# Re-running is safe: the venv, database and unit are reused or rewritten.

set -euo pipefail

APP_DIR=/opt/dsh-backend
PORT="${PORT:-58443}"
HOST="${HOST:-0.0.0.0}"
MIRROR="${MIRROR:-https://pypi.tuna.tsinghua.edu.cn/simple}"
SERVICE=dsh-relay
PY=python3.11

cd "$APP_DIR"

echo "== [1/7] python venv =="
if [ ! -x .venv/bin/python ]; then
  if ! "$PY" -m venv .venv >/dev/null 2>&1; then
    echo "   $PY -m venv unavailable; installing ${PY}-venv"
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${PY}-venv"
    "$PY" -m venv .venv
  fi
else
  echo "   venv already present"
fi
.venv/bin/python --version

echo "== [2/7] dependencies =="
.venv/bin/python -m pip install -q --upgrade pip -i "$MIRROR"
.venv/bin/pip install -q -i "$MIRROR" fastapi 'uvicorn[standard]' pydantic
.venv/bin/python - <<'PY'
import fastapi, pydantic, uvicorn
print(f"   fastapi {fastapi.__version__} | uvicorn {uvicorn.__version__} | pydantic {pydantic.__version__}")
PY

echo "== [3/7] database =="
mkdir -p "$APP_DIR/var"
chmod 700 "$APP_DIR/var"
.venv/bin/python -m app.cli init-db
[ -f "$APP_DIR/var/relay.db" ] && chmod 600 "$APP_DIR/var/relay.db" || true

echo "== [4/7] tls =="
CERT_DIR="$APP_DIR/certs"
TLS_FLAGS=""
SCHEME=http
CACERT=""
if [ -f "$CERT_DIR/server.crt" ] && [ -f "$CERT_DIR/server.key" ]; then
  # Present leaf + CA so clients can build the chain even without the root.
  if [ -f "$CERT_DIR/ca.crt" ]; then
    cat "$CERT_DIR/server.crt" "$CERT_DIR/ca.crt" > "$CERT_DIR/server-fullchain.crt"
  else
    cp "$CERT_DIR/server.crt" "$CERT_DIR/server-fullchain.crt"
  fi
  chmod 644 "$CERT_DIR/server-fullchain.crt" "$CERT_DIR/server.crt" 2>/dev/null || true
  chmod 600 "$CERT_DIR/server.key"
  TLS_FLAGS="--ssl-certfile $CERT_DIR/server-fullchain.crt --ssl-keyfile $CERT_DIR/server.key"
  SCHEME=https
  [ -f "$CERT_DIR/ca.crt" ] && CACERT="--cacert $CERT_DIR/ca.crt"
  echo "   TLS enabled"
  openssl x509 -in "$CERT_DIR/server.crt" -noout -subject -dates -ext subjectAltName 2>/dev/null | sed 's/^/   /' || true
else
  echo "   WARNING: no certs/server.crt + certs/server.key found"
  echo "   WARNING: serving PLAINTEXT; the connector token will cross the internet in the clear"
fi

echo "== [5/7] service account =="
SERVICE_USER=dshrelay
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
  echo "   created system user $SERVICE_USER"
else
  echo "   system user $SERVICE_USER already exists"
fi
# The service must not run as root: it terminates TLS and parses untrusted input.
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR"
chmod 700 "$APP_DIR/var"
[ -f "$APP_DIR/var/relay.db" ] && chmod 600 "$APP_DIR/var/relay.db" || true
chmod 600 "$CERT_DIR/server.key" 2>/dev/null || true

echo "== [6/7] systemd unit =="
cat >"/etc/systemd/system/${SERVICE}.service" <<UNIT
[Unit]
Description=DSH Remote Bridge relay
Documentation=file://${APP_DIR}/README.md
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_USER}
WorkingDirectory=${APP_DIR}
Environment=DSH_RELAY_DB=${APP_DIR}/var/relay.db
Environment=DSH_RELAY_HOST=${HOST}
Environment=DSH_RELAY_PORT=${PORT}
Environment=DSH_RELAY_LOG_LEVEL=info
# --ws-max-size bounds a single WebSocket frame. Pinned explicitly rather than left
# to the server's default: uvicorn 0.54 defaults to 16 MiB, older releases to 1 MiB,
# and a session snapshot with a long transcript has to fit either way.
#
# --timeout-keep-alive is raised well above every client's idle timeout. At the
# default of 5s a mobile client that pools connections (Dart's HttpClient keeps
# them for 15s) could reuse one the relay had already closed, which surfaces as
# "Connection closed before full header was received" on a perfectly healthy
# link. Clients still expire locally first; this just widens the margin.
ExecStart=${APP_DIR}/.venv/bin/python -m uvicorn app.main:app \\
          --host ${HOST} --port ${PORT} \\
          --log-level info \\
          --ws-max-size 8388608 \\
          --timeout-keep-alive 75 \\
          --no-server-header \\
          ${TLS_FLAGS}
Restart=always
RestartSec=3
KillSignal=SIGINT
TimeoutStopSec=15
UMask=0077
LimitNOFILE=8192

# --- sandboxing -------------------------------------------------------------
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
ProtectClock=true
RestrictNamespaces=true
RestrictSUIDSGID=true
RestrictRealtime=true
LockPersonality=true
RestrictAddressFamilies=AF_INET AF_INET6
CapabilityBoundingSet=
AmbientCapabilities=
# Only the database directory is writable under ProtectSystem=strict.
ReadWritePaths=${APP_DIR}/var
# Backstop: this VM also runs unrelated services, so a runaway relay is killed
# and restarted rather than taking the whole host down.
MemoryMax=768M

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null
systemctl restart "$SERVICE"
sleep 2
echo "   unit state: $(systemctl is-active "$SERVICE")"

echo "== [7/7] health check (${SCHEME}) =="
ok=0
for _ in $(seq 1 30); do
  if curl -fsS $CACERT "${SCHEME}://127.0.0.1:${PORT}/healthz" >/tmp/healthz.json 2>/dev/null; then
    ok=1
    break
  fi
  sleep 0.5
done

if [ "$ok" -ne 1 ]; then
  echo "HEALTH CHECK FAILED"
  systemctl status "$SERVICE" --no-pager -l | head -30 || true
  journalctl -u "$SERVICE" -n 50 --no-pager || true
  exit 1
fi

cat /tmp/healthz.json
echo
echo "listening sockets:"
ss -ltnp | grep -E ":${PORT}\b" || true
echo "deploy ok (${SCHEME} on port ${PORT})"
