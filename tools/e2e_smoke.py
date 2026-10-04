"""Cross-language end-to-end smoke test.

Starts a real relay, launches the real Node plugin (via `plugin/tools/simulate.mjs`
with a stub DSH context), and exercises the whole contract: handshake, unary ops,
SSE streaming, event fan-out and the approval round trip — including the deferral
path when the phone does not answer.

Usage (from the backend directory):

    .venv\\Scripts\\python.exe tools\\e2e_smoke.py

Exits 0 when every check passes, 1 otherwise.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import uvicorn
import websockets

BACKEND_DIR = Path(__file__).resolve().parent.parent
# The plugin is a separate repository. When the two are checked out side by side
# this finds it; otherwise point `DSH_PLUGIN_DIR` at the plugin checkout. This is
# the only thing in this repository that needs the plugin (and nothing in the
# plugin needs this one) — a cross-language test has to have both halves.
PLUGIN_DIR = Path(os.environ.get("DSH_PLUGIN_DIR") or (BACKEND_DIR.parent / "plugin"))
SIMULATOR = PLUGIN_DIR / "tools" / "simulate.mjs"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402
from app.store import Store  # noqa: E402

DEVICE_ID = "dev-sim"
ASK_DEVICE_ID = "dev-ask"
DEFER_DEVICE_ID = "dev-defer"

_results: list[tuple[bool, str]] = []


def check(condition: bool, label: str) -> bool:
    _results.append((bool(condition), label))
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    return bool(condition)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class Relay:
    def __init__(self, settings: Settings, port: int) -> None:
        self.port = port
        self.http = f"http://127.0.0.1:{port}"
        self.ws = f"ws://127.0.0.1:{port}"
        self._server = uvicorn.Server(
            uvicorn.Config(create_app(settings), host="127.0.0.1", port=port, log_level="warning")
        )
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def start(self, timeout: float = 20.0) -> None:
        self._thread.start()
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._server.started:
                return
            if not self._thread.is_alive():  # pragma: no cover
                raise RuntimeError("relay exited during startup")
            time.sleep(0.05)
        raise TimeoutError("relay did not start")

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=10)


class Simulator:
    """The real plugin, driven through a stub DSH context."""

    def __init__(self, relay: Relay, token: str, device_id: str, extra: list[str] | None = None) -> None:
        node = shutil.which("node")
        if node is None:
            raise RuntimeError("node was not found on PATH")
        if not SIMULATOR.exists():
            raise RuntimeError(
                f"the plugin simulator was not found at {SIMULATOR}; "
                "check the plugin out somewhere and set DSH_PLUGIN_DIR to it"
            )
        self.device_id = device_id
        self.lines: list[str] = []
        self._process = subprocess.Popen(
            [
                node,
                str(SIMULATOR),
                "--server",
                f"{relay.ws}/api/v1/attach",
                "--token",
                token,
                "--device-id",
                device_id,
                "--log",
                "info",
                *(extra or []),
            ],
            cwd=str(PLUGIN_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self) -> None:
        assert self._process.stdout is not None
        for line in self._process.stdout:
            self.lines.append(line.rstrip())
            print(f"    [plugin:{self.device_id}] {line.rstrip()}")

    def wait_for_log(self, needle: str, timeout: float = 30.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if any(needle in line for line in self.lines):
                return True
            time.sleep(0.1)
        return False

    def stop(self) -> None:
        with contextlib.suppress(Exception):
            self._process.terminate()
        with contextlib.suppress(Exception):
            self._process.wait(timeout=10)
        with contextlib.suppress(Exception):
            self._process.kill()


async def wait_for_online(http: httpx.AsyncClient, headers: dict[str, str], device_id: str, timeout: float = 25.0):
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        response = await http.get("/api/v1/devices", headers=headers)
        last = response.json()
        items = {item["id"]: item for item in last.get("value", {}).get("items", [])}
        if items.get(device_id, {}).get("online"):
            return items[device_id]
        await asyncio.sleep(0.2)
    raise AssertionError(f"{device_id} never came online; last /devices: {json.dumps(last)[:400]}")


async def scenario(relay: Relay, connector_token: str, device_token: str, db_path: Path) -> None:
    headers = {"Authorization": f"Bearer {device_token}"}
    simulators: list[Simulator] = []

    try:
        # ---------------------------------------------------------------- handshake
        print("\n[1] handshake and device registry")
        simulator = Simulator(relay, connector_token, DEVICE_ID, ["--events", "0"])
        simulators.append(simulator)

        async with httpx.AsyncClient(base_url=relay.http, timeout=30) as http:
            info = await wait_for_online(http, headers, DEVICE_ID)
            check(info["online"] is True, "the plugin appears online")
            check(info["platform"] == "simulator", f"platform is reported ({info['platform']})")
            check(
                sorted(info["capabilities"]) == ["approvals", "events", "ops"],
                f"capabilities are advertised ({info['capabilities']})",
            )
            health = (await http.get("/healthz")).json()
            check(health["devices"] == {"total": 1, "online": 1}, f"healthz counts one online device: {health['devices']}")

            # ------------------------------------------------------------ unary ops
            print("\n[2] unary ops through typertGateway")
            listed = await http.post(
                f"/api/v1/devices/{DEVICE_ID}/op", json={"op": "session.list", "args": {}}, headers=headers
            )
            body = listed.json()
            check(listed.status_code == 200 and body.get("ok") is True, "session.list returns ok")
            session_ids = [item["sessionId"] for item in body["value"]["items"]]
            check(session_ids == ["sim-session-1", "sim-session-2"], f"session ids round-trip: {session_ids}")

            info_op = await http.post(
                f"/api/v1/devices/{DEVICE_ID}/op", json={"op": "harness.info", "args": {}}, headers=headers
            )
            value = info_op.json()["value"]
            check(value["deviceId"] == DEVICE_ID, "harness.info is served locally by the plugin")
            check(value["gateway"] is True, "the plugin sees its Remote gateway")

            prompt = await http.post(
                f"/api/v1/devices/{DEVICE_ID}/op",
                json={"op": "session.prompt", "args": {"sessionId": "sim-session-1", "content": [{"type": "text", "text": "hi from the relay"}]}},
                headers=headers,
            )
            check(prompt.json()["value"] == {"accepted": True}, "session.prompt is accepted")
            check(
                simulator.wait_for_log("simulator received a prompt", timeout=10),
                "the prompt reached the plugin",
            )

            rejected = await http.post(
                f"/api/v1/devices/{DEVICE_ID}/op", json={"op": "terminal.create", "args": {}}, headers=headers
            )
            check(rejected.status_code == 501, "an op outside the allowlist is refused by the relay")

            # ------------------------------------------------------------- attachments
            #
            # Attachment bytes travel as base64 inside the op args, so this is the
            # one path where a whole file crosses the HTTP body limit, the plugin's
            # WebSocket frame and the gateway argument check. 1 MiB of raw bytes is
            # 1.4 MiB of base64 — big enough that a truncating layer fails to decode
            # rather than quietly "passing".
            print("\n[2b] fileUploads.upload (attachment bytes through every layer)")
            payload = bytes((index % 251 for index in range(1024 * 1024)))
            upload = await http.post(
                f"/api/v1/devices/{DEVICE_ID}/op",
                json={
                    "op": "fileUploads.upload",
                    "args": {
                        "agentId": "sim-session-1",
                        "request": {
                            "data": base64.b64encode(payload).decode("ascii"),
                            "name": "e2e-probe.bin",
                        },
                    },
                },
                headers=headers,
            )
            upload_body = upload.json()
            check(upload.status_code == 200 and upload_body.get("ok") is True, "fileUploads.upload is accepted")
            receipt = (upload_body.get("value") or {}).get("receiptId", "")
            file_info = (upload_body.get("value") or {}).get("file") or {}
            check(bool(receipt), f"a receipt came back: {receipt}")
            check(file_info.get("bytes") == len(payload), f"all {len(payload)} byte(s) survived the round trip")
            check(file_info.get("name") == "e2e-probe.bin", "the file name survived the round trip")
            check(
                simulator.wait_for_log("simulator staged 1048576 byte(s)", timeout=10),
                "the upload reached the plugin with the right length",
            )

            # --------------------------------------------------------------- streams
            print("\n[3] SSE streaming (session.follow)")
            frames: list[str] = []
            async with http.stream(
                "POST",
                f"/api/v1/devices/{DEVICE_ID}/stream",
                json={"op": "session.follow", "args": {"address": {"kind": "session", "sessionId": "sim-session-1"}}},
                headers=headers,
                timeout=30,
            ) as response:
                check(response.status_code == 200, "the stream endpoint accepts session.follow")
                check(
                    response.headers.get("content-type", "").startswith("text/event-stream"),
                    "the response is text/event-stream",
                )
                async for chunk in response.aiter_text():
                    frames.append(chunk)
                    if "event: chunk" in "".join(frames):
                        break
            joined = "".join(frames)
            check("event: open" in joined, "the stream opens")
            check("event: chunk" in joined, "at least one chunk arrived")

            # ---------------------------------------------------------------- events
            print("\n[4] event fan-out over the phone socket")
            events_url = f"{relay.ws}/api/v1/events?token={device_token}"
            async with websockets.connect(events_url) as phone:
                ready = json.loads(await phone.recv())
                check(ready["t"] == "ready", "the phone socket reports ready")
                await phone.send(
                    json.dumps(
                        {
                            "t": "sub",
                            "id": "s1",
                            "deviceId": DEVICE_ID,
                            "topics": ["session.event"],
                            "args": {},
                        }
                    )
                )
                ack = json.loads(await phone.recv())
                check(ack == {"t": "ack", "id": "s1", "ok": True}, "subscription is acknowledged")

                # The first simulator runs with --events 0, so start a second one
                # with a fast cadence to observe real fan-out.
                simulators.append(Simulator(relay, connector_token, "dev-events", ["--events", "300"]))
                await wait_for_online(http, headers, "dev-events")
                await phone.send(
                    json.dumps({"t": "sub", "id": "s2", "deviceId": "dev-events", "topics": ["session.event"], "args": {}})
                )
                seen = None
                deadline = time.time() + 20
                while time.time() < deadline:
                    frame = json.loads(await asyncio.wait_for(phone.recv(), timeout=20))
                    if frame.get("subId") == "s2" and frame.get("topic") == "session.event":
                        seen = frame
                        break
                check(seen is not None, "a session event reaches the phone")
                if seen:
                    check(seen["payload"]["sessionId"] == "sim-session-1", "the event carries its session id")
                    check(isinstance(seen["payload"]["seq"], int), "the event carries a monotonic seq")

                # ------------------------------------------------------- approvals
                print("\n[5] approval round trip")
                # --ask-wait models the real built-in forwarder: the request is
                # published to the desktop and left pending, so the phone winning the
                # race has to end it explicitly. Without it the simulator answers
                # "unavailable" immediately and there is no desktop prompt to withdraw.
                asker = Simulator(
                    relay,
                    connector_token,
                    ASK_DEVICE_ID,
                    ["--events", "0", "--ask", "--ask-wait", "--approval-timeout-ms", "8000"],
                )
                simulators.append(asker)
                await wait_for_online(http, headers, ASK_DEVICE_ID)
                await phone.send(
                    json.dumps({"t": "sub", "id": "s3", "deviceId": ASK_DEVICE_ID, "topics": ["approval.ask"], "args": {}})
                )

                ask = None
                deadline = time.time() + 25
                while time.time() < deadline:
                    frame = json.loads(await asyncio.wait_for(phone.recv(), timeout=25))
                    if frame.get("subId") == "s3" and frame.get("topic") == "approval.ask":
                        ask = frame
                        break
                check(ask is not None, "the approval ask reaches the phone")

                if ask:
                    ask_id = ask["payload"]["askId"]
                    resolved = await http.post(
                        f"/api/v1/approvals/{ask_id}", json={"decision": "approved"}, headers=headers
                    )
                    check(resolved.status_code == 200, "the phone decision is accepted")
                    check(
                        asker.wait_for_log("approval outcome: allowed-once", timeout=15),
                        "the plugin applied 'allowed-once' from the phone decision",
                    )
                    check(
                        asker.wait_for_log("desktop prompt withdrawn for approval/request", timeout=15),
                        "the losing desktop prompt was withdrawn once the phone answered",
                    )
                    again = await http.post(
                        f"/api/v1/approvals/{ask_id}", json={"decision": "denied"}, headers=headers
                    )
                    check(again.status_code == 404, "an ask is single-use")

                # ------------------------------------------------- deferral path
                print("\n[6] deferral when the phone does not answer")
                deferrer = Simulator(
                    relay,
                    connector_token,
                    DEFER_DEVICE_ID,
                    ["--events", "0", "--ask", "--approval-timeout-ms", "2000"],
                )
                simulators.append(deferrer)
                await wait_for_online(http, headers, DEFER_DEVICE_ID)
                check(
                    deferrer.wait_for_log("approval was NOT answered by the phone", timeout=25),
                    "an unanswered ask falls through to the desktop answerer",
                )

            # ----------------------------------------------------------------- audit
            print("\n[7] audit trail")
            audit = Store(db_path)
            audit.initialize()
            try:
                rows = audit.recent_audit(50)
                ops = {row["op"] for row in rows}
                check("session.list" in ops, "session.list was audited")
                check("session.prompt" in ops, "session.prompt was audited")
                check(
                    all(len(row["args_digest"] or "") == 16 for row in rows),
                    "audit rows store only a short digest of the arguments",
                )
            finally:
                audit.close()

    finally:
        for simulator in simulators:
            simulator.stop()


def main() -> int:
    # Checked before anything is started, so a missing plugin checkout reads as one
    # clear line instead of a failed check in the middle of the run.
    if not SIMULATOR.exists():
        print(f"the plugin simulator was not found at {SIMULATOR}")
        print("check the plugin repository out and set DSH_PLUGIN_DIR to its root, e.g.")
        print(r'  $env:DSH_PLUGIN_DIR = "D:\path\to\plugin"')
        return 2

    db_path = BACKEND_DIR / "var" / "e2e-smoke.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        with contextlib.suppress(FileNotFoundError):
            Path(str(db_path) + suffix).unlink()

    settings = Settings(db_path=db_path, heartbeat_seconds=3600, request_timeout_seconds=30.0)
    store = Store(db_path)
    store.initialize()
    try:
        _, connector_token = store.create_connector("smoke-desktop")
        _, device_token = store.create_device("smoke-phone", approved_by="e2e")
    finally:
        store.close()

    port = free_port()
    relay = Relay(settings, port)
    relay.start()
    print(f"relay listening on {relay.http}")

    try:
        asyncio.run(scenario(relay, connector_token, device_token, db_path))
    except Exception as error:  # noqa: BLE001 - a smoke test reports, it does not raise
        _results.append((False, f"unhandled error: {error!r}"))
        print(f"\n  FAIL  unhandled error: {error!r}")
    finally:
        relay.stop()

    passed = sum(1 for ok, _ in _results if ok)
    failed = [label for ok, label in _results if not ok]
    print(f"\n{'=' * 68}")
    print(f"  {passed}/{len(_results)} checks passed")
    for label in failed:
        print(f"    FAILED: {label}")
    print(f"{'=' * 68}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
