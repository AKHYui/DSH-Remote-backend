#!/usr/bin/env bash
# Server-side acceptance checks for a deployed relay.
# Run as root on the server:  bash /opt/dsh-backend/accept.sh
#
# Complements scripts/verify_remote.py, which drives the relay from outside.
# This one checks what is only visible from the host: systemd state and
# sandboxing, the service account, TLS, listening sockets, credential
# revocation, audit hygiene, log redaction and file permissions.

set -uo pipefail

APP_DIR=/opt/dsh-backend
PORT="${PORT:-58443}"
SERVICE=dsh-relay
PY="$APP_DIR/.venv/bin/python"
CERT_DIR="$APP_DIR/certs"

pass=0
fail=0
ok() { echo "  PASS  $1"; pass=$((pass + 1)); }
no() { echo "  FAIL  $1"; fail=$((fail + 1)); }

cd "$APP_DIR"

# The relay speaks TLS whenever a certificate is deployed.
SCHEME=http
CACERT=""
if [ -f "$CERT_DIR/server.crt" ]; then
  SCHEME=https
  [ -f "$CERT_DIR/ca.crt" ] && CACERT="--cacert $CERT_DIR/ca.crt"
fi
base_url="${SCHEME}://127.0.0.1:${PORT}"

echo "[1] systemd"
systemctl is-active --quiet "$SERVICE" && ok "$SERVICE is active" || no "$SERVICE is active"
systemctl is-enabled --quiet "$SERVICE" && ok "$SERVICE starts at boot" || no "$SERVICE starts at boot"
ss -ltn 2>/dev/null | grep -q ":${PORT}[[:space:]]" && ok "listening on tcp/${PORT}" || no "listening on tcp/${PORT}"

echo "[2] health (${SCHEME})"
curl -fsS $CACERT "${base_url}/healthz" | grep -q '"status":"ok"' \
  && ok "local /healthz reports ok" || no "local /healthz reports ok"

if [ "$SCHEME" = "https" ]; then
  # Plaintext must no longer be served on this port.
  if curl -fsS --max-time 5 "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1; then
    no "plaintext HTTP is still served on ${PORT}"
  else
    ok "plaintext HTTP is refused on ${PORT}"
  fi
fi

echo "[3] service account and sandbox"
unit_user=$(systemctl show "$SERVICE" -p User --value)
[ -n "$unit_user" ] && [ "$unit_user" != "root" ] \
  && ok "runs as the unprivileged user '$unit_user'" \
  || no "runs as '${unit_user:-root}' (expected a dedicated user)"

listener_pid=$(ss -ltnp 2>/dev/null | grep ":${PORT}[[:space:]]" | grep -oP 'pid=\K[0-9]+' | head -1)
if [ -n "${listener_pid:-}" ]; then
  owner=$(ps -o user= -p "$listener_pid" 2>/dev/null | tr -d ' ')
  [ "$owner" = "$unit_user" ] && ok "the listening process is owned by $owner" \
                             || no "the listening process is owned by ${owner:-unknown}"
else
  no "could not identify the listening process"
fi

for prop in NoNewPrivileges PrivateTmp ProtectSystem ProtectHome; do
  value=$(systemctl show "$SERVICE" -p "$prop" --value)
  case "$prop" in
    ProtectSystem) expected=strict ;;
    *) expected=yes ;;
  esac
  [ "$value" = "$expected" ] && ok "$prop=$value" || no "$prop=$value (expected $expected)"
done
caps=$(systemctl show "$SERVICE" -p CapabilityBoundingSet --value)
[ -z "$caps" ] && ok "no capabilities retained" || no "capabilities retained: $caps"
memmax=$(systemctl show "$SERVICE" -p MemoryMax --value)
[ "$memmax" != "infinity" ] && [ -n "$memmax" ] \
  && ok "memory capped at $memmax" || no "no memory cap"

