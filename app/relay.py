"""Relay core: device links, request correlation, streams, event fan-out.

One `DeviceLink` per connected desktop plugin. The hub owns the correlation
tables, so `main.py` stays a thin transport layer.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from . import protocol as P
from .config import Settings

# Use a logger name uvicorn already configures. A private name such as
# "dsh_relay.events" is not wired to any handler, so its records are dropped
# silently — which cost a debugging round trip: the absence of output looked
# exactly like the absence of events.
_log = logging.getLogger("uvicorn.error")

SendFrame = Callable[[dict[str, Any]], Awaitable[None]]
CloseLink = Callable[[int, str], Awaitable[None]]

_BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()


class RelayError(Exception):
    """A failure that maps onto a protocol error code."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code


class StreamChannel:
    """One streaming call, surfaced as an async iterator.

    Backpressure policy: when the consumer falls behind, frames are dropped and
    counted rather than blocking the plugin connection. `session.follow` is
    resumable by cursor, so dropping is recoverable; stalling the link is not.

    `finish()` never discards buffered frames: it records the outcome and the
    iterator drains the queue before terminating. When the consumer is idle the
    iterator re-checks the closed flag at most every `_IDLE_POLL_SECONDS`, which
    bounds end-of-stream detection latency without a per-chunk wake-up pair.
    """

    _IDLE_POLL_SECONDS = 0.25

    def __init__(self, req_id: str, op: str, maxsize: int) -> None:
        self.req_id = req_id
        self.op = op
        self._queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=max(1, maxsize))
        self._done = False
        self.error: tuple[str, str] | None = None
        self.dropped = 0

    @property
    def done(self) -> bool:
        return self._done

    def push(self, value: Any) -> None:
        if self._done:
            return
        try:
            self._queue.put_nowait(value)
        except asyncio.QueueFull:
            self.dropped += 1

    def finish(self, error: tuple[str, str] | None = None) -> None:
        if self._done:
            return
        self._done = True
        self.error = error

    async def __aiter__(self) -> AsyncIterator[Any]:
        while True:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                if self._done:
                    return
                try:
                    item = await asyncio.wait_for(
                        self._queue.get(), timeout=self._IDLE_POLL_SECONDS
                    )
                except asyncio.TimeoutError:
                    continue
            yield item


@dataclass
class PendingRequest:
    id: str
    op: str
    created: float
    future: asyncio.Future[Any] | None = None
    stream: StreamChannel | None = None


@dataclass
class Subscription:
    phone_id: str
    device_id: str
    sub_id: str
    topics: set[str]
    args: dict[str, Any]
    plugin_id: str


@dataclass(frozen=True)
class PendingAsk:
    """An approval or question the desktop is still waiting on.

    The whole payload is kept, not just the device, so a phone that reconnects
    can be handed the request again. Without it a question raised while the phone
    was away would be unanswerable and invisible: the relay knows it exists, but
    nothing can render it.
    """

    ask_id: str
    device_id: str
    topic: str
    payload: dict[str, Any]
    expires_at: float

    @property
    def seconds_left(self) -> int:
        return max(0, int(self.expires_at - time.time()))


