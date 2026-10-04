"""Runtime configuration, resolved from environment variables.

Every value has a working default so `uvicorn app.main:app` runs with zero setup.
The database lives inside the repository (`backend/var/relay.db`) so nothing is
written outside the checkout.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = BACKEND_DIR / "var" / "relay.db"

ENV_PREFIX = "DSH_RELAY_"


def _env_str(name: str, default: str) -> str:
    value = os.environ.get(ENV_PREFIX + name)
    return default if value is None or value == "" else value


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(ENV_PREFIX + name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:  # pragma: no cover - misconfiguration
        raise ValueError(f"{ENV_PREFIX}{name} must be an integer, got {raw!r}") from exc


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(ENV_PREFIX + name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:  # pragma: no cover - misconfiguration
        raise ValueError(f"{ENV_PREFIX}{name} must be a number, got {raw!r}") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(ENV_PREFIX + name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    """Immutable runtime settings."""

    db_path: Path = DEFAULT_DB_PATH
    host: str = "127.0.0.1"
    port: int = 8787
    public_url: str = ""
    log_level: str = "info"

    # Pairing
    pair_ttl_seconds: int = 300

    # Relay
    request_timeout_seconds: float = 60.0
    stream_idle_timeout_seconds: float = 600.0
    approval_ttl_seconds: int = 120
    max_pending_per_device: int = 64
    event_queue_size: int = 512
    heartbeat_seconds: int = 30

    # Rate limits (per key, sliding window)
    pair_attempts_per_hour: int = 20
    op_requests_per_minute: int = 240

    trust_proxy_headers: bool = False

    # Hardening
    #
    # The relay is reachable from the public internet, so the defaults are the
    # secure ones: no interactive API explorer, and a bound on request bodies.
    enable_docs: bool = False
    max_request_bytes: int = 4 * 1024 * 1024
    redact_access_log: bool = True

    # Log every plugin-pushed event and every forwarded subscription. Off by
    # default: session events are chatty. This exists because "the plugin never
    # pushed anything" and "the relay dropped it" look identical from the phone,
    # and the relay is the only side that can tell them apart without restarting
    # DSH.
    debug_events: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            db_path=Path(_env_str("DB", str(DEFAULT_DB_PATH))),
            host=_env_str("HOST", "127.0.0.1"),
            port=_env_int("PORT", 8787),
            public_url=_env_str("PUBLIC_URL", ""),
            log_level=_env_str("LOG_LEVEL", "info"),
            pair_ttl_seconds=_env_int("PAIR_TTL_SECONDS", 300),
            request_timeout_seconds=_env_float("REQUEST_TIMEOUT_SECONDS", 60.0),
            stream_idle_timeout_seconds=_env_float("STREAM_IDLE_TIMEOUT_SECONDS", 600.0),
            approval_ttl_seconds=_env_int("APPROVAL_TTL_SECONDS", 120),
            max_pending_per_device=_env_int("MAX_PENDING_PER_DEVICE", 64),
            event_queue_size=_env_int("EVENT_QUEUE_SIZE", 512),
            heartbeat_seconds=_env_int("HEARTBEAT_SECONDS", 30),
            pair_attempts_per_hour=_env_int("PAIR_ATTEMPTS_PER_HOUR", 20),
            op_requests_per_minute=_env_int("OP_REQUESTS_PER_MINUTE", 240),
            trust_proxy_headers=_env_bool("TRUST_PROXY_HEADERS", False),
            enable_docs=_env_bool("ENABLE_DOCS", False),
            max_request_bytes=_env_int("MAX_REQUEST_BYTES", 4 * 1024 * 1024),
            redact_access_log=_env_bool("REDACT_ACCESS_LOG", True),
            debug_events=_env_bool("DEBUG_EVENTS", False),
        )

    def ensure_dirs(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