echo "[4] TLS"
if [ "$SCHEME" = "https" ]; then
  if echo | openssl s_client -connect "127.0.0.1:${PORT}" -tls1_1 2>/dev/null | grep -q "BEGIN CERTIFICATE"; then
    no "TLS 1.1 is still accepted"
  else
    ok "TLS 1.1 is refused (floor is 1.2)"
  fi
  echo | openssl s_client -connect "127.0.0.1:${PORT}" -tls1_3 2>/dev/null | grep -q "BEGIN CERTIFICATE" \
    && ok "TLS 1.3 is accepted" || no "TLS 1.3 is accepted"
  keymode=$(stat -c '%a' "$CERT_DIR/server.key" 2>/dev/null || echo missing)
  [ "$keymode" = "600" ] && ok "server.key is 0600" || no "server.key mode is ${keymode}"
  days=$(( ( $(date -d "$(openssl x509 -in "$CERT_DIR/server.crt" -noout -enddate | cut -d= -f2)" +%s) - $(date +%s) ) / 86400 ))
  [ "$days" -gt 30 ] && ok "certificate valid for another ${days} days" \
                     || no "certificate expires in ${days} days"
else
  no "no certificate deployed: the token crosses the network in clear text"
fi

echo "[5] the API schema is not published"
for path in /docs /openapi.json; do
  code=$(curl -s -o /dev/null -w '%{http_code}' $CACERT "${base_url}${path}")
  [ "$code" = "404" ] && ok "${path} is 404" || no "${path} returned ${code} (expected 404)"
done

echo "[6] tokens never reach the journal"
newest=$(journalctl -u "$SERVICE" -n 400 --no-pager 2>/dev/null | grep 'attach' | tail -1)
if [ -z "$newest" ]; then
  echo "  INFO  no attach line in the recent journal; skipping"
elif echo "$newest" | grep -q 'token=<redacted>'; then
  ok "the access log redacts the connector token"
else
  no "the access log still contains an unredacted token: ${newest:0:120}"
fi

echo "[7] credential revocation"
"$PY" - >/tmp/dsh-probe.txt 2>/dev/null <<'PY_PROBE'
import sys
sys.path.insert(0, '/opt/dsh-backend')
from app.config import Settings
from app.store import Store

settings = Settings.from_env()
settings.ensure_dirs()
store = Store(settings.db_path)
store.initialize()
device, token = store.create_device('acceptance-probe', approved_by='accept.sh')
print(device.id, token)
PY_PROBE

PROBE_ID=$(awk '{print $1}' /tmp/dsh-probe.txt)
PROBE_TOKEN=$(awk '{print $2}' /tmp/dsh-probe.txt)
rm -f /tmp/dsh-probe.txt

if [ -n "${PROBE_TOKEN:-}" ]; then
  code=$(curl -s -o /dev/null -w '%{http_code}' $CACERT \
    -H "Authorization: Bearer ${PROBE_TOKEN}" "${base_url}/api/v1/devices")
  [ "$code" = "200" ] && ok "a fresh device token authenticates (200)" \
                     || no "a fresh device token authenticates (got $code)"

  "$PY" -m app.cli revoke-device "$PROBE_ID" >/dev/null 2>&1

  code=$(curl -s -o /dev/null -w '%{http_code}' $CACERT \
    -H "Authorization: Bearer ${PROBE_TOKEN}" "${base_url}/api/v1/devices")
  [ "$code" = "401" ] && ok "the revoked token is refused (401)" \
                      || no "the revoked token is refused (got $code)"
else
  no "could not mint a probe device token"
fi

echo "[8] audit hygiene"
if "$PY" - <<'PY_AUDIT'
import sys
sys.path.insert(0, '/opt/dsh-backend')
from app.config import Settings
from app.store import Store

settings = Settings.from_env()
store = Store(settings.db_path)
store.initialize()
rows = store.recent_audit(500)
print(f"  INFO  {len(rows)} audit row(s)")

malformed = [row for row in rows if row['args_digest'] and len(row['args_digest']) != 16]
if malformed:
    print(f"  INFO  {len(malformed)} row(s) have an unexpected digest length")
    raise SystemExit(1)

# The audit table must never contain prompt text.
blob = repr(rows)
for needle in ('hello over the internet', 'content'):
    if needle in blob:
        print(f"  INFO  audit rows mention {needle!r}")
        raise SystemExit(1)
raise SystemExit(0)
PY_AUDIT
then ok "audit rows are digest-only and carry no content"; else no "audit rows look wrong"; fi

