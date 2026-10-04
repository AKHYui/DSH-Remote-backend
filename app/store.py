"""SQLite persistence: connectors, devices, pair codes, audit log.

Deliberately synchronous: every statement here is a sub-millisecond local write
on a single-worker relay, and keeping it blocking makes the call sites in
`main.py` and `relay.py` obvious. Access is serialised by a re-entrant lock so
the connection is safe to share across the event-loop thread and the
`TestClient` portal thread.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS connectors (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    token_sha256  TEXT NOT NULL,
    created_at    INTEGER NOT NULL,
    last_seen     INTEGER,
    revoked_at    INTEGER
);

CREATE TABLE IF NOT EXISTS devices (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    token_sha256  TEXT NOT NULL,
    created_at    INTEGER NOT NULL,
    paired_at     INTEGER NOT NULL,
    last_seen     INTEGER,
    approved_by   TEXT,
    revoked_at    INTEGER
);

CREATE TABLE IF NOT EXISTS pair_codes (
    code         TEXT PRIMARY KEY,
    device_name  TEXT NOT NULL,
    created_at   INTEGER NOT NULL,
    expires_at   INTEGER NOT NULL,
    approved_at  INTEGER,
    approved_by  TEXT,
    claimed_at   INTEGER,
    device_id    TEXT
);

-- Desktops are DSH hosts that dial in through a connector. They are the "device"
-- the phone API addresses; `devices` above holds the paired phones instead.
CREATE TABLE IF NOT EXISTS desktops (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    platform        TEXT NOT NULL DEFAULT '',
    harness_version TEXT NOT NULL DEFAULT '',
    capabilities    TEXT NOT NULL DEFAULT '[]',
    connector_id    TEXT,
    first_seen      INTEGER NOT NULL,
    last_seen       INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    device_id   TEXT,
    op          TEXT NOT NULL,
    args_digest TEXT,
    ok          INTEGER NOT NULL,
    ms          INTEGER NOT NULL,
    error_code  TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit (ts DESC);
CREATE INDEX IF NOT EXISTS idx_audit_device ON audit (device_id, ts DESC);
"""


