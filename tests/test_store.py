"""Persistence tests: credential lifecycle, pairing, audit privacy."""

from __future__ import annotations

import json

from app.store import Store, digest_args


def test_connector_lifecycle(admin: Store):
    connector, token = admin.create_connector("home-pc")
    assert connector.name == "home-pc"
    assert len(token) >= 32

    verified = admin.verify_connector(token)
    assert verified is not None and verified.id == connector.id

    assert admin.verify_connector("wrong-token") is None
    assert admin.verify_connector("") is None

    assert admin.revoke_connector(connector.id) is True
    assert admin.verify_connector(token) is None
    assert admin.revoke_connector(connector.id) is False  # already revoked
    assert admin.list_connectors() == []


def test_device_lifecycle(admin: Store):
    device, token = admin.create_device("pixel", approved_by="admin")
    assert admin.verify_device(token) is not None
    assert admin.get_device(device.id) is not None

    assert admin.revoke_device(device.id) is True
    assert admin.verify_device(token) is None
    assert admin.get_device(device.id) is None
    assert admin.list_devices() == []


def test_tokens_are_not_stored_in_plaintext(admin: Store):
    _, token = admin.create_device("pixel")
    with admin._lock:
        row = admin._conn.execute("SELECT token_sha256 FROM devices").fetchone()
    assert row["token_sha256"] != token
    assert len(row["token_sha256"]) == 64


def test_pair_requires_admin_approval(admin: Store):
    code, expires_at = admin.start_pair("my phone", ttl_seconds=300)
    assert len(code) == 6 and code.isdigit() and expires_at > 0

    # Not approvable-claimable until an admin approves.
    assert admin.claim_pair(code) is None

    ok, reason = admin.approve_pair(code, "admin")
    assert ok and reason == "approved"

    claimed = admin.claim_pair(code)
    assert claimed is not None
    device, token = claimed
    assert device.name == "my phone"
    assert admin.verify_device(token) is not None

    # Single use: claiming twice fails.
    assert admin.claim_pair(code) is None


def test_pair_rejects_unknown_expired_and_reused_codes(admin: Store):
    assert admin.approve_pair("000000", "admin") == (False, "unknown_code")

    code, _ = admin.start_pair("stale", ttl_seconds=300)
    # Force expiry rather than sleeping.
    with admin._lock:
        admin._conn.execute("UPDATE pair_codes SET expires_at = 1 WHERE code = ?", (code,))
        admin._conn.commit()
    assert admin.approve_pair(code, "admin") == (False, "expired")
    assert admin.claim_pair(code) is None


def test_pair_codes_are_distinct(admin: Store):
    codes = {admin.start_pair(f"phone-{i}", ttl_seconds=300)[0] for i in range(25)}
    assert len(codes) == 25


def test_approve_is_idempotent(admin: Store):
    code, _ = admin.start_pair("my phone", ttl_seconds=300)
    assert admin.approve_pair(code, "admin") == (True, "approved")
    assert admin.approve_pair(code, "admin") == (True, "already_approved")


def test_desktop_upsert_is_idempotent_and_merges(admin: Store):
    admin.upsert_desktop(
        desktop_id="dev-1",
        name="home-pc",
        platform="win32",
        harness_version="0.2.0-rc.2",
        capabilities=["ops"],
        connector_id="con-1",
    )
    admin.upsert_desktop(
        desktop_id="dev-1",
        name="home-pc-renamed",
        platform="win32",
        harness_version="0.2.1",
        capabilities=["ops", "events", "events"],
        connector_id="con-1",
    )
    desktops = admin.list_desktops()
    assert len(desktops) == 1
    desktop = desktops[0]
    assert desktop.name == "home-pc-renamed"
    assert desktop.harness_version == "0.2.1"
    assert desktop.capabilities == ("events", "ops")  # deduped and sorted
    assert desktop.first_seen <= desktop.last_seen


def test_desktop_delete_is_hard_and_reports_whether_it_existed(admin: Store):
    """A removed desktop must not linger as an offline row.

    The phone's device picker reads this table, so a tombstone would show up as a
    permanently-offline device — indistinguishable from a real PC that is simply
    switched off, which is exactly the "ghost device" the delete exists to remove.
    """
    admin.upsert_desktop(desktop_id="sim-1", name="remote-sim (simulator)", platform="simulator")
    admin.upsert_desktop(desktop_id="dev-1", name="home-pc", platform="win32")

    assert admin.delete_desktop("sim-1") is True
    assert admin.delete_desktop("sim-1") is False  # already gone
    assert [d.id for d in admin.list_desktops()] == ["dev-1"]


def test_audit_stores_only_a_digest(admin: Store):
    secret = "my very secret prompt"
    admin.record_audit(
        device_id="dev-1",
        op="session.prompt",
        args={"content": [{"type": "text", "text": secret}]},
        ok=True,
        ms=7,
    )
    rows = admin.recent_audit()
    assert len(rows) == 1
    row = rows[0]
    assert row["op"] == "session.prompt"
    assert row["ok"] == 1
    assert secret not in json.dumps(row)
    assert row["args_digest"] == digest_args({"content": [{"type": "text", "text": secret}]})


def test_digest_is_stable_regardless_of_key_order():
    assert digest_args({"a": 1, "b": 2}) == digest_args({"b": 2, "a": 1})
    assert digest_args({"a": 1}) != digest_args({"a": 2})


def test_store_rejects_use_before_initialize(tmp_path):
    handle = Store(tmp_path / "never-initialized.db")
    try:
        handle.list_desktops()
    except Exception as exc:  # sqlite3.OperationalError
        assert "desktops" in str(exc) or "no such table" in str(exc)
    else:  # pragma: no cover - schema must not exist yet
        raise AssertionError("expected a failure before initialize()")
