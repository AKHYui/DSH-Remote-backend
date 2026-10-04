"""FastAPI application: plugin attach channel, phone API, and admin surface.

NOTE: this module deliberately does **not** use `from __future__ import
annotations`. FastAPI resolves `Annotated[X, Depends(dep)]` eagerly; the
dependencies below are closures inside `create_app`, so a postponed (string)
annotation could not be evaluated and FastAPI would silently reinterpret the
parameter as a query field.
"""

import asyncio
import json
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import __version__
from . import protocol as P
from .auth import SlidingWindowLimiter, bearer_token, client_key
from .config import Settings
from .relay import RelayError, RelayHub
from .store import Device, Store

logger = logging.getLogger("dsh_relay")

STARTED_AT = time.time()

# protocol error code -> HTTP status
_STATUS_BY_CODE: dict[str, int] = {
    P.ERR_DEVICE_OFFLINE: 503,
    P.ERR_LINK_LOST: 503,
    P.ERR_TIMEOUT: 504,
    P.ERR_CANCELLED: 409,
    P.ERR_OP_NOT_SUPPORTED: 501,
    P.ERR_SESSION_NOT_ALLOWED: 403,
    P.ERR_BAD_ARGS: 400,
    P.ERR_REMOTE_ERROR: 400,
    P.ERR_DEVICE_BUSY: 429,
    P.ERR_GATEWAY_INTERNAL: 500,
}


# --------------------------------------------------------------------------- #
# request / response bodies
# --------------------------------------------------------------------------- #


class PairStartBody(BaseModel):
    deviceName: str = Field(default="", max_length=64)


class PairClaimBody(BaseModel):
    code: str = Field(min_length=4, max_length=12)


class OpBody(BaseModel):
    op: str
    args: dict[str, Any] = Field(default_factory=dict)


class StreamBody(BaseModel):
    op: str
    args: dict[str, Any] = Field(default_factory=dict)


class ApprovalBody(BaseModel):
    decision: Literal["approved", "denied", "cancelled"]
    answers: list[dict[str, Any]] | None = None


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def error_payload(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}


def http_error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def relay_http_error(error: RelayError) -> HTTPException:
    """Map a relay failure onto HTTP. Used where a route needs to translate."""
    return http_error(_STATUS_BY_CODE.get(error.code, 500), error.code, error.message)


def json_text(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def make_sender(ws: WebSocket) -> Callable[[dict[str, Any]], Awaitable[None]]:
    """Serialise every send on one socket.

    Multiple tasks (request handler, heartbeat, `resolve_ask`) push frames into
    the same WebSocket; concurrent `send_text` calls are not safe.
    """
    lock = asyncio.Lock()

    async def send(payload: dict[str, Any]) -> None:
        text = json_text(payload)
        async with lock:
            await ws.send_text(text)

    return send


def sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json_text(data) if isinstance(data, dict) else data}\n\n"


async def _pump_events(
    send: Callable[[dict[str, Any]], Awaitable[None]],
    queue: asyncio.Queue[dict[str, Any]],
) -> None:
    """Drain the hub's per-connection queue onto the socket."""
    try:
        while True:
            frame = await queue.get()
            await send(frame)
    except asyncio.CancelledError:
        raise
    except Exception:  # pragma: no cover - socket already gone
        return


# --------------------------------------------------------------------------- #
# hardening helpers
# --------------------------------------------------------------------------- #


class RedactTokens(logging.Filter):
    """Strip `token=...` out of uvicorn's access log.

    The desktop plugin has to put its connector token in the query string,
    because the WHATWG `WebSocket` constructor cannot set request headers. Left
    alone, that token is written to the journal on every single reconnect — a
    live credential sitting in a log file. This filter is installed on the
    `uvicorn.access` logger so request visibility is kept without the secret.
    """

    _PATTERN = re.compile(r"(\btoken=)[^&\s\"]+")

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 - logging API
        args = record.args
        if isinstance(args, tuple):
            record.args = tuple(
                self._PATTERN.sub(r"\1<redacted>", item) if isinstance(item, str) else item
                for item in args
            )
        elif isinstance(args, dict):
            record.args = {
                key: (self._PATTERN.sub(r"\1<redacted>", value) if isinstance(value, str) else value)
                for key, value in args.items()
            }
        return True


