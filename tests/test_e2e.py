"""End-to-end tests against a real uvicorn server, real WebSockets and real HTTP.

These exercise the same code paths the desktop plugin and the phone app use:
the ASGI test client cannot run a plugin socket and a REST call concurrently, so
this module starts an actual server on a loopback port.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx
import pytest
import websockets

from app import protocol as P


@dataclass
class StreamScript:
    """Tells the fake plugin to answer one request with stream frames."""

    chunks: list[Any] = field(default_factory=list)


@dataclass
class NoReply:
    """Tells the fake plugin to answer nothing, to exercise relay timeouts."""


class FakePlugin:
    """A stand-in for the desktop plugin that speaks the v1 wire protocol."""

    def __init__(self, socket: Any) -> None:
        self.socket = socket
        self.handlers: dict[str, Callable[[dict[str, Any]], Any]] = {}
        self.subscriptions: dict[str, dict[str, Any]] = {}
        self.approvals: list[dict[str, Any]] = []
        self.cancels: list[str] = []
        self.pongs: list[str] = []
        self.inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._pump: asyncio.Task[None] | None = None

    # -- lifecycle -----------------------------------------------------------

    def on(self, op: str, handler: Callable[[dict[str, Any]], Any]) -> None:
        self.handlers[op] = handler

    async def hello(
        self,
        device_id: str = "dev-e2e",
        *,
        capabilities: tuple[str, ...] = ("ops", "events", "approvals"),
        version: int = P.PROTOCOL_VERSION,
    ) -> dict[str, Any]:
        await self._send(
            {
                "t": "hello",
                "v": version,
                "deviceId": device_id,
                "deviceName": "fake-desktop",
                "platform": "pytest",
                "harness": {"version": "0.2.0-rc.2", "cwd": "/tmp/ws"},
                "capabilities": list(capabilities),
            }
        )
        return json.loads(await self.socket.recv())

    async def start(self) -> None:
        self._pump = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._pump is not None:
            self._pump.cancel()
            with pytest.raises((asyncio.CancelledError, Exception)):
                await self._pump
            self._pump = None

    async def _run(self) -> None:
        try:
            async for raw in self.socket:
                await self._handle(json.loads(raw))
        except asyncio.CancelledError:
            raise
        except Exception:
            return

    # -- protocol ------------------------------------------------------------

    async def _send(self, payload: dict[str, Any]) -> None:
        await self.socket.send(json.dumps(payload))

    async def _handle(self, frame: dict[str, Any]) -> None:
        kind = frame.get("t")
        if kind == "req":
            await self._handle_request(frame)
        elif kind == "ping":
            await self._send({"t": "pong", "id": frame["id"]})
        elif kind == "sub":
            self.subscriptions[frame["id"]] = frame
        elif kind == "unsub":
            self.subscriptions.pop(frame["id"], None)
        elif kind == "cancel":
            self.cancels.append(frame["id"])
        elif kind == "pong":
            self.pongs.append(frame["id"])
        elif kind == "approval":
            self.approvals.append(frame)
            await self.inbox.put(frame)

    async def _handle_request(self, frame: dict[str, Any]) -> None:
        handler = self.handlers.get(frame["op"])
        if handler is None:
            await self._send(
                {
                    "t": "res",
                    "id": frame["id"],
                    "ok": False,
                    "error": {"code": P.ERR_OP_NOT_SUPPORTED, "message": frame["op"]},
                }
            )
            return
        try:
            result = handler(frame.get("args") or {})
        except Exception as exc:  # pragma: no cover - exercised via bad handlers
            await self._send(
                {
                    "t": "res",
                    "id": frame["id"],
                    "ok": False,
                    "error": {"code": P.ERR_REMOTE_ERROR, "message": str(exc)},
                }
            )
            return
        if isinstance(result, NoReply):
            return
        if isinstance(result, StreamScript):
            await self._send({"t": "stream", "id": frame["id"], "phase": "open"})
            for chunk in result.chunks:
                await self._send({"t": "stream", "id": frame["id"], "phase": "chunk", "value": chunk})
                await asyncio.sleep(0)
            await self._send({"t": "stream", "id": frame["id"], "phase": "end"})
            return
        await self._send({"t": "res", "id": frame["id"], "ok": True, "value": result})

    # -- test helpers --------------------------------------------------------

    async def push(self, topic: str, payload: dict[str, Any]) -> None:
        await self._send({"t": "evt", "topic": topic, "payload": payload})

    async def wait_for_subscription(self, timeout: float = 5.0) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not self.subscriptions:
            if loop.time() > deadline:
                raise AssertionError("plugin never received a sub frame")
            await asyncio.sleep(0.02)
        return next(iter(self.subscriptions.values()))

    async def wait_for_approval(self, timeout: float = 5.0) -> dict[str, Any]:
        return await asyncio.wait_for(self.inbox.get(), timeout)


def attach_url(server: Any, token: str) -> str:
    return f"{server.ws_url}/api/v1/attach?token={token}"


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------- #
# handshake
# --------------------------------------------------------------------------- #


def test_handshake_registers_the_desktop(live_server, admin, connector_token, device_token):
    async def scenario() -> None:
        async with websockets.connect(attach_url(live_server, connector_token)) as socket:
            plugin = FakePlugin(socket)
            welcome = await plugin.hello("dev-e2e")
            assert welcome["t"] == "welcome"
            assert welcome["deviceId"] == "dev-e2e"
            assert welcome["v"] == P.PROTOCOL_VERSION
            await plugin.start()

            async with httpx.AsyncClient(base_url=live_server.url) as http:
                devices = (await http.get("/api/v1/devices", headers=bearer(device_token))).json()
                assert devices["value"]["online"] == ["dev-e2e"]
                (item,) = devices["value"]["items"]
                assert item["name"] == "fake-desktop"
                assert item["platform"] == "pytest"
                assert item["harnessVersion"] == "0.2.0-rc.2"
                assert item["online"] is True

                health = (await http.get("/healthz")).json()
                assert health["devices"] == {"total": 1, "online": 1}

            await plugin.stop()

        # After the plugin disconnects the desktop stays registered but offline.
        async with httpx.AsyncClient(base_url=live_server.url) as http:
            devices = (await http.get("/api/v1/devices", headers=bearer(device_token))).json()
            assert devices["value"]["online"] == []
            assert devices["value"]["items"][0]["online"] is False

    asyncio.run(scenario())


def test_handshake_rejects_a_wrong_protocol_version(live_server, connector_token):
    async def scenario() -> None:
        async with websockets.connect(attach_url(live_server, connector_token)) as socket:
            plugin = FakePlugin(socket)
            bye = await plugin.hello("dev-e2e", version=P.PROTOCOL_VERSION + 1)
            assert bye["t"] == "bye" and bye["code"] == "handshake_rejected"
            assert str(P.PROTOCOL_VERSION) in bye["message"]
            with pytest.raises(websockets.exceptions.ConnectionClosed) as info:
                await socket.recv()
            assert info.value.rcvd is not None and info.value.rcvd.code == P.CLOSE_PROTOCOL

    asyncio.run(scenario())


def test_handshake_rejects_an_empty_device_id(live_server, connector_token):
    async def scenario() -> None:
        async with websockets.connect(attach_url(live_server, connector_token)) as socket:
            plugin = FakePlugin(socket)
            bye = await plugin.hello("")
            assert bye["t"] == "bye" and bye["code"] == "handshake_rejected"

    asyncio.run(scenario())


def test_attach_rejects_a_bogus_connector_token(live_server):
    async def scenario() -> None:
        # The socket is accepted and then closed with 4401 so the plugin can
        # distinguish "bad token" from a network failure and stop reconnecting.
        async with websockets.connect(attach_url(live_server, "not-a-token")) as socket:
            with pytest.raises(websockets.exceptions.ConnectionClosed) as info:
                await socket.recv()
            assert info.value.rcvd is not None
            assert info.value.rcvd.code == P.CLOSE_UNAUTHORIZED

    asyncio.run(scenario())


def test_a_second_connection_replaces_the_first(live_server, connector_token):
    async def scenario() -> None:
        async with websockets.connect(attach_url(live_server, connector_token)) as first_socket:
            first = FakePlugin(first_socket)
            await first.hello("dev-dup")

            async with websockets.connect(attach_url(live_server, connector_token)) as second_socket:
                second = FakePlugin(second_socket)
                welcome = await second.hello("dev-dup")
                assert welcome["t"] == "welcome"

                with pytest.raises(websockets.exceptions.ConnectionClosed) as info:
                    await asyncio.wait_for(first_socket.recv(), timeout=5)
                assert info.value.rcvd is not None
                assert info.value.rcvd.code == P.CLOSE_REPLACED

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# keepalive and forward compatibility
#
# Regression coverage for a bug that only appeared in production: the relay's
# inbound frame union had no `ping`, so the plugin's 30-second heartbeat looked
# like a malformed frame and the relay dropped the link every interval. The test
# suite never caught it because every test finished in well under 30 seconds.
# --------------------------------------------------------------------------- #


def test_plugin_heartbeat_ping_is_answered_and_keeps_the_link(
    live_server, admin, connector_token, device_token
):
    async def scenario() -> None:
        async with websockets.connect(attach_url(live_server, connector_token)) as socket:
            plugin = FakePlugin(socket)
            await plugin.hello("dev-ping")
            await plugin.start()
            plugin.on("session.list", lambda args: {"items": [{"sessionId": "sess-1"}]})

            await plugin._send({"t": "ping", "id": "hb-1"})

            for _ in range(60):
                if plugin.pongs:
                    break
                await asyncio.sleep(0.05)
            assert plugin.pongs == ["hb-1"], "the relay must answer the plugin's ping with pong"

            # The link must still be the same one: a dropped link would make this
            # fail with 503 while the plugin backs off and reconnects.
            async with httpx.AsyncClient(base_url=live_server.url, timeout=10) as http:
                response = await http.post(
                    "/api/v1/devices/dev-ping/op",
                    json={"op": "session.list", "args": {}},
                    headers=bearer(device_token),
                )
                assert response.status_code == 200, response.text
                assert response.json()["value"]["items"][0]["sessionId"] == "sess-1"

            await plugin.stop()

    asyncio.run(scenario())


def test_an_unknown_frame_does_not_drop_the_link(
    live_server, admin, connector_token, device_token
):
    """A frame type this relay does not know is forward compatibility, not a
    protocol violation — it must be ignored, not fatal."""

    async def scenario() -> None:
        async with websockets.connect(attach_url(live_server, connector_token)) as socket:
            plugin = FakePlugin(socket)
            await plugin.hello("dev-future")
            await plugin.start()
            plugin.on("session.list", lambda args: {"items": []})

            await socket.send(json.dumps({"t": "someFutureFrame", "payload": {"v": 2}}))
            # A structurally broken frame is still fatal, but the unknown one
            # above must not have closed the socket.
            await asyncio.sleep(0.2)

            async with httpx.AsyncClient(base_url=live_server.url, timeout=10) as http:
                response = await http.post(
                    "/api/v1/devices/dev-future/op",
                    json={"op": "session.list", "args": {}},
                    headers=bearer(device_token),
                )
                assert response.status_code == 200, response.text

            await plugin.stop()

    asyncio.run(scenario())


def test_a_structurally_broken_frame_still_closes_the_link(live_server, connector_token):
    async def scenario() -> None:
        async with websockets.connect(attach_url(live_server, connector_token)) as socket:
            plugin = FakePlugin(socket)
            # No plugin.start(): this test reads the socket directly, and two
            # concurrent readers on one connection are a websockets error.
            await plugin.hello("dev-broken")

            await socket.send("{not json at all")

            while True:
                raw = await asyncio.wait_for(socket.recv(), timeout=5)
                frame = json.loads(raw)
                if frame.get("t") == "bye":
                    assert frame["code"] == P.ERR_BAD_ARGS
                    break

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# ops
# --------------------------------------------------------------------------- #


def test_op_round_trip(live_server, admin, connector_token, device_token):
    async def scenario() -> None:
        async with websockets.connect(attach_url(live_server, connector_token)) as socket:
            plugin = FakePlugin(socket)
            await plugin.hello("dev-e2e")
            await plugin.start()
            plugin.on("session.list", lambda args: {"items": [{"sessionId": "sess-1"}, {"sessionId": "sess-2"}]})
            plugin.on("session.prompt", lambda args: {"accepted": True})

            async with httpx.AsyncClient(base_url=live_server.url, timeout=10) as http:
                listed = await http.post(
                    "/api/v1/devices/dev-e2e/op",
                    json={"op": "session.list", "args": {}},
                    headers=bearer(device_token),
                )
                assert listed.status_code == 200
                assert [item["sessionId"] for item in listed.json()["value"]["items"]] == [
                    "sess-1",
                    "sess-2",
                ]

                prompted = await http.post(
                    "/api/v1/devices/dev-e2e/op",
                    json={
                        "op": "session.prompt",
                        "args": {"sessionId": "sess-1", "content": [{"type": "text", "text": "hi"}]},
                    },
                    headers=bearer(device_token),
                )
                assert prompted.status_code == 200
                assert prompted.json()["value"] == {"accepted": True}

                # The convenience route proxies session.list too.
                sessions = await http.get(
                    "/api/v1/devices/dev-e2e/sessions", headers=bearer(device_token)
                )
                assert sessions.status_code == 200
                assert len(sessions.json()["value"]["items"]) == 2

            await plugin.stop()

    asyncio.run(scenario())


def test_op_propagates_a_plugin_side_failure(live_server, admin, connector_token, device_token):
    async def scenario() -> None:
        async with websockets.connect(attach_url(live_server, connector_token)) as socket:
            plugin = FakePlugin(socket)
            await plugin.hello("dev-e2e")
            await plugin.start()
            plugin.on("session.page", lambda args: (_ for _ in ()).throw(RuntimeError("boom")))

            async with httpx.AsyncClient(base_url=live_server.url, timeout=10) as http:
                response = await http.post(
                    "/api/v1/devices/dev-e2e/op",
                    json={"op": "session.page", "args": {}},
                    headers=bearer(device_token),
                )
                assert response.status_code == 400
                assert response.json()["error"]["code"] == P.ERR_REMOTE_ERROR
                assert "boom" in response.json()["error"]["message"]

            await plugin.stop()

    asyncio.run(scenario())


def test_op_timeout_is_reported_and_cancelled_on_the_plugin(
    live_server, admin, connector_token, device_token
):
    async def scenario() -> None:
        async with websockets.connect(attach_url(live_server, connector_token)) as socket:
            plugin = FakePlugin(socket)
            await plugin.hello("dev-e2e")
            await plugin.start()
            plugin.on("model.catalog", lambda args: NoReply())

            async with httpx.AsyncClient(base_url=live_server.url, timeout=30) as http:
                response = await http.post(
                    "/api/v1/devices/dev-e2e/op",
                    json={"op": "model.catalog", "args": {}},
                    headers=bearer(device_token),
                )
                assert response.status_code == 504
                assert response.json()["error"]["code"] == P.ERR_TIMEOUT

            # The relay must tell the plugin to stop working on the dead call.
            for _ in range(60):
                if plugin.cancels:
                    break
                await asyncio.sleep(0.05)
            assert plugin.cancels, "relay did not send cancel after a timeout"

            await plugin.stop()

    asyncio.run(scenario())


def test_stream_op_delivers_sse_frames(live_server, admin, connector_token, device_token):
    async def scenario() -> None:
        async with websockets.connect(attach_url(live_server, connector_token)) as socket:
            plugin = FakePlugin(socket)
            await plugin.hello("dev-e2e")
            await plugin.start()
            plugin.on(
                "session.follow",
                lambda args: StreamScript(
                    chunks=[
                        {"type": "snapshot", "cursor": 0},
                        {"type": "event", "event": {"type": "assistant/message", "seq": 1}},
                    ]
                ),
            )

            async with httpx.AsyncClient(base_url=live_server.url, timeout=15) as http:
                async with http.stream(
                    "POST",
                    "/api/v1/devices/dev-e2e/stream",
                    json={"op": "session.follow", "args": {"address": {"kind": "session", "sessionId": "s1"}}},
                    headers=bearer(device_token),
                ) as response:
                    assert response.status_code == 200
                    assert response.headers["content-type"].startswith("text/event-stream")
                    body = ""
                    async for chunk in response.aiter_text():
                        body += chunk
                        if "event: end" in body:
                            break

            assert "event: open" in body
            assert body.count("event: chunk") == 2
            assert '"cursor":0' in body.replace(" ", "")
            assert "event: end" in body

            await plugin.stop()

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# events + approvals
# --------------------------------------------------------------------------- #


def test_events_are_fanned_out_to_phone_subscribers(
    live_server, admin, connector_token, device_token
):
    async def scenario() -> None:
        async with websockets.connect(attach_url(live_server, connector_token)) as socket:
            plugin = FakePlugin(socket)
            await plugin.hello("dev-e2e")
            await plugin.start()

            events_url = f"{live_server.ws_url}/api/v1/events?token={device_token}"
            async with websockets.connect(events_url) as phone:
                ready = json.loads(await phone.recv())
                assert ready["t"] == "ready"

                await phone.send(
                    json.dumps(
                        {
                            "t": "sub",
                            "id": "s1",
                            "deviceId": "dev-e2e",
                            "topics": ["session.event"],
                            "args": {"sessionIds": ["sess-1"]},
                        }
                    )
                )
                assert json.loads(await phone.recv()) == {"t": "ack", "id": "s1", "ok": True}

                sub = await plugin.wait_for_subscription()
                assert sub["id"].endswith(":s1")
                assert sub["topics"] == ["session.event"]
                assert sub["args"] == {"sessionIds": ["sess-1"]}

                await plugin.push(
                    "session.event",
                    {"sessionId": "sess-1", "seq": 7, "type": "assistant/message", "data": {"turn": 1}},
                )
                frame = json.loads(await asyncio.wait_for(phone.recv(), timeout=5))
                assert frame["topic"] == "session.event"
                assert frame["subId"] == "s1"
                assert frame["deviceId"] == "dev-e2e"
                assert frame["payload"]["seq"] == 7

                await phone.send(json.dumps({"t": "unsub", "id": "s1"}))
                assert json.loads(await phone.recv()) == {"t": "ack", "id": "s1", "ok": True}

            await plugin.stop()

    asyncio.run(scenario())


def test_approval_round_trip(live_server, admin, connector_token, device_token):
    async def scenario() -> None:
        async with websockets.connect(attach_url(live_server, connector_token)) as socket:
            plugin = FakePlugin(socket)
            await plugin.hello("dev-e2e")
            await plugin.start()

            async with httpx.AsyncClient(base_url=live_server.url, timeout=10) as http:
                await plugin.push(
                    "approval.ask",
                    {
                        "askId": "ask-1",
                        "sessionId": "sess-1",
                        "toolName": "pwsh",
                        "reason": "run a command",
                        "deadline": 4_000_000_000,
                    },
                )

                # Wait until the relay has registered the ask.
                for _ in range(60):
                    health = (await http.get("/healthz")).json()
                    if health["pendingAsks"] >= 1:
                        break
                    await asyncio.sleep(0.05)
                assert health["pendingAsks"] == 1

                resolved = await http.post(
                    "/api/v1/approvals/ask-1",
                    json={"decision": "approved"},
                    headers=bearer(device_token),
                )
                assert resolved.status_code == 200

                frame = await plugin.wait_for_approval()
                assert frame["t"] == "approval"
                assert frame["askId"] == "ask-1"
                assert frame["decision"] == "approved"

                # Single use.
                again = await http.post(
                    "/api/v1/approvals/ask-1",
                    json={"decision": "denied"},
                    headers=bearer(device_token),
                )
                assert again.status_code == 404

            await plugin.stop()

    asyncio.run(scenario())


def test_question_answers_are_carried_through(live_server, admin, connector_token, device_token):
    async def scenario() -> None:
        async with websockets.connect(attach_url(live_server, connector_token)) as socket:
            plugin = FakePlugin(socket)
            await plugin.hello("dev-e2e")
            await plugin.start()

            async with httpx.AsyncClient(base_url=live_server.url, timeout=10) as http:
                await plugin.push(
                    "question.ask",
                    {
                        "askId": "ask-q1",
                        "sessionId": "sess-1",
                        "questions": [
                            {
                                "id": "q1",
                                "question": "Which database?",
                                "options": [{"label": "sqlite"}, {"label": "postgres"}],
                            }
                        ],
                        "deadline": 4_000_000_000,
                    },
                )
                for _ in range(60):
                    if (await http.get("/healthz")).json()["pendingAsks"] >= 1:
                        break
                    await asyncio.sleep(0.05)

                response = await http.post(
                    "/api/v1/approvals/ask-q1",
                    json={"decision": "approved", "answers": [{"id": "q1", "selected": ["sqlite"]}]},
                    headers=bearer(device_token),
                )
                assert response.status_code == 200

                frame = await plugin.wait_for_approval()
                assert frame["answers"] == [{"id": "q1", "selected": ["sqlite"]}]

            await plugin.stop()

    asyncio.run(scenario())
