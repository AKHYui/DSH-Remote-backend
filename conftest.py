"""Shared pytest fixtures.

The relay's `Store` is deliberately synchronous and file-backed, so tests give
each case its own SQLite file and open a *second* `Store` handle to mint tokens
the same way the admin CLI does.
"""

from __future__ import annotations

import socket
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path:  # allow `import app` without installation
    sys.path.insert(0, str(BACKEND_DIR))

from app.config import Settings  # noqa: E402
from app.store import Store  # noqa: E402


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    """Settings isolated to one temp database.

    The heartbeat is pushed far out so the relay's keepalive never interleaves
    with the frames a test is asserting on.
    """
    return Settings(
        db_path=tmp_path / "relay.db",
        heartbeat_seconds=3600,
        request_timeout_seconds=2.0,
        approval_ttl_seconds=30,
        pair_ttl_seconds=300,
    )


@pytest.fixture()
def admin(settings: Settings) -> Iterator[Store]:
    """A second handle on the same database, used to mint credentials."""
    handle = Store(settings.db_path)
    handle.initialize()
    try:
        yield handle
    finally:
        handle.close()


@pytest.fixture()
def app_client(settings: Settings) -> Iterator["object"]:
    """FastAPI TestClient bound to the isolated settings."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app(settings)) as client:
        yield client


@pytest.fixture()
def connector_token(admin: Store) -> str:
    _, token = admin.create_connector("test-desktop")
    return token


@pytest.fixture()
def device_token(admin: Store) -> str:
    _, token = admin.create_device("test-phone", approved_by="pytest")
    return token


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class LiveServer:
    """A real uvicorn server on a loopback port, for end-to-end tests."""

    def __init__(self, app: object, port: int) -> None:
        import uvicorn

        self.port = port
        self.url = f"http://127.0.0.1:{port}"
        self.ws_url = f"ws://127.0.0.1:{port}"
        self._config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on")
        self._server = uvicorn.Server(self._config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def start(self, timeout: float = 20.0) -> None:
        self._thread.start()
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._server.started:
                return
            if not self._thread.is_alive():  # pragma: no cover - startup crash
                raise RuntimeError("uvicorn exited during startup")
            time.sleep(0.05)
        raise TimeoutError("uvicorn did not start in time")

    def stop(self, timeout: float = 10.0) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=timeout)


@pytest.fixture()
def live_server(settings: Settings) -> Iterator[LiveServer]:
    from app.main import create_app

    server = LiveServer(create_app(settings), free_port())
    server.start()
    try:
        yield server
    finally:
        server.stop()