def new_token() -> str:
    """A 256-bit URL-safe secret, shown to the operator exactly once."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def digest_args(args: Any) -> str:
    """Stable short digest of call arguments.

    Prompts and file paths flow through `args`, so the audit log stores only a
    digest — never the payload itself.
    """
    try:
        canonical = json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        canonical = repr(args)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class Connector:
    id: str
    name: str
    created_at: int
    last_seen: int | None


@dataclass(frozen=True)
class Device:
    id: str
    name: str
    created_at: int
    paired_at: int
    last_seen: int | None
    approved_by: str | None


@dataclass(frozen=True)
class Desktop:
    """A DSH host reachable through one connector token."""

    id: str
    name: str
    platform: str
    harness_version: str
    capabilities: tuple[str, ...]
    connector_id: str | None
    first_seen: int
    last_seen: int


def _now() -> int:
    return int(time.time())


class Store:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")

    # -- lifecycle -----------------------------------------------------------

    def initialize(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- connectors ----------------------------------------------------------

    def create_connector(self, name: str) -> tuple[Connector, str]:
        token = new_token()
        connector = Connector(id=f"con-{secrets.token_hex(6)}", name=name, created_at=_now(), last_seen=None)
        with self._lock:
            self._conn.execute(
                "INSERT INTO connectors (id, name, token_sha256, created_at) VALUES (?, ?, ?, ?)",
                (connector.id, connector.name, hash_token(token), connector.created_at),
            )
            self._conn.commit()
        return connector, token

    def verify_connector(self, token: str) -> Connector | None:
        if not token:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM connectors WHERE token_sha256 = ? AND revoked_at IS NULL",
                (hash_token(token),),
            ).fetchone()
            if row is None:
                return None
            self._conn.execute("UPDATE connectors SET last_seen = ? WHERE id = ?", (_now(), row["id"]))
            self._conn.commit()
        return Connector(
            id=row["id"], name=row["name"], created_at=row["created_at"], last_seen=row["last_seen"]
        )

    def list_connectors(self) -> list[Connector]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, name, created_at, last_seen FROM connectors WHERE revoked_at IS NULL ORDER BY created_at"
            ).fetchall()
        return [
            Connector(id=r["id"], name=r["name"], created_at=r["created_at"], last_seen=r["last_seen"])
            for r in rows
        ]

    def revoke_connector(self, connector_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE connectors SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                (_now(), connector_id),
            )
            self._conn.commit()
        return cur.rowcount > 0

    # -- devices -------------------------------------------------------------

    def create_device(self, name: str, approved_by: str | None = None) -> tuple[Device, str]:
        token = new_token()
        now = _now()
        device = Device(
            id=f"dev-{secrets.token_hex(6)}",
            name=name,
            created_at=now,
            paired_at=now,
            last_seen=None,
            approved_by=approved_by,
        )
        with self._lock:
            self._conn.execute(
                "INSERT INTO devices (id, name, token_sha256, created_at, paired_at, approved_by)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (device.id, device.name, hash_token(token), now, now, approved_by),
            )
            self._conn.commit()
        return device, token

    def verify_device(self, token: str) -> Device | None:
        if not token:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM devices WHERE token_sha256 = ? AND revoked_at IS NULL",
                (hash_token(token),),
            ).fetchone()
            if row is None:
                return None
            self._conn.execute("UPDATE devices SET last_seen = ? WHERE id = ?", (_now(), row["id"]))
            self._conn.commit()
        return self._device_from_row(row)

    def get_device(self, device_id: str) -> Device | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM devices WHERE id = ? AND revoked_at IS NULL", (device_id,)
            ).fetchone()
        return self._device_from_row(row) if row else None

    def list_devices(self) -> list[Device]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM devices WHERE revoked_at IS NULL ORDER BY paired_at"
            ).fetchall()
        return [self._device_from_row(r) for r in rows]

    def revoke_device(self, device_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE devices SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                (_now(), device_id),
            )
            self._conn.commit()
        return cur.rowcount > 0

    @staticmethod
    def _device_from_row(row: sqlite3.Row) -> Device:
        return Device(
            id=row["id"],
            name=row["name"],
            created_at=row["created_at"],
            paired_at=row["paired_at"],
            last_seen=row["last_seen"],
            approved_by=row["approved_by"],
        )

    # -- desktops ------------------------------------------------------------

    def upsert_desktop(
        self,
        *,
        desktop_id: str,
        name: str,
        platform: str = "",
        harness_version: str = "",
        capabilities: list[str] | tuple[str, ...] = (),
        connector_id: str | None = None,
    ) -> None:
        """Record or refresh one DSH host. Called on every plugin `hello`."""
        now = _now()
        payload = json.dumps(sorted(set(capabilities)), separators=(",", ":"))
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO desktops
                    (id, name, platform, harness_version, capabilities, connector_id, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name            = excluded.name,
                    platform        = excluded.platform,
                    harness_version = excluded.harness_version,
                    capabilities    = excluded.capabilities,
                    connector_id    = excluded.connector_id,
                    last_seen       = excluded.last_seen
                """,
                (desktop_id, name, platform, harness_version, payload, connector_id, now, now),
            )
            self._conn.commit()

    def get_desktop(self, desktop_id: str) -> Desktop | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM desktops WHERE id = ?", (desktop_id,)).fetchone()
        return self._desktop_from_row(row) if row else None

    def list_desktops(self) -> list[Desktop]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM desktops ORDER BY name, id").fetchall()
        return [self._desktop_from_row(row) for row in rows]

    def delete_desktop(self, desktop_id: str) -> bool:
        """Forget one DSH host row outright.

        For rows that should never have been recorded at all — the simulator
        desktops a verification run announces, for instance. The row is exactly
        what the phone's device picker lists, so this is a hard delete rather than
        a tombstone: a tombstoned desktop would reappear as an offline device,
        which is indistinguishable from a real PC that happens to be switched off.
        """
        with self._lock:
            cur = self._conn.execute("DELETE FROM desktops WHERE id = ?", (desktop_id,))
            self._conn.commit()
        return cur.rowcount > 0

    @staticmethod
    def _desktop_from_row(row: sqlite3.Row) -> Desktop:
        try:
            capabilities = tuple(json.loads(row["capabilities"]))
        except (TypeError, ValueError):  # pragma: no cover - only on manual edits
            capabilities = ()
        return Desktop(
            id=row["id"],
            name=row["name"],
            platform=row["platform"],
            harness_version=row["harness_version"],
            capabilities=capabilities,
            connector_id=row["connector_id"],
            first_seen=row["first_seen"],
            last_seen=row["last_seen"],
        )

    # -- pairing -------------------------------------------------------------

    def start_pair(self, device_name: str, ttl_seconds: int) -> tuple[str, int]:
        """Mint a 6-digit pairing code. Retries on the (unlikely) collision."""
        now = _now()
        expires_at = now + ttl_seconds
        with self._lock:
            # Drop codes that can no longer be claimed so 6-digit space stays free.
            self._conn.execute(
                "DELETE FROM pair_codes WHERE expires_at < ? OR claimed_at IS NOT NULL", (now - 3600,)
            )
            for _ in range(32):
                code = f"{secrets.randbelow(1_000_000):06d}"
                exists = self._conn.execute(
                    "SELECT 1 FROM pair_codes WHERE code = ? AND claimed_at IS NULL", (code,)
                ).fetchone()
                if exists:
                    continue
                self._conn.execute(
                    "INSERT INTO pair_codes (code, device_name, created_at, expires_at) VALUES (?, ?, ?, ?)",
                    (code, device_name, now, expires_at),
                )
                self._conn.commit()
                return code, expires_at
        raise RuntimeError("could not allocate a unique pairing code")

    def approve_pair(self, code: str, approved_by: str) -> tuple[bool, str]:
        now = _now()
        with self._lock:
            row = self._conn.execute("SELECT * FROM pair_codes WHERE code = ?", (code,)).fetchone()
            if row is None:
                return False, "unknown_code"
            if row["claimed_at"] is not None:
                return False, "already_claimed"
            if row["expires_at"] < now:
                return False, "expired"
            if row["approved_at"] is not None:
                return True, "already_approved"
            self._conn.execute(
                "UPDATE pair_codes SET approved_at = ?, approved_by = ? WHERE code = ?",
                (now, approved_by, code),
            )
            self._conn.commit()
        return True, "approved"

    def claim_pair(self, code: str) -> tuple[Device, str] | None:
        """Consume an approved, unexpired code and mint the device token."""
        now = _now()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM pair_codes WHERE code = ? AND claimed_at IS NULL", (code,)
            ).fetchone()
            if row is None or row["expires_at"] < now or row["approved_at"] is None:
                return None
            device, token = self.create_device(row["device_name"], approved_by=row["approved_by"])
            self._conn.execute(
                "UPDATE pair_codes SET claimed_at = ?, device_id = ? WHERE code = ?",
                (now, device.id, code),
            )
            self._conn.commit()
        return device, token

    def list_pair_codes(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM pair_codes WHERE claimed_at IS NULL ORDER BY created_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    # -- audit ---------------------------------------------------------------

    def record_audit(
        self,
        *,
        device_id: str | None,
        op: str,
        args: Any,
        ok: bool,
        ms: int,
        error_code: str | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO audit (ts, device_id, op, args_digest, ok, ms, error_code)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (_now(), device_id, op, digest_args(args), 1 if ok else 0, ms, error_code),
            )
            self._conn.commit()

    def recent_audit(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM audit ORDER BY ts DESC, id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]
