"""The operator CLI.

`remove-desktop` exists because the relay's `desktops` table is what the phone's
device picker lists, and a verification run announces itself as a desktop like any
real PC. The commands are exercised through `main()` rather than by importing the
store, so argument parsing — including the `--simulators` convenience and
`--dry-run` — is covered too.
"""

from __future__ import annotations

import json

from app import cli
from app.store import Store


def seed(db_path) -> Store:
    store = Store(db_path)
    store.initialize()
    store.upsert_desktop(desktop_id="home-pc", name="home-pc", platform="win32")
    store.upsert_desktop(desktop_id="remote-sim", name="remote-sim (simulator)", platform="simulator")
    store.upsert_desktop(desktop_id="remote-ask", name="remote-ask (simulator)", platform="simulator")
    return store


def test_desktops_lists_what_the_phone_can_see(tmp_path, capsys):
    seed(tmp_path / "relay.db").close()

    assert cli.main(["--db", str(tmp_path / "relay.db"), "desktops"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [row["id"] for row in rows] == ["home-pc", "remote-ask", "remote-sim"]
    assert rows[0]["platform"] == "win32"


def test_remove_desktop_by_id(tmp_path, capsys):
    seed(tmp_path / "relay.db").close()

    assert cli.main(["--db", str(tmp_path / "relay.db"), "remove-desktop", "remote-sim"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"ok": True, "removed": ["remote-sim"], "missing": []}

    store = Store(tmp_path / "relay.db")
    try:
        assert [d.id for d in store.list_desktops()] == ["home-pc", "remote-ask"]
    finally:
        store.close()


def test_remove_desktop_reports_an_unknown_id(tmp_path, capsys):
    seed(tmp_path / "relay.db").close()

    assert cli.main(["--db", str(tmp_path / "relay.db"), "remove-desktop", "typo"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["removed"] == []
    assert payload["missing"] == ["typo"]


def test_remove_desktop_simulators_keeps_real_machines(tmp_path, capsys):
    seed(tmp_path / "relay.db").close()

    code = cli.main(["--db", str(tmp_path / "relay.db"), "remove-desktop", "--simulators"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert sorted(payload["removed"]) == ["remote-ask", "remote-sim"]

    store = Store(tmp_path / "relay.db")
    try:
        assert [d.id for d in store.list_desktops()] == ["home-pc"]
    finally:
        store.close()


def test_remove_desktop_dry_run_changes_nothing(tmp_path, capsys):
    seed(tmp_path / "relay.db").close()

    code = cli.main(["--db", str(tmp_path / "relay.db"), "remove-desktop", "--simulators", "--dry-run"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["dryRun"] is True
    assert sorted(payload["wouldRemove"]) == ["remote-ask", "remote-sim"]

    store = Store(tmp_path / "relay.db")
    try:
        assert len(store.list_desktops()) == 3
    finally:
        store.close()


def test_remove_desktop_with_no_selection_is_an_error(tmp_path, capsys):
    seed(tmp_path / "relay.db").close()

    # Nothing selected must not be read as "remove everything".
    assert cli.main(["--db", str(tmp_path / "relay.db"), "remove-desktop"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False

    store = Store(tmp_path / "relay.db")
    try:
        assert len(store.list_desktops()) == 3
    finally:
        store.close()
