"""HTTP surface tests, driven through the in-process TestClient."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import protocol as P
from app.config import Settings
from app.main import RedactTokens, create_app
from app.store import Store


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------- #
# hardening
# --------------------------------------------------------------------------- #


def test_the_api_schema_is_not_published_by_default(app_client: TestClient):
    assert app_client.get("/docs").status_code == 404
    assert app_client.get("/openapi.json").status_code == 404
    # The service descriptor must not advertise a route that does not exist.
    assert "docs" not in app_client.get("/").json()


def test_the_api_schema_can_be_enabled(tmp_path):
    settings = Settings(db_path=tmp_path / "docs.db", enable_docs=True, heartbeat_seconds=3600)
    with TestClient(create_app(settings)) as client:
        assert client.get("/docs").status_code == 200
        assert client.get("/openapi.json").status_code == 200
        assert client.get("/").json()["docs"] == "/docs"


def test_an_oversized_body_is_refused_before_it_is_buffered(tmp_path, admin: Store):
    settings = Settings(
        db_path=tmp_path / "big.db", max_request_bytes=512, heartbeat_seconds=3600
    )
    seed = Store(settings.db_path)
    seed.initialize()
    try:
        _, device_token = seed.create_device("phone", approved_by="pytest")
    finally:
        seed.close()

    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/v1/devices/whatever/op",
            content=b"x" * 4096,
            headers={**auth(device_token), "Content-Type": "application/json"},
        )
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "payload_too_large"

        # A small body still gets through the middleware to normal handling.
        small = client.post(
            "/api/v1/devices/whatever/op",
            json={"op": "session.list", "args": {}},
            headers=auth(device_token),
        )
        assert small.status_code == 404  # unknown desktop, not 413


def test_the_access_log_never_carries_a_token():
    """Regression: the plugin must pass its token in the query string, so without
    this filter a live credential is written to the journal on every reconnect."""
    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=(
            "1.2.3.4:5678",
            "WebSocket",
            "/api/v1/attach?token=super-secret-value&x=1",
            "1.1",
            101,
        ),
        exc_info=None,
    )
    assert RedactTokens().filter(record) is True
    rendered = record.getMessage()
    assert "super-secret-value" not in rendered
    assert "token=<redacted>" in rendered
    assert "x=1" in rendered, "only the token value is removed"


def test_access_log_redaction_leaves_clean_records_alone():
    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("1.2.3.4:5678", "POST", "/api/v1/approvals/ask-1", "1.1", 200),
        exc_info=None,
    )
    assert RedactTokens().filter(record) is True
    assert record.args[2] == "/api/v1/approvals/ask-1"


def test_redaction_is_installed_on_every_uvicorn_logger_that_logs_paths(tmp_path):
    """uvicorn logs HTTP access lines through `uvicorn.access` but the WebSocket
    "accepted" line through `uvicorn.error`; a filter on only one of them leaves
    the connector token in the journal."""
    from app.main import install_access_log_redaction

    created = ["uvicorn.access", "uvicorn.error"]
    saved = {name: list(logging.getLogger(name).filters) for name in created}
    try:
        for name in created:
            logging.getLogger(name).filters = [
                item for item in logging.getLogger(name).filters if not isinstance(item, RedactTokens)
            ]
        install_access_log_redaction()
        for name in created:
            assert any(
                isinstance(item, RedactTokens) for item in logging.getLogger(name).filters
            ), f"{name} has no redaction filter"
    finally:
        for name, filters in saved.items():
            logging.getLogger(name).filters = filters


def test_root_and_healthz(app_client: TestClient):
    root = app_client.get("/").json()
    assert root["service"] == "dsh-remote-bridge"
    assert root["protocol"] == P.PROTOCOL_VERSION

    health = app_client.get("/healthz")
    assert health.status_code == 200
    body = health.json()
    assert body["status"] == "ok"
    assert body["devices"] == {"total": 0, "online": 0}
    assert body["pendingAsks"] == 0


def test_healthz_needs_no_auth(app_client: TestClient):
    assert app_client.get("/healthz").status_code == 200


def test_protected_routes_require_a_device_token(app_client: TestClient):
    for path in ("/api/v1/devices", "/api/v1/devices/dev-1/sessions"):
        assert app_client.get(path).status_code == 401

    assert app_client.post("/api/v1/devices/dev-1/op", json={"op": "session.list"}).status_code == 401
    assert app_client.post("/api/v1/approvals/ask-1", json={"decision": "approved"}).status_code == 401
    assert (
        app_client.post(
            "/api/v1/devices/dev-1/stream", json={"op": "session.follow", "args": {}}
        ).status_code
        == 401
    )


def test_bad_token_is_rejected_with_the_error_envelope(app_client: TestClient):
    response = app_client.get("/api/v1/devices", headers=auth("nope"))
    assert response.status_code == 401
    body = response.json()
    assert body["ok"] is False and body["error"]["code"] == "unauthorized"


def test_devices_lists_registered_desktops_with_online_state(
    app_client: TestClient, admin: Store, device_token: str
):
    admin.upsert_desktop(
        desktop_id="dev-home",
        name="home-pc",
        platform="win32",
        harness_version="0.2.0-rc.2",
        capabilities=["ops", "events"],
    )
    body = app_client.get("/api/v1/devices", headers=auth(device_token)).json()
    assert body["ok"] is True
    assert body["value"]["online"] == []
    (item,) = body["value"]["items"]
    assert item["id"] == "dev-home"
    assert item["name"] == "home-pc"
    assert item["online"] is False
    assert item["capabilities"] == ["events", "ops"]
    assert item["pendingRequests"] == 0


def test_op_rejects_unknown_op_and_streaming_op(
    app_client: TestClient, admin: Store, device_token: str
):
    admin.upsert_desktop(desktop_id="dev-home", name="home-pc")

    unknown = app_client.post(
        "/api/v1/devices/dev-home/op",
        json={"op": "session.deleteEverything", "args": {}},
        headers=auth(device_token),
    )
    assert unknown.status_code == 501
    assert unknown.json()["error"]["code"] == P.ERR_OP_NOT_SUPPORTED

    streaming = app_client.post(
        "/api/v1/devices/dev-home/op",
        json={"op": "session.follow", "args": {}},
        headers=auth(device_token),
    )
    assert streaming.status_code == 400
    assert streaming.json()["error"]["code"] == P.ERR_BAD_ARGS


def test_op_rejects_unknown_desktop(app_client: TestClient, device_token: str):
    response = app_client.post(
        "/api/v1/devices/dev-ghost/op",
        json={"op": "session.list", "args": {}},
        headers=auth(device_token),
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "unknown_device"


def test_op_reports_offline_device(app_client: TestClient, admin: Store, device_token: str):
    admin.upsert_desktop(desktop_id="dev-home", name="home-pc")
    response = app_client.post(
        "/api/v1/devices/dev-home/op",
        json={"op": "session.list", "args": {}},
        headers=auth(device_token),
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == P.ERR_DEVICE_OFFLINE


def test_stream_route_rejects_non_streaming_op(
    app_client: TestClient, admin: Store, device_token: str
):
    admin.upsert_desktop(desktop_id="dev-home", name="home-pc")
    response = app_client.post(
        "/api/v1/devices/dev-home/stream",
        json={"op": "session.list", "args": {}},
        headers=auth(device_token),
    )
    assert response.status_code == 501
    assert response.json()["error"]["code"] == P.ERR_OP_NOT_SUPPORTED


def test_approval_for_unknown_ask_returns_404(app_client: TestClient, device_token: str):
    response = app_client.post(
        "/api/v1/approvals/ask-ghost", json={"decision": "approved"}, headers=auth(device_token)
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "unknown_ask"


def test_pairing_round_trip(app_client: TestClient, admin: Store):
    started = app_client.post("/api/v1/auth/pair/start", json={"deviceName": "Pixel 8"})
    assert started.status_code == 200
    payload = started.json()["value"]
    code = payload["code"]
    assert len(code) == 6 and payload["ttlSeconds"] == 300

    # Not claimable until an administrator approves it.
    early = app_client.post("/api/v1/auth/pair/claim", json={"code": code})
    assert early.status_code == 400
    assert early.json()["error"]["code"] == "pair_not_ready"

    assert admin.approve_pair(code, "admin")[0] is True

    claimed = app_client.post("/api/v1/auth/pair/claim", json={"code": code})
    assert claimed.status_code == 200
    value = claimed.json()["value"]
    assert value["deviceName"] == "Pixel 8"
    assert value["deviceToken"] and value["deviceId"]

    # The freshly minted token works, and the code is single-use.
    assert app_client.get("/api/v1/devices", headers=auth(value["deviceToken"])).status_code == 200
    assert app_client.post("/api/v1/auth/pair/claim", json={"code": code}).status_code == 400


def test_pair_start_rejects_overlong_device_name(app_client: TestClient):
    response = app_client.post("/api/v1/auth/pair/start", json={"deviceName": "x" * 200})
    assert response.status_code == 422


def test_pairing_is_rate_limited(tmp_path):
    settings = Settings(db_path=tmp_path / "limited.db", pair_attempts_per_hour=2, heartbeat_seconds=3600)
    with TestClient(create_app(settings)) as client:
        assert client.post("/api/v1/auth/pair/start", json={"deviceName": "a"}).status_code == 200
        assert client.post("/api/v1/auth/pair/start", json={"deviceName": "b"}).status_code == 200
        blocked = client.post("/api/v1/auth/pair/start", json={"deviceName": "c"})
        assert blocked.status_code == 429
        assert blocked.json()["error"]["code"] == "rate_limited"


def test_op_requests_are_rate_limited(tmp_path):
    settings = Settings(
        db_path=tmp_path / "op-limited.db", op_requests_per_minute=1, heartbeat_seconds=3600
    )
    # Seed credentials in the *same* database the app will read.
    seed = Store(settings.db_path)
    seed.initialize()
    try:
        _, device_token = seed.create_device("phone", approved_by="pytest")
        seed.upsert_desktop(desktop_id="dev-home", name="home-pc")
    finally:
        seed.close()

    with TestClient(create_app(settings)) as client:
        first = client.post(
            "/api/v1/devices/dev-home/op",
            json={"op": "session.list", "args": {}},
            headers=auth(device_token),
        )
        assert first.status_code == 503  # offline, but the limiter consumed the slot
        second = client.post(
            "/api/v1/devices/dev-home/op",
            json={"op": "session.list", "args": {}},
            headers=auth(device_token),
        )
        assert second.status_code == 429
        assert second.json()["error"]["code"] == "rate_limited"


def test_events_socket_rejects_an_invalid_token(app_client: TestClient):
    ready_seen = False
    try:
        with app_client.websocket_connect("/api/v1/events") as socket:
            socket.receive_text()
            ready_seen = True
    except WebSocketDisconnect as exc:
        assert exc.code == P.CLOSE_UNAUTHORIZED
    assert ready_seen is False


def test_events_socket_sends_ready_then_acks_subscriptions(app_client: TestClient, device_token: str):
    with app_client.websocket_connect(f"/api/v1/events?token={device_token}") as socket:
        ready = socket.receive_json()
        assert ready["t"] == "ready" and ready["phoneId"].startswith("phone-")

        # Subscribing to a device that is not online is refused, not fatal.
        socket.send_json({"t": "sub", "id": "s1", "deviceId": "dev-ghost", "topics": ["session.event"]})
        error = socket.receive_json()
        assert error["t"] == "error" and error["id"] == "s1"
        assert error["error"]["code"] == P.ERR_DEVICE_OFFLINE

        socket.send_json({"t": "unsub", "id": "s1"})
        ack = socket.receive_json()
        assert ack == {"t": "ack", "id": "s1", "ok": True}

        socket.send_json({"t": "ping", "id": "p1"})
        assert socket.receive_json() == {"t": "pong", "id": "p1"}


def test_attach_rejects_a_missing_connector_token(app_client: TestClient):
    try:
        with app_client.websocket_connect("/api/v1/attach") as socket:
            socket.receive_text()
    except WebSocketDisconnect as exc:
        assert exc.code == P.CLOSE_UNAUTHORIZED
    else:  # pragma: no cover - must not accept
        raise AssertionError("attach must reject a missing token")
