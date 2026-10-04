"""Relay core tests: request correlation, streams, subscriptions, approvals."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app import protocol as P
from app.config import Settings
from app.relay import DeviceLink, RelayError, RelayHub, StreamChannel


def run(coro: Any) -> Any:
    return asyncio.run(coro)


class Recorder:
    """Captures the frames a link would have written to its socket."""

    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []
        self.closed: list[tuple[int, str]] = []

    async def send(self, frame: dict[str, Any]) -> None:
        self.frames.append(frame)

    async def close(self, code: int, reason: str) -> None:
        self.closed.append((code, reason))


def hello(device_id: str = "dev-1", caps: tuple[str, ...] = ("ops", "events", "approvals")) -> P.HelloFrame:
    return P.HelloFrame(
        t="hello",
        v=1,
        deviceId=device_id,
        deviceName="test-desktop",
        platform="pytest",
        harness={"version": "test"},
        capabilities=list(caps),
    )


def make_link(
    settings: Settings,
    device_id: str = "dev-1",
    caps: tuple[str, ...] = ("ops", "events", "approvals"),
) -> tuple[DeviceLink, Recorder]:
    recorder = Recorder()
    link = DeviceLink(device_id, hello(device_id, caps), recorder.send, recorder.close, settings)
    return link, recorder


async def wait_frames(recorder: Recorder, count: int, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while len(recorder.frames) < count:
        if loop.time() > deadline:
            raise AssertionError(f"expected >= {count} frames, saw {len(recorder.frames)}")
        await asyncio.sleep(0)


def of_kind(recorder: Recorder, kind: str) -> list[dict[str, Any]]:
    return [frame for frame in recorder.frames if frame["t"] == kind]


# --------------------------------------------------------------------------- #
# DeviceLink: request / response
# --------------------------------------------------------------------------- #


def test_request_resolves_with_value(settings: Settings):
    async def scenario() -> Any:
        link, recorder = make_link(settings)
        task = asyncio.create_task(link.request("session.list", {"cursor": None}, 2.0))
        await wait_frames(recorder, 1)
        request = recorder.frames[-1]
        assert request["t"] == "req" and request["op"] == "session.list"
        assert request["args"] == {"cursor": None}
        assert request["deadlineMs"] == 2000
        link.resolve(P.ResFrame(t="res", id=request["id"], ok=True, value={"items": []}))
        value = await task
        assert link.pending == {}  # correlation entry released
        return value

    assert run(scenario()) == {"items": []}


def test_request_failure_maps_to_relay_error(settings: Settings):
    async def scenario() -> None:
        link, recorder = make_link(settings)
        task = asyncio.create_task(link.request("session.page", {}, 2.0))
        await wait_frames(recorder, 1)
        request = recorder.frames[-1]
        link.resolve(
            P.ResFrame(
                t="res",
                id=request["id"],
                ok=False,
                error=P.ErrorPayload(code="session/not-found", message="gone"),
            )
        )
        with pytest.raises(RelayError) as info:
            await task
        assert info.value.code == "session/not-found"
        assert info.value.message == "gone"

    run(scenario())


def test_request_times_out_and_notifies_peer(settings: Settings):
    async def scenario() -> None:
        link, recorder = make_link(settings)
        with pytest.raises(RelayError) as info:
            await link.request("session.list", {}, 0.05)
        assert info.value.code == P.ERR_TIMEOUT
        await asyncio.sleep(0.05)
        assert of_kind(recorder, "cancel"), "a timed-out request must send cancel"
        assert link.pending == {}

    run(scenario())


def test_request_on_closed_link_is_rejected(settings: Settings):
    async def scenario() -> None:
        link, _ = make_link(settings)
        link.closed = True
        with pytest.raises(RelayError) as info:
            await link.request("session.list", {}, 1.0)
        assert info.value.code == P.ERR_LINK_LOST

    run(scenario())


def test_request_respects_pending_ceiling(settings: Settings):
    async def scenario() -> None:
        capped = Settings(**{**settings.__dict__, "max_pending_per_device": 1})
        link, recorder = make_link(capped)
        first = asyncio.create_task(link.request("session.list", {}, 2.0))
        await wait_frames(recorder, 1)
        with pytest.raises(RelayError) as info:
            await link.request("session.list", {}, 2.0)
        assert info.value.code == "device_busy"
        link.resolve(P.ResFrame(t="res", id=recorder.frames[-1]["id"], ok=True, value=None))
        await first

    run(scenario())


def test_fail_all_releases_every_pending_call(settings: Settings):
    async def scenario() -> None:
        link, recorder = make_link(settings)
        tasks = [
            asyncio.create_task(link.request("session.list", {}, 5.0)),
            asyncio.create_task(link.request("session.page", {}, 5.0)),
        ]
        await wait_frames(recorder, 2)
        link.fail_all(P.ERR_LINK_LOST, "device disconnected")
        results = await asyncio.gather(*tasks, return_exceptions=True)
        codes = sorted(exc.code for exc in results if isinstance(exc, RelayError))
        assert codes == [P.ERR_LINK_LOST, P.ERR_LINK_LOST]
        assert link.pending == {}

    run(scenario())


# --------------------------------------------------------------------------- #
# DeviceLink: streams
# --------------------------------------------------------------------------- #


def test_stream_yields_chunks_then_ends(settings: Settings):
    async def scenario() -> list[Any]:
        link, recorder = make_link(settings)
        channel = await link.open_stream("session.follow", {})
        request = recorder.frames[-1]
        assert request["op"] == "session.follow"
        assert "deadlineMs" not in request  # streams are not deadline-bounded
        link.push_stream(P.StreamFrame(t="stream", id=request["id"], phase="open"))
        link.push_stream(P.StreamFrame(t="stream", id=request["id"], phase="chunk", value={"seq": 1}))
        link.push_stream(P.StreamFrame(t="stream", id=request["id"], phase="chunk", value={"seq": 2}))
        link.push_stream(P.StreamFrame(t="stream", id=request["id"], phase="end"))
        collected = [chunk async for chunk in channel]
        assert link.pending == {}
        return collected

    assert run(scenario()) == [{"seq": 1}, {"seq": 2}]


def test_stream_surfaces_error_frame(settings: Settings):
    async def scenario() -> Any:
        link, recorder = make_link(settings)
        channel = await link.open_stream("session.follow", {})
        request = recorder.frames[-1]
        link.push_stream(P.StreamFrame(t="stream", id=request["id"], phase="chunk", value=1))
        link.push_stream(
            P.StreamFrame(
                t="stream",
                id=request["id"],
                phase="error",
                error=P.ErrorPayload(code="session/not-found", message="gone"),
            )
        )
        collected = [chunk async for chunk in channel]
        return collected, channel.error

    collected, error = run(scenario())
    assert collected == [1]
    assert error == ("session/not-found", "gone")


def test_stream_channel_drops_instead_of_blocking():
    channel = StreamChannel("s-1", "session.follow", maxsize=1)
    channel.push(1)
    channel.push(2)
    channel.push(3)
    assert channel.dropped == 2
    channel.finish()

    async def drain() -> list[Any]:
        return [item async for item in channel]

    assert run(drain()) == [1]


def test_finish_preserves_chunks_already_buffered():
    channel = StreamChannel("s-2", "session.follow", maxsize=4)
    channel.push("a")
    channel.push("b")
    channel.finish()

    async def drain() -> list[Any]:
        return [item async for item in channel]

    assert run(drain()) == ["a", "b"]


# --------------------------------------------------------------------------- #
# RelayHub
# --------------------------------------------------------------------------- #


def test_attach_replaces_existing_link_with_close_code(settings: Settings):
    async def scenario() -> Any:
        hub = RelayHub(settings)
        first, first_rec = make_link(settings)
        first = await hub.attach("dev-1", first.hello, first_rec.send, first_rec.close)

        second_rec = Recorder()
        second = await hub.attach("dev-1", hello("dev-1"), second_rec.send, second_rec.close)
        return hub, first, first_rec, second

    hub, first, first_rec, second = run(scenario())
    assert first_rec.closed == [(P.CLOSE_REPLACED, "replaced by a newer connection")]
    assert hub.link("dev-1") is second


def test_detaching_a_replaced_link_does_not_evict_the_new_one(settings: Settings):
    async def scenario() -> Any:
        hub = RelayHub(settings)
        recorder_a, recorder_b = Recorder(), Recorder()
        link_a = await hub.attach("dev-1", hello("dev-1"), recorder_a.send, recorder_a.close)
        link_b = await hub.attach("dev-1", hello("dev-1"), recorder_b.send, recorder_b.close)
        await hub.detach(link_a)
        return hub, link_b

    hub, link_b = run(scenario())
    assert hub.link("dev-1") is link_b
    assert hub.online_ids() == {"dev-1"}


def test_detach_fails_pending_and_clears_online(settings: Settings):
    async def scenario() -> Any:
        hub = RelayHub(settings)
        recorder = Recorder()
        link = await hub.attach("dev-1", hello("dev-1"), recorder.send, recorder.close)
        task = asyncio.create_task(hub.request("dev-1", "session.list", {}))
        await wait_frames(recorder, 1)
        await hub.detach(link)
        with pytest.raises(RelayError) as info:
            await task
        return hub, info.value.code

    hub, code = run(scenario())
    assert code == P.ERR_LINK_LOST
    assert hub.online_ids() == set()


def test_request_requires_an_online_device(settings: Settings):
    async def scenario() -> None:
        hub = RelayHub(settings)
        with pytest.raises(RelayError) as info:
            await hub.request("dev-offline", "session.list", {})
        assert info.value.code == P.ERR_DEVICE_OFFLINE

    run(scenario())


def test_subscribe_rejects_unknown_topics_and_offline_devices(settings: Settings):
    async def scenario() -> None:
        hub = RelayHub(settings)
        recorder = Recorder()
        await hub.attach("dev-1", hello("dev-1"), recorder.send, recorder.close)

        with pytest.raises(RelayError) as unknown:
            await hub.subscribe("phone-1", "dev-1", "s1", ["not.a.topic"])
        assert unknown.value.code == P.ERR_BAD_ARGS

        with pytest.raises(RelayError) as empty:
            await hub.subscribe("phone-1", "dev-1", "s1", [])
        assert empty.value.code == P.ERR_BAD_ARGS

        with pytest.raises(RelayError) as offline:
            await hub.subscribe("phone-1", "dev-elsewhere", "s1", ["session.event"])
        assert offline.value.code == P.ERR_DEVICE_OFFLINE

    run(scenario())


def test_subscribe_requires_the_events_capability(settings: Settings):
    async def scenario() -> None:
        hub = RelayHub(settings)
        recorder = Recorder()
        await hub.attach("dev-1", hello("dev-1", caps=("ops",)), recorder.send, recorder.close)
        with pytest.raises(RelayError) as info:
            await hub.subscribe("phone-1", "dev-1", "s1", ["session.event"])
        assert info.value.code == P.ERR_OP_NOT_SUPPORTED

    run(scenario())


def test_subscribe_sends_a_namespaced_plugin_subscription(settings: Settings):
    async def scenario() -> Any:
        hub = RelayHub(settings)
        recorder = Recorder()
        await hub.attach("dev-1", hello("dev-1"), recorder.send, recorder.close)
        await hub.subscribe("phone-1", "dev-1", "s1", ["session.event"], {"sessionIds": ["a"]})
        await hub.subscribe("phone-2", "dev-1", "s1", ["session.status"])
        return recorder

    recorder = run(scenario())
    subs = of_kind(recorder, "sub")
    # Same caller-supplied sub id from two phones must not collide on the wire.
    assert sorted(frame["id"] for frame in subs) == ["phone-1:s1", "phone-2:s1"]
    assert subs[0]["topics"] == ["session.event"]
    assert subs[0]["args"] == {"sessionIds": ["a"]}


def test_unsubscribe_and_drop_phone_notify_the_plugin(settings: Settings):
    async def scenario() -> Any:
        hub = RelayHub(settings)
        recorder = Recorder()
        await hub.attach("dev-1", hello("dev-1"), recorder.send, recorder.close)
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        hub.register_sink("phone-1", queue)
        await hub.subscribe("phone-1", "dev-1", "s1", ["session.event"])
        await hub.drop_phone("phone-1")
        return hub, recorder

    hub, recorder = run(scenario())
    assert of_kind(recorder, "unsub")[0]["id"] == "phone-1:s1"
    assert hub.sink_count() == 0
    assert hub.subscriptions_of("phone-1") == []


def test_dispatch_evt_fans_out_only_to_matching_subscribers(settings: Settings):
    async def scenario() -> tuple[Any, Any, Any]:
        hub = RelayHub(settings)
        recorder = Recorder()
        link = await hub.attach("dev-1", hello("dev-1"), recorder.send, recorder.close)
        events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        statuses: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        hub.register_sink("phone-1", events)
        hub.register_sink("phone-2", statuses)
        await hub.subscribe("phone-1", "dev-1", "s1", ["session.event"])
        await hub.subscribe("phone-2", "dev-1", "s2", ["session.status"])

        await hub.dispatch_evt(link, "session.event", {"sessionId": "a", "seq": 1})
        return events, statuses, hub

    events, statuses, _ = run(scenario())
    assert events.qsize() == 1
    assert statuses.qsize() == 0
    frame = events.get_nowait()
    assert frame["topic"] == "session.event" and frame["subId"] == "s1" and frame["deviceId"] == "dev-1"


def test_device_status_is_broadcast_to_every_phone(settings: Settings):
    async def scenario() -> asyncio.Queue[dict[str, Any]]:
        hub = RelayHub(settings)
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        hub.register_sink("phone-1", queue)
        recorder = Recorder()
        await hub.attach("dev-1", hello("dev-1"), recorder.send, recorder.close)
        return queue

    queue = run(scenario())
    frame = queue.get_nowait()
    assert frame["topic"] == P.TOPIC_DEVICE_STATUS
    assert frame["payload"] == {"deviceId": "dev-1", "connected": True}
    assert frame["subId"] is None  # device-level, not subscription-scoped


def test_dispatch_registers_ask_and_resolve_sends_approval(settings: Settings):
    async def scenario() -> Any:
        hub = RelayHub(settings)
        recorder = Recorder()
        link = await hub.attach("dev-1", hello("dev-1"), recorder.send, recorder.close)

        await hub.dispatch_evt(link, "approval.ask", {"askId": "ask-1", "toolName": "pwsh"})
        assert hub.ask_is_pending("ask-1") is True

        accepted = await hub.resolve_ask("ask-1", "approved")
        second = await hub.resolve_ask("ask-1", "approved")
        unknown = await hub.resolve_ask("ask-nope", "denied")
        return recorder, accepted, second, unknown

    recorder, accepted, second, unknown = run(scenario())
    assert accepted is True
    assert second is False  # an ask is single-use
    assert unknown is False
    approvals = of_kind(recorder, "approval")
    assert approvals[0]["askId"] == "ask-1" and approvals[0]["decision"] == "approved"


def test_resolve_ask_fails_when_the_device_is_offline(settings: Settings):
    async def scenario() -> Any:
        hub = RelayHub(settings)
        recorder = Recorder()
        link = await hub.attach("dev-1", hello("dev-1"), recorder.send, recorder.close)
        await hub.dispatch_evt(link, "approval.ask", {"askId": "ask-1"})
        await hub.detach(link)
        return await hub.resolve_ask("ask-1", "approved")

    assert run(scenario()) is False


def test_expired_asks_are_pruned(settings: Settings):
    async def scenario() -> Any:
        hub = RelayHub(settings)
        recorder = Recorder()
        link = await hub.attach("dev-1", hello("dev-1"), recorder.send, recorder.close)
        hub.register_ask(
            "dev-1",
            "ask-old",
            P.TOPIC_APPROVAL_ASK,
            {"askId": "ask-old", "sessionId": "s1"},
            ttl_seconds=-1,
        )
        assert hub.pending_asks == 0
        assert await hub.resolve_ask("ask-old", "approved") is False

    run(scenario())


def test_pending_asks_keep_their_payload(settings: Settings):
    """A phone that reconnects must be able to *render* the request it missed.

    Storing only the device and expiry was not enough: the relay could say an ask
    existed but could not hand over the questions, so the request was effectively
    lost for any phone that was offline when it was raised.
    """

    async def scenario() -> Any:
        hub = RelayHub(settings)
        recorder = Recorder()
        await hub.attach("dev-1", hello("dev-1"), recorder.send, recorder.close)
        payload = {
            "askId": "ask-1",
            "sessionId": "s1",
            "questions": [{"id": "q1", "question": "Which database?"}],
        }
        hub.register_ask("dev-1", "ask-1", P.TOPIC_QUESTION_ASK, payload, ttl_seconds=300)

        asks = hub.list_asks()
        assert len(asks) == 1
        assert asks[0].ask_id == "ask-1"
        assert asks[0].topic == P.TOPIC_QUESTION_ASK
        assert asks[0].payload == payload
        assert asks[0].seconds_left > 0

        # Filtering by device, and forgetting on resolution.
        assert hub.list_asks("dev-other") == []
        assert hub.list_asks("dev-1") == asks
        hub.forget_ask("ask-1")
        assert hub.list_asks() == []

    run(scenario())


def test_dispatched_asks_are_remembered(settings: Settings):
    """The dispatch path is what populates the reconnect list."""

    async def scenario() -> Any:
        hub = RelayHub(settings)
        recorder = Recorder()
        link = await hub.attach("dev-1", hello("dev-1"), recorder.send, recorder.close)
        await hub.dispatch_evt(
            link,
            P.TOPIC_APPROVAL_ASK,
            {"askId": "ask-9", "sessionId": "s1", "toolName": "pwsh"},
        )
        asks = hub.list_asks()
        assert [ask.ask_id for ask in asks] == ["ask-9"]
        assert asks[0].payload["toolName"] == "pwsh"

        # Non-ask topics must not be tracked.
        await hub.dispatch_evt(link, P.TOPIC_SESSION_STATUS, {"sessionId": "s1"})
        assert len(hub.list_asks()) == 1

    run(scenario())


def test_dispatch_evt_ignores_unsubscribed_topics(settings: Settings):
    async def scenario() -> asyncio.Queue[dict[str, Any]]:
        hub = RelayHub(settings)
        recorder = Recorder()
        link = await hub.attach("dev-1", hello("dev-1"), recorder.send, recorder.close)
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        hub.register_sink("phone-1", queue)
        await hub.dispatch_evt(link, "session.event", {"seq": 1})
        return queue

    assert run(scenario()).qsize() == 0


def test_event_sink_overflow_is_dropped_not_raised(settings: Settings):
    async def scenario() -> asyncio.Queue[dict[str, Any]]:
        hub = RelayHub(settings)
        recorder = Recorder()
        link = await hub.attach("dev-1", hello("dev-1"), recorder.send, recorder.close)
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1)
        hub.register_sink("phone-1", queue)
        await hub.subscribe("phone-1", "dev-1", "s1", ["session.event"])
        for seq in range(5):
            await hub.dispatch_evt(link, "session.event", {"seq": seq})
        return queue

    assert run(scenario()).qsize() == 1


def test_shutdown_closes_links_and_releases_everything(settings: Settings):
    async def scenario() -> Any:
        hub = RelayHub(settings)
        recorder = Recorder()
        link = await hub.attach("dev-1", hello("dev-1"), recorder.send, recorder.close)
        task = asyncio.create_task(hub.request("dev-1", "session.list", {}))
        await wait_frames(recorder, 1)
        await hub.shutdown()
        with pytest.raises(RelayError):
            await task
        return hub, recorder

    hub, recorder = run(scenario())
    assert recorder.closed and recorder.closed[0][0] == 1001
    assert hub.online_ids() == set()
    assert hub.sink_count() == 0