echo "[9] file permissions"
mode=$(stat -c '%a' "$APP_DIR/var/relay.db" 2>/dev/null || echo missing)
[ "$mode" = "600" ] && ok "relay.db is 0600" || no "relay.db mode is ${mode} (expected 600)"
mode=$(stat -c '%a' "$APP_DIR/var" 2>/dev/null || echo missing)
[ "$mode" = "700" ] && ok "var/ is 0700" || no "var/ mode is ${mode} (expected 700)"

echo "[10] test suite on this host"
if "$PY" -m pytest -q >/tmp/dsh-pytest.out 2>&1; then
  ok "pytest passes here"
else
  no "pytest failed here (see /tmp/dsh-pytest.out)"
fi
grep -E '[0-9]+ (passed|failed)' /tmp/dsh-pytest.out | tail -1 | sed 's/^/  INFO  /'

echo "[11] the link is not flapping"
# Regression guard for the keepalive bug: an unrecognised inbound `ping` used to
# make the relay drop the link every heartbeat interval (~31s), i.e. a reconnect
# roughly every minute for as long as it ran.
#
# Counting attaches alone cannot tell churn from a test-run burst, so the signal
# is *spread*: how many distinct minutes contain a reconnect. A burst lands in
# one minute; churn covers most of the window. The window starts when the service
# did, so pre-deploy history cannot pollute it, and it is only judged once there
# is enough of it to mean something.
started=$(systemctl show -p ActiveEnterTimestamp --value "$SERVICE")
up_seconds=$(( $(date +%s) - $(date -d "$started" +%s) ))
recent=$(journalctl -u "$SERVICE" --since "$started" --no-pager 2>/dev/null | grep 'attach' || true)
total=$(printf '%s' "$recent" | grep -c 'attach' || true)
minutes=$(printf '%s' "$recent" | awk '{print $3}' | cut -c1-5 | sort -u | wc -l)
total=${total:-0}
minutes=${minutes:-0}

if [ "$up_seconds" -lt 180 ]; then
  echo "  INFO  the service has only been up ${up_seconds}s; too early to judge flapping"
elif [ "$minutes" -lt 3 ]; then
  ok "${total} attach connection(s) in ${minutes} distinct minute(s) over ${up_seconds}s — not flapping"
else
  no "${total} attaches spread over ${minutes} distinct minutes — the link is flapping"
fi

# Supplementary: how long the current link has survived. Informational, because a
# deploy legitimately resets it.
if [ "$SCHEME" = "https" ]; then
  AGE_PROBE=$("$PY" - <<'PY_MINT'
import sys
sys.path.insert(0, '/opt/dsh-backend')
from app.config import Settings
from app.store import Store

settings = Settings.from_env()
settings.ensure_dirs()
store = Store(settings.db_path)
store.initialize()
device, token = store.create_device('link-age-probe', approved_by='accept.sh')
print(device.id, token)
PY_MINT
)
  AGE_ID=$(echo "$AGE_PROBE" | awk '{print $1}')
  AGE_TOKEN=$(echo "$AGE_PROBE" | awk '{print $2}')

  age=$("$PY" - "$AGE_TOKEN" <<'PY_AGE' 2>/dev/null | tail -1
import json
import ssl
import sys
import urllib.request

ctx = ssl.create_default_context(cafile='/opt/dsh-backend/certs/ca.crt')
request = urllib.request.Request(
    'https://127.0.0.1:58443/api/v1/devices',
    headers={'Authorization': f"Bearer {sys.argv[1]}"},
)
try:
    payload = json.load(urllib.request.urlopen(request, context=ctx, timeout=10))
except Exception:
    print(-1)
    raise SystemExit(0)
ages = [
    item['linkAgeSeconds']
    for item in payload.get('value', {}).get('items', [])
    if item.get('online') and item.get('linkAgeSeconds') is not None
]
print(max(ages) if ages else -1)
PY_AGE
)
  "$PY" -m app.cli revoke-device "$AGE_ID" >/dev/null 2>&1

  if [ -z "${age:-}" ] || [ "${age:--1}" -lt 0 ]; then
    echo "  INFO  no desktop is online right now"
  else
    echo "  INFO  the live link has been up for ${age}s"
  fi
fi

echo
echo "${pass} passed, ${fail} failed"
[ "$fail" -eq 0 ]