def install_access_log_redaction() -> None:
    """Install `RedactTokens` on every logger uvicorn writes the request line to.

    uvicorn is not consistent here: HTTP access lines come from `uvicorn.access`,
    but the WebSocket "accepted" line comes from `uvicorn.error`. A filter only
    applies to records logged *through* the logger it is attached to, so both
    must carry it — attaching to a parent does not help, because propagation
    does not re-run ancestor logger filters.
    """
    for name in ("uvicorn.access", "uvicorn.error", "uvicorn"):
        target = logging.getLogger(name)
        if not any(isinstance(existing, RedactTokens) for existing in target.filters):
            target.addFilter(RedactTokens())


# --------------------------------------------------------------------------- #
# application factory
# --------------------------------------------------------------------------- #


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or Settings.from_env()
    store = Store(resolved.db_path)
    hub = RelayHub(resolved)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        resolved.ensure_dirs()
        store.initialize()
        if resolved.redact_access_log:
            install_access_log_redaction()
        try:
            yield
        finally:
            await hub.shutdown()
            store.close()

    docs_path = "/docs" if resolved.enable_docs else None
    app = FastAPI(
        title="DSH Remote Bridge",
        description="Relay between the DSH desktop plugin and mobile clients.",
        version=__version__,
        lifespan=lifespan,
        # The schema is a map of the whole relay; it is not published by default.
        docs_url=docs_path,
        redoc_url=None,
        openapi_url="/openapi.json" if resolved.enable_docs else None,
    )
    app.state.settings = resolved
    app.state.store = store
    app.state.hub = hub
    app.state.pair_limiter = SlidingWindowLimiter(resolved.pair_attempts_per_hour, 3600)
    app.state.op_limiter = SlidingWindowLimiter(resolved.op_requests_per_minute, 60)

    # -- hardening middleware ------------------------------------------------

    @app.middleware("http")
    async def limit_request_body(request: Request, call_next: Callable[[Request], Awaitable[Any]]):
        """Refuse oversized bodies before they are buffered.

        The host is a small VM sharing memory with unrelated services; an
        unbounded upload would be an easy way to take it down.
        """
        if request.method in ("POST", "PUT", "PATCH"):
            declared = request.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > resolved.max_request_bytes:
                return JSONResponse(
                    status_code=413,
                    content=error_payload(
                        "payload_too_large",
                        f"request body exceeds {resolved.max_request_bytes} bytes",
                    ),
                )
        return await call_next(request)

    # -- error envelope ------------------------------------------------------

    @app.exception_handler(HTTPException)
    async def _http_exception_handler(_: Request, exc: HTTPException) -> JSONResponse:
        detail = exc.detail
        if isinstance(detail, dict) and "code" in detail:
            error = detail
        else:
            error = {"code": "http_error", "message": str(detail)}
        return JSONResponse(status_code=exc.status_code, content={"ok": False, "error": error})

    @app.exception_handler(RelayError)
    async def _relay_exception_handler(_: Request, exc: RelayError) -> JSONResponse:
        return JSONResponse(
            status_code=_STATUS_BY_CODE.get(exc.code, 500),
            content=error_payload(exc.code, exc.message),
        )

    # -- dependencies --------------------------------------------------------

    async def require_device(request: Request) -> Device:
        token = bearer_token(request)
        device = store.verify_device(token)
        if device is None:
            raise http_error(401, "unauthorized", "missing or invalid device token")
        return device

    def require_known_device(device_id: str) -> None:
        if store.get_desktop(device_id) is None:
            raise http_error(404, "unknown_device", f"desktop {device_id!r} has never connected")

    # -- service surface -----------------------------------------------------

    @app.get("/")
    async def root() -> dict[str, Any]:
        payload: dict[str, Any] = {
            "service": "dsh-remote-bridge",
            "version": __version__,
            "protocol": P.PROTOCOL_VERSION,
            "health": "/healthz",
        }
        if resolved.enable_docs:
            payload["docs"] = "/docs"
        return payload

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        desktops = store.list_desktops()
        return {
            "status": "ok",
            "version": __version__,
            "protocol": P.PROTOCOL_VERSION,
            "uptimeSeconds": int(time.time() - STARTED_AT),
            "devices": {"total": len(desktops), "online": len(hub.online_ids())},
            "phones": len(store.list_devices()),
            "pendingAsks": hub.pending_asks,
            "phoneConnections": hub.sink_count(),
        }

    # -- pairing -------------------------------------------------------------

    @app.post("/api/v1/auth/pair/start")
    async def pair_start(request: Request, body: PairStartBody) -> dict[str, Any]:
        limiter: SlidingWindowLimiter = request.app.state.pair_limiter
        allowed, retry_after = limiter.check(client_key(request, resolved))
        if not allowed:
            raise http_error(429, "rate_limited", f"too many pairing attempts; retry in {retry_after}s")
        name = body.deviceName.strip() or "unnamed phone"
        code, expires_at = store.start_pair(name, resolved.pair_ttl_seconds)
        return {
            "ok": True,
            "value": {"code": code, "expiresAt": expires_at, "ttlSeconds": resolved.pair_ttl_seconds},
        }

    @app.post("/api/v1/auth/pair/claim")
    async def pair_claim(request: Request, body: PairClaimBody) -> dict[str, Any]:
        limiter: SlidingWindowLimiter = request.app.state.pair_limiter
        allowed, retry_after = limiter.check(client_key(request, resolved))
        if not allowed:
            raise http_error(429, "rate_limited", f"too many pairing attempts; retry in {retry_after}s")
        result = store.claim_pair(body.code.strip())
        if result is None:
            raise http_error(
                400,
                "pair_not_ready",
                "code is unknown, expired, already used, or not yet approved by an administrator",
            )
        limiter.reset(client_key(request, resolved))
        device, token = result
        return {
            "ok": True,
            "value": {"deviceToken": token, "deviceId": device.id, "deviceName": device.name},
        }

    # -- devices -------------------------------------------------------------

    @app.get("/api/v1/devices")
    async def list_devices(_: Annotated[Device, Depends(require_device)]) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        for desktop in store.list_desktops():
            link = hub.link(desktop.id)
            online = link is not None and not link.closed
            items.append(
                {
                    "id": desktop.id,
                    "name": desktop.name,
                    "online": online,
                    "platform": desktop.platform,
                    "harnessVersion": desktop.harness_version,
                    "capabilities": sorted(desktop.capabilities),
                    "lastSeen": desktop.last_seen,
                    "pendingRequests": len(link.pending) if online and link else 0,
                    # How long the current link has been up. A link that keeps
                    # resetting every heartbeat interval is the signature of a
                    # keepalive bug, and this is the cheapest way to see it.
                    "connectedAt": int(link.connected_at) if online and link else None,
                    "linkAgeSeconds": int(time.time() - link.connected_at) if online and link else None,
                }
            )
        return {"ok": True, "value": {"items": items, "online": sorted(hub.online_ids())}}

    @app.get("/api/v1/devices/{device_id}/sessions")
    async def list_sessions(
        device_id: str, _: Annotated[Device, Depends(require_device)]
    ) -> dict[str, Any]:
        require_known_device(device_id)
        value = await hub.request(device_id, P.OP_SESSION_LIST, {})
        return {"ok": True, "value": value}

    @app.get("/api/v1/approvals")
    async def list_approvals(
        _: Annotated[Device, Depends(require_device)],
        deviceId: str | None = None,
    ) -> dict[str, Any]:
        """Approvals and questions the desktops are still waiting on.

        The event socket is fire-and-forget, so a phone that was backgrounded or
        disconnected when an ask was raised would otherwise never learn about it
        — the request would simply time out on the desktop. Polling this on
        reconnect (and on resume) closes that hole.
        """
        items = [
            {
                "askId": ask.ask_id,
                "deviceId": ask.device_id,
                "topic": ask.topic,
                "payload": ask.payload,
                "expiresInSeconds": ask.seconds_left,
            }
            for ask in hub.list_asks(deviceId)
        ]
        return {"ok": True, "value": {"items": items}}

    @app.post("/api/v1/devices/{device_id}/op")
    async def run_op(
        request: Request,
        device_id: str,
        body: OpBody,
        device: Annotated[Device, Depends(require_device)],
    ) -> dict[str, Any]:
        require_known_device(device_id)
        if body.op not in P.ALLOWED_OPS:
            raise http_error(501, P.ERR_OP_NOT_SUPPORTED, f"op {body.op!r} is not allowed")
        if body.op in P.STREAM_OPS:
            raise http_error(
                400,
                P.ERR_BAD_ARGS,
                f"op {body.op!r} is streaming; use POST /api/v1/devices/{device_id}/stream",
            )

        limiter: SlidingWindowLimiter = request.app.state.op_limiter
        allowed, retry_after = limiter.check(device.id)
        if not allowed:
            raise http_error(429, "rate_limited", f"too many requests; retry in {retry_after}s")

        started = time.perf_counter()
        try:
            value = await hub.request(device_id, body.op, body.args)
        except RelayError as exc:
            store.record_audit(
                device_id=device_id,
                op=body.op,
                args=body.args,
                ok=False,
                ms=int((time.perf_counter() - started) * 1000),
                error_code=exc.code,
            )
            raise
        store.record_audit(
            device_id=device_id,
            op=body.op,
            args=body.args,
            ok=True,
            ms=int((time.perf_counter() - started) * 1000),
        )
        return {"ok": True, "value": value}

    @app.post("/api/v1/devices/{device_id}/stream")
    async def stream_op(
        request: Request,
        device_id: str,
        body: StreamBody,
        device: Annotated[Device, Depends(require_device)],
    ) -> StreamingResponse:
        require_known_device(device_id)
        if body.op not in P.STREAM_OPS:
            raise http_error(501, P.ERR_OP_NOT_SUPPORTED, f"op {body.op!r} is not a streaming op")

        limiter: SlidingWindowLimiter = request.app.state.op_limiter
        allowed, retry_after = limiter.check(device.id)
        if not allowed:
            raise http_error(429, "rate_limited", f"too many requests; retry in {retry_after}s")

        # Open before returning so offline/busy failures surface as HTTP errors
        # rather than as a 200 with an immediate error event.
        channel = await hub.open_stream(device_id, body.op, body.args)

        async def generator() -> AsyncIterator[str]:
            started = time.perf_counter()
            failed: str | None = None
            try:
                yield sse("open", {"op": body.op, "streamId": channel.req_id})
                async for chunk in channel:
                    yield sse("chunk", chunk)
                if channel.error is not None:
                    failed = channel.error[0]
                    yield sse("error", {"code": channel.error[0], "message": channel.error[1]})
                else:
                    yield sse("end", {"dropped": channel.dropped})
            finally:
                # Safe during GeneratorExit: pops locally, notifies asynchronously.
                link = hub.link(device_id)
                if link is not None:
                    link.cancel_nowait(channel.req_id)
                store.record_audit(
                    device_id=device_id,
                    op=body.op,
                    args=body.args,
                    ok=failed is None,
                    ms=int((time.perf_counter() - started) * 1000),
                    error_code=failed,
                )

        return StreamingResponse(
            generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    # -- approvals -----------------------------------------------------------

    @app.post("/api/v1/approvals/{ask_id}")
    async def resolve_approval(
        ask_id: str,
        body: ApprovalBody,
        device: Annotated[Device, Depends(require_device)],
    ) -> dict[str, Any]:
        del device  # authorization only; the ask id is the capability
        accepted = await hub.resolve_ask(ask_id, body.decision, body.answers)
        if not accepted:
            raise http_error(404, "unknown_ask", "ask is unknown, expired, or its device is offline")
        return {"ok": True, "value": {"askId": ask_id, "decision": body.decision}}

    # -- plugin attach channel ----------------------------------------------

    @app.websocket("/api/v1/attach")
    async def attach(ws: WebSocket) -> None:
        await ws.accept()
        token = bearer_token(ws)
        connector = store.verify_connector(token)
        if connector is None:
            await ws.close(code=P.CLOSE_UNAUTHORIZED, reason="invalid connector token")
            return

        try:
            raw = await ws.receive_text()
        except WebSocketDisconnect:
            return

        try:
            handshake = P.parse_plugin_frame(raw)
        except P.FrameError as exc:
            await ws.send_text(json_text({"t": "bye", "code": exc.code, "message": exc.message}))
            await ws.close(code=P.CLOSE_PROTOCOL, reason="malformed handshake")
            return

        if not isinstance(handshake, P.HelloFrame):
            await ws.send_text(
                json_text({"t": "bye", "code": P.ERR_BAD_ARGS, "message": "first frame must be hello"})
            )
            await ws.close(code=P.CLOSE_PROTOCOL, reason="handshake must be hello")
            return

        device_id = (handshake.deviceId or "").strip()
        if handshake.v != P.PROTOCOL_VERSION or not device_id:
            reason = (
                f"relay speaks protocol v{P.PROTOCOL_VERSION}, plugin speaks v{handshake.v}"
                if handshake.v != P.PROTOCOL_VERSION
                else "hello.deviceId must not be empty"
            )
            await ws.send_text(json_text({"t": "bye", "code": "handshake_rejected", "message": reason}))
            await ws.close(code=P.CLOSE_PROTOCOL, reason="handshake rejected")
            return

        send = make_sender(ws)

        async def close(code: int, reason: str) -> None:
            try:
                await ws.close(code=code, reason=reason)
            except Exception:  # pragma: no cover - already closed
                pass

        link = await hub.attach(device_id, handshake, send, close)
        store.upsert_desktop(
            desktop_id=device_id,
            name=handshake.deviceName or device_id,
            platform=handshake.platform,
            harness_version=str((handshake.harness or {}).get("version") or ""),
            capabilities=handshake.capabilities,
            connector_id=connector.id,
        )
        await send(P.dump_relay_frame(P.WelcomeFrame(deviceId=device_id, serverTime=int(time.time()))))

        stop = asyncio.Event()

        async def heartbeat() -> None:
            interval = max(5, resolved.heartbeat_seconds)
            while not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=interval)
                    return
                except asyncio.TimeoutError:
                    pass
                idle = time.time() - link.last_frame_at
                if idle > interval * 2.5:
                    await send(
                        {
                            "t": "bye",
                            "code": "heartbeat_timeout",
                            "message": f"no frame from plugin for {idle:.0f}s",
                        }
                    )
                    await close(1001, "heartbeat timeout")
                    return
                try:
                    await send(P.dump_relay_frame(P.PingFrame(id=f"p-{uuid.uuid4().hex[:8]}")))
                except Exception:  # pragma: no cover - socket gone
                    return

        beat = asyncio.create_task(heartbeat())
        try:
            while True:
                raw = await ws.receive_text()
                link.last_frame_at = time.time()
                try:
                    inbound = P.parse_plugin_frame(raw)
                except P.FrameError as exc:
                    if exc.code == P.ERR_UNKNOWN_FRAME:
                        # Forward compatibility: a frame type this relay does not
                        # recognise must never tear down the tunnel.
                        logger.warning(
                            "ignoring an unknown frame from %s: %s", device_id, exc.message
                        )
                        continue
                    await send({"t": "bye", "code": exc.code, "message": exc.message})
                    break
                if isinstance(inbound, P.PingFrame):
                    # Either side may ping. The plugin pings on its own schedule
                    # to notice a half-open socket, so this reply is what keeps
                    # its link alive.
                    await send({"t": "pong", "id": inbound.id})
                elif isinstance(inbound, P.ResFrame):
                    link.resolve(inbound)
                elif isinstance(inbound, P.StreamFrame):
                    link.push_stream(inbound)
                elif isinstance(inbound, P.EvtFrame):
                    await hub.dispatch_evt(link, inbound.topic, inbound.payload)
                elif isinstance(inbound, (P.PongFrame, P.HelloFrame)):
                    continue
        except WebSocketDisconnect:
            pass
        except RuntimeError:  # pragma: no cover - closed under us
            pass
        finally:
            stop.set()
            beat.cancel()
            await hub.detach(link)

    # -- phone event channel -------------------------------------------------

    @app.websocket("/api/v1/events")
    async def events(ws: WebSocket) -> None:
        await ws.accept()
        token = bearer_token(ws)
        device = store.verify_device(token)
        if device is None:
            await ws.close(code=P.CLOSE_UNAUTHORIZED, reason="invalid device token")
            return

        phone_id = f"phone-{uuid.uuid4().hex[:10]}"
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=resolved.event_queue_size)
        hub.register_sink(phone_id, queue)
        send = make_sender(ws)
        await send({"t": "ready", "phoneId": phone_id, "protocol": P.PROTOCOL_VERSION})

        pump = asyncio.create_task(_pump_events(send, queue))
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    message = json.loads(raw)
                except ValueError:
                    await send({"t": "error", "id": None, "error": {"code": P.ERR_BAD_ARGS, "message": "not JSON"}})
                    continue
                if not isinstance(message, dict):
                    continue
                kind = message.get("t")
                if kind == "sub":
                    sub_id = str(message.get("id") or "")
                    device_id = str(message.get("deviceId") or "")
                    topics = message.get("topics") or []
                    args = message.get("args") or {}
                    try:
                        await hub.subscribe(
                            phone_id, device_id, sub_id, list(topics), dict(args)
                        )
                    except RelayError as exc:
                        await send({"t": "error", "id": sub_id, "error": {"code": exc.code, "message": exc.message}})
                    else:
                        await send({"t": "ack", "id": sub_id, "ok": True})
                elif kind == "unsub":
                    sub_id = str(message.get("id") or "")
                    await hub.unsubscribe(phone_id, sub_id)
                    await send({"t": "ack", "id": sub_id, "ok": True})
                elif kind == "ping":
                    await send({"t": "pong", "id": message.get("id")})
        except WebSocketDisconnect:
            pass
        except RuntimeError:  # pragma: no cover - closed under us
            pass
        finally:
            pump.cancel()
            await hub.drop_phone(phone_id)

    return app


app = create_app()
