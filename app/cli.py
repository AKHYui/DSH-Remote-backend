"""Administrator CLI.

Run everything through the venv interpreter, e.g.:

    .venv\\Scripts\\python.exe -m app.cli issue-connector --name home-pc
    .venv\\Scripts\\python.exe -m app.cli pair-approve 123456 --name "Pixel 8"

There is deliberately no HTTP admin surface: minting and approving credentials
requires shell access to the relay host.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .config import Settings
from .store import Store

TOKEN_BANNER = (
    "=" * 72
    + "\n  SECRET — shown once, never recoverable. Store it in the desktop plugin\n"
    "  config (or your password manager) before closing this terminal.\n"
    + "=" * 72
)


def _open_store(args: argparse.Namespace) -> tuple[Store, Settings]:
    settings = Settings.from_env()
    if args.db:
        # `db_path` is a Path: `Settings.ensure_dirs()` calls `.parent` on it, so a
        # bare string here used to raise before anything ran.
        settings = Settings(**{**settings.__dict__, "db_path": Path(args.db)})
    settings.ensure_dirs()
    store = Store(settings.db_path)
    store.initialize()
    return store, settings


def _print(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli",
        description=f"DSH Remote Bridge administrator CLI (v{__version__})",
    )
    parser.add_argument("--db", help="override the SQLite path (default: DSH_RELAY_DB)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="create the schema and exit")
    sub.add_parser("devices", help="list paired phone devices")
    sub.add_parser("connectors", help="list desktop connectors")
    sub.add_parser("desktops", help="list DSH hosts that have dialled in")
    sub.add_parser("pending", help="list pairing codes awaiting approval")

    p = sub.add_parser(
        "remove-desktop",
        help="forget DSH host rows (a verification's simulator desktops, say)",
    )
    p.add_argument("desktop_id", nargs="*", help="one or more desktop ids")
    p.add_argument(
        "--simulators",
        action="store_true",
        help="every desktop whose platform is 'simulator', i.e. a test harness rather than a PC",
    )
    p.add_argument("--dry-run", action="store_true", help="show what would be removed")

    p = sub.add_parser("issue-connector", help="mint a token for one desktop plugin")
    p.add_argument("--name", required=True, help="human label, e.g. home-pc")

    p = sub.add_parser("issue-device", help="mint a phone device token without pairing")
    p.add_argument("--name", required=True)

    p = sub.add_parser("pair-start", help="mint a pairing code the phone will claim")
    p.add_argument("--name", default="unnamed phone", help="device label")

    p = sub.add_parser("pair-approve", help="approve a pairing code")
    p.add_argument("code")
    p.add_argument("--by", default="admin")
    p.add_argument("--name", help="override the device label")

    p = sub.add_parser("revoke-device", help="revoke a phone device token")
    p.add_argument("device_id")

    p = sub.add_parser("revoke-connector", help="revoke a desktop connector token")
    p.add_argument("connector_id")

    p = sub.add_parser("audit", help="show recent relay audit rows")
    p.add_argument("--limit", type=int, default=50)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store, settings = _open_store(args)

    try:
        if args.command == "init-db":
            _print({"ok": True, "db": str(settings.db_path)})

        elif args.command == "issue-connector":
            connector, token = store.create_connector(args.name)
            print(TOKEN_BANNER)
            _print({"connectorId": connector.id, "name": connector.name, "token": token})
            print(
                "\nPaste into the plugin config:\n"
                f"  serverUrl: wss://<your-relay-host>:{settings.port}/api/v1/attach\n"
                f"  connectorToken: {token}\n"
                f"  deviceId: {connector.name}\n"
            )

        elif args.command == "issue-device":
            device, token = store.create_device(args.name, approved_by="cli")
            print(TOKEN_BANNER)
            _print({"deviceId": device.id, "name": device.name, "deviceToken": token})

        elif args.command == "pair-start":
            code, expires_at = store.start_pair(args.name, settings.pair_ttl_seconds)
            _print({"code": code, "expiresAt": expires_at, "ttlSeconds": settings.pair_ttl_seconds})
            print(f"\nNow approve it:  python -m app.cli pair-approve {code} --name \"{args.name}\"\n")

        elif args.command == "pair-approve":
            ok, reason = store.approve_pair(args.code, args.by)
            if not ok:
                _print({"ok": False, "reason": reason})
                return 1
            payload: dict[str, Any] = {"ok": True, "reason": reason, "code": args.code}
            if args.name:
                with store._lock:  # local admin tool: update the pending label
                    store._conn.execute(
                        "UPDATE pair_codes SET device_name = ? WHERE code = ?", (args.name, args.code)
                    )
                    store._conn.commit()
                payload["deviceName"] = args.name
            _print(payload)

        elif args.command == "pending":
            _print(store.list_pair_codes())

        elif args.command == "devices":
            _print(
                [
                    {
                        "id": d.id,
                        "name": d.name,
                        "pairedAt": d.paired_at,
                        "lastSeen": d.last_seen,
                        "approvedBy": d.approved_by,
                    }
                    for d in store.list_devices()
                ]
            )

        elif args.command == "connectors":
            _print(
                [
                    {"id": c.id, "name": c.name, "createdAt": c.created_at, "lastSeen": c.last_seen}
                    for c in store.list_connectors()
                ]
            )

        elif args.command == "desktops":
            _print(
                [
                    {
                        "id": d.id,
                        "name": d.name,
                        "platform": d.platform,
                        "harnessVersion": d.harness_version,
                        "connectorId": d.connector_id,
                        "firstSeen": d.first_seen,
                        "lastSeen": d.last_seen,
                    }
                    for d in store.list_desktops()
                ]
            )

        elif args.command == "remove-desktop":
            targets = list(args.desktop_id)
            if args.simulators:
                targets += [d.id for d in store.list_desktops() if d.platform == "simulator"]
            # De-duplicate while keeping the order the operator wrote them in.
            seen: set[str] = set()
            targets = [t for t in targets if not (t in seen or seen.add(t))]
            if not targets:
                _print({"ok": False, "reason": "nothing selected: pass ids or --simulators"})
                return 1
            if args.dry_run:
                _print({"ok": True, "dryRun": True, "wouldRemove": targets})
                return 0
            removed = [t for t in targets if store.delete_desktop(t)]
            _print(
                {
                    "ok": True,
                    "removed": removed,
                    "missing": [t for t in targets if t not in removed],
                }
            )

        elif args.command == "revoke-device":
            _print({"ok": store.revoke_device(args.device_id), "deviceId": args.device_id})

        elif args.command == "revoke-connector":
            _print({"ok": store.revoke_connector(args.connector_id), "connectorId": args.connector_id})

        elif args.command == "audit":
            _print(store.recent_audit(args.limit))

        else:  # pragma: no cover - argparse enforces the choices
            print(f"unknown command {args.command!r}", file=sys.stderr)
            return 2

        return 0
    finally:
        store.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