class DeviceLink:
    """One connected desktop plugin."""

    def __init__(
        self,
        device_id: str,
        hello: P.HelloFrame,
        send: SendFrame,
        close: CloseLink,
        settings: Settings,
    ) -> None:
        self.device_id = device_id
        self.hello = hello
        self.connected_at = time.time()
        self.last_frame_at = self.connected_at
        self.pending: dict[str, PendingRequest] = {}
        self.closed = False
        self._send = send
        self._close = close
        self._settings = settings

    # -- transport -----------------------------------------------------------

    async def send_frame(self, frame: P.RelayFrame) -> None:
        await self._send(P.dump_relay_frame(frame))

    async def safe_send(self, frame: P.RelayFrame) -> None:
        try:
            await self.send_frame(frame)
        except Exception:  # pragma: no cover - link already gone
            pass

    async def close(self, code: int, reason: str) -> None:
        self.closed = True
        try:
            await self._close(code, reason)
        except Exception:  # pragma: no cover - already closed
            pass

    # -- capability ----------------------------------------------------------

    @property
    def capabilities(self) -> set[str]:
        return set(self.hello.capabilities or [])

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities

    # -- request / response --------------------------------------------------

    async def request(self, op: str, args: dict[str, Any], timeout: float) -> Any:
        if self.closed:
            raise RelayError(P.ERR_LINK_LOST, "device link is closed")
        if len(self.pending) >= self._settings.max_pending_per_device:
            raise RelayError("device_busy", "too many in-flight requests for this device")

        req_id = f"r-{uuid.uuid4().hex[:12]}"
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self.pending[req_id] = PendingRequest(id=req_id, op=op, created=time.time(), future=future)

        try:
            await self.send_frame(
                P.ReqFrame(id=req_id, op=op, args=args, deadlineMs=max(1, int(timeout * 1000)))
            )
        except Exception as exc:
            self.pending.pop(req_id, None)
            raise RelayError(P.ERR_LINK_LOST, f"failed to send request: {exc}") from exc

        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            await self.cancel(req_id)
            raise RelayError(P.ERR_TIMEOUT, f"{op} timed out after {timeout:g}s") from None
        finally:
            self.pending.pop(req_id, None)

    async def open_stream(self, op: str, args: dict[str, Any]) -> StreamChannel:
        if self.closed:
            raise RelayError(P.ERR_LINK_LOST, "device link is closed")
        if len(self.pending) >= self._settings.max_pending_per_device:
            raise RelayError("device_busy", "too many in-flight requests for this device")

        req_id = f"s-{uuid.uuid4().hex[:12]}"
        channel = StreamChannel(req_id, op, self._settings.event_queue_size)
        self.pending[req_id] = PendingRequest(id=req_id, op=op, created=time.time(), stream=channel)
        try:
            await self.send_frame(P.ReqFrame(id=req_id, op=op, args=args, deadlineMs=None))
        except Exception as exc:
            self.pending.pop(req_id, None)
            channel.finish((P.ERR_LINK_LOST, str(exc)))
            raise RelayError(P.ERR_LINK_LOST, f"failed to open stream: {exc}") from exc
        return channel

    def cancel_nowait(self, req_id: str) -> None:
        """Cancel a call without awaiting.

        Required by streaming-response finalisers: awaiting inside a generator's
        `finally` while it is being closed raises `RuntimeError`. The peer
        notification is therefore scheduled instead of awaited.
        """
        pending = self.pending.pop(req_id, None)
        if pending is None:
            return
        if pending.stream is not None:
            pending.stream.finish((P.ERR_CANCELLED, "cancelled by relay"))
        if pending.future is not None and not pending.future.done():
            pending.future.cancel()
        if self.closed:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - no loop to notify from
            return
        task = loop.create_task(self.safe_send(P.CancelFrame(id=req_id)))
        _BACKGROUND_TASKS.add(task)
        task.add_done_callback(_BACKGROUND_TASKS.discard)

    async def cancel(self, req_id: str) -> None:
        if req_id in self.pending:
            self.cancel_nowait(req_id)

    def resolve(self, frame: P.ResFrame) -> None:
        pending = self.pending.get(frame.id)
        if pending is None or pending.future is None or pending.future.done():
            return
        if frame.ok:
            pending.future.set_result(frame.value)
            return
        error = frame.error
        code = error.code if error else P.ERR_GATEWAY_INTERNAL
        message = error.message if error else ""
        pending.future.set_exception(RelayError(code, message))

    def push_stream(self, frame: P.StreamFrame) -> None:
        pending = self.pending.get(frame.id)
        if pending is None or pending.stream is None:
            return
        if frame.phase == "open":
            return
        if frame.phase == "chunk":
            pending.stream.push(frame.value)
            return
        self.pending.pop(frame.id, None)
        if frame.phase == "end":
            pending.stream.finish()
            return
        error = frame.error
        pending.stream.finish(
            (error.code if error else P.ERR_GATEWAY_INTERNAL, error.message if error else "")
        )

    def fail_all(self, code: str, message: str) -> None:
        """Fail every in-flight call; used when the link drops."""
        for pending in list(self.pending.values()):
            if pending.stream is not None:
                pending.stream.finish((code, message))
            if pending.future is not None and not pending.future.done():
                pending.future.set_exception(RelayError(code, message))
        self.pending.clear()

    @property
    def info(self) -> dict[str, Any]:
        return {
            "deviceId": self.device_id,
            "deviceName": self.hello.deviceName,
            "platform": self.hello.platform,
            "harness": self.hello.harness,
            "capabilities": sorted(self.capabilities),
            "connectedAt": int(self.connected_at),
            "pending": len(self.pending),
        }


class RelayHub:
    """Owns all device links, subscriptions and pending approvals."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._links: dict[str, DeviceLink] = {}
        # ask_id -> the whole request, not just its device: a phone that was
        # disconnected when the ask was raised still has to be able to render it,
        # and it can only do that from the payload.
        self._asks: dict[str, PendingAsk] = {}
        self._sinks: dict[str, asyncio.Queue[dict[str, Any]]] = {}
        self._subs: dict[tuple[str, str], Subscription] = {}

    # -- devices -------------------------------------------------------------

    def link(self, device_id: str) -> DeviceLink | None:
        return self._links.get(device_id)

    def online_ids(self) -> set[str]:
        return {device_id for device_id, link in self._links.items() if not link.closed}

    def links_info(self) -> list[dict[str, Any]]:
        return [link.info for link in self._links.values()]

    async def attach(
        self,
        device_id: str,
        hello: P.HelloFrame,
        send: SendFrame,
        close: CloseLink,
    ) -> DeviceLink:
        existing = self._links.get(device_id)
        if existing is not None:
            # Last writer wins; the older instance must not fight for the id.
            await existing.close(P.CLOSE_REPLACED, "replaced by a newer connection")
            existing.fail_all(P.ERR_LINK_LOST, "device reconnected")

        link = DeviceLink(device_id, hello, send, close, self._settings)
        self._links[device_id] = link
        await self._broadcast_device_status(device_id, connected=True)
        return link

    async def detach(self, link: DeviceLink) -> None:
        link.closed = True
        link.fail_all(P.ERR_LINK_LOST, "device disconnected")
        current = self._links.get(link.device_id)
        if current is link:
            del self._links[link.device_id]
            await self._broadcast_device_status(link.device_id, connected=False)

    async def shutdown(self) -> None:
        for link in list(self._links.values()):
            await link.close(1001, "relay shutting down")
            link.fail_all(P.ERR_LINK_LOST, "relay shutting down")
        self._links.clear()
        self._sinks.clear()
        self._subs.clear()
        self._asks.clear()

    # -- ops -----------------------------------------------------------------

    async def request(self, device_id: str, op: str, args: dict[str, Any], timeout: float | None = None) -> Any:
        link = self._require_link(device_id)
        return await link.request(op, args, self._settings.request_timeout_seconds if timeout is None else timeout)

    async def open_stream(self, device_id: str, op: str, args: dict[str, Any]) -> StreamChannel:
        link = self._require_link(device_id)
        return await link.open_stream(op, args)

    def _require_link(self, device_id: str) -> DeviceLink:
        link = self._links.get(device_id)
        if link is None or link.closed:
            raise RelayError(P.ERR_DEVICE_OFFLINE, f"device {device_id} is not connected")
        return link

    # -- phone event sinks ---------------------------------------------------

    def register_sink(self, phone_id: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._sinks[phone_id] = queue

    def unregister_sink(self, phone_id: str) -> None:
        self._sinks.pop(phone_id, None)

    def sink_count(self) -> int:
        return len(self._sinks)

    def subscriptions_of(self, phone_id: str) -> list[dict[str, Any]]:
        return [
            {
                "subId": sub.sub_id,
                "deviceId": sub.device_id,
                "topics": sorted(sub.topics),
                "args": sub.args,
            }
            for (owner, _), sub in self._subs.items()
            if owner == phone_id
        ]

    def _emit(self, phone_id: str, frame: dict[str, Any]) -> bool:
        queue = self._sinks.get(phone_id)
        if queue is None:
            return False
        try:
            queue.put_nowait(frame)
            return True
        except asyncio.QueueFull:
            return False

    async def _broadcast_device_status(self, device_id: str, *, connected: bool) -> None:
        frame = {
            "t": "evt",
            "subId": None,
            "deviceId": device_id,
            "topic": P.TOPIC_DEVICE_STATUS,
            "payload": {"deviceId": device_id, "connected": connected},
        }
        for phone_id in list(self._sinks):
            self._emit(phone_id, frame)

    # -- subscriptions -------------------------------------------------------

    async def subscribe(
        self,
        phone_id: str,
        device_id: str,
        sub_id: str,
        topics: list[str],
        args: dict[str, Any] | None = None,
    ) -> None:
        unknown = [topic for topic in topics if topic not in P.KNOWN_TOPICS]
        if unknown:
            raise RelayError(P.ERR_BAD_ARGS, f"unknown topics: {', '.join(sorted(unknown))}")
        if not topics:
            raise RelayError(P.ERR_BAD_ARGS, "topics must not be empty")

        await self.unsubscribe(phone_id, sub_id)

        link = self._require_link(device_id)
        if P.CAP_EVENTS not in link.capabilities:
            raise RelayError(P.ERR_OP_NOT_SUPPORTED, "device does not advertise the events capability")

        plugin_id = f"{phone_id}:{sub_id}"
        args = args or {}
        sub = Subscription(
            phone_id=phone_id,
            device_id=device_id,
            sub_id=sub_id,
            topics=set(topics),
            args=args,
            plugin_id=plugin_id,
        )
        await link.send_frame(P.SubFrame(id=plugin_id, topics=sorted(sub.topics), args=args))
        self._subs[(phone_id, sub_id)] = sub
        if self._settings.debug_events:
            _log.info(
                "sub %s topics=%s forwarded to plugin as %s",
                device_id,
                sorted(sub.topics),
                plugin_id,
            )

    async def unsubscribe(self, phone_id: str, sub_id: str) -> None:
        sub = self._subs.pop((phone_id, sub_id), None)
        if sub is None:
            return
        link = self._links.get(sub.device_id)
        if link is not None and not link.closed:
            await link.safe_send(P.UnsubFrame(id=sub.plugin_id))

    async def drop_phone(self, phone_id: str) -> None:
        for key in [key for key in self._subs if key[0] == phone_id]:
            await self.unsubscribe(*key)
        self.unregister_sink(phone_id)

    async def dispatch_evt(self, link: DeviceLink, topic: str, payload: dict[str, Any]) -> None:
        """Fan one plugin event out to every subscriber that asked for it."""
        if self._settings.debug_events:
            _log.info("evt received from %s topic=%s", link.device_id, topic)

        if topic in (P.TOPIC_APPROVAL_ASK, P.TOPIC_QUESTION_ASK):
            ask_id = payload.get("askId")
            if isinstance(ask_id, str) and ask_id:
                self.register_ask(
                    link.device_id,
                    ask_id,
                    topic,
                    payload,
                    self._settings.approval_ttl_seconds,
                )

        matched = [
            sub
            for sub in self._subs.values()
            if sub.device_id == link.device_id and topic in sub.topics
        ]
        if self._settings.debug_events:
            _log.info(
                "evt %s matched %d subscriber(s) of %d",
                topic,
                len(matched),
                len(self._subs),
            )
        if not matched:
            return
        for sub in matched:
            self._emit(
                sub.phone_id,
                {
                    "t": "evt",
                    "subId": sub.sub_id,
                    "deviceId": link.device_id,
                    "topic": topic,
                    "payload": payload,
                },
            )

    # -- approvals -----------------------------------------------------------

    def register_ask(
        self,
        device_id: str,
        ask_id: str,
        topic: str,
        payload: dict[str, Any],
        ttl_seconds: int,
    ) -> None:
        self._prune_asks()
        self._asks[ask_id] = PendingAsk(
            ask_id=ask_id,
            device_id=device_id,
            topic=topic,
            payload=payload,
            expires_at=time.time() + ttl_seconds,
        )

    def _prune_asks(self) -> None:
        now = time.time()
        for ask_id in [key for key, ask in self._asks.items() if ask.expires_at < now]:
            del self._asks[ask_id]

    def list_asks(self, device_id: str | None = None) -> list[PendingAsk]:
        """Pending asks, oldest first.

        A phone that reconnects calls this so a question raised while it was
        away is not lost: the relay is the only place that still knows about it.
        """
        self._prune_asks()
        asks = [
            ask
            for ask in self._asks.values()
            if device_id is None or ask.device_id == device_id
        ]
        return sorted(asks, key=lambda ask: ask.expires_at)

    def forget_ask(self, ask_id: str) -> None:
        self._asks.pop(ask_id, None)

    @property
    def pending_asks(self) -> int:
        self._prune_asks()
        return len(self._asks)

    async def resolve_ask(
        self,
        ask_id: str,
        decision: str,
        answers: list[dict[str, Any]] | None = None,
    ) -> bool:
        entry = self._asks.pop(ask_id, None)
        if entry is None:
            return False
        if entry.expires_at < time.time():
            return False
        link = self._links.get(entry.device_id)
        if link is None or link.closed:
            return False
        await link.send_frame(
            P.ApprovalFrame(askId=ask_id, decision=decision, answers=answers)  # type: ignore[arg-type]
        )
        return True

    def ask_is_pending(self, ask_id: str) -> bool:
        self._prune_asks()
        return ask_id in self._asks
