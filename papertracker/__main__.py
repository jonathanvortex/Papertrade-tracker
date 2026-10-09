"""CLI entry point: ``python -m papertracker <command>``.

Commands: ``poll`` (the hourly job; ``--daily`` adds 1d and 1m history),
``status``, ``build-dashboard``, ``serve`` (scheduler plus HTTP dashboard,
for a host like Railway) and ``fetch-fixtures`` (build step 1).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from . import dashboard, display, metrics, poll, serve
from .client import INTERVALS, FetchResult, PapertradeClient
from .config import DEFAULT_PATH as CONFIG_PATH
from .config import data_dir, load_config, load_env
from .store import Store

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures"


def describe(value, path: str = "$", out: list[str] | None = None, max_items: int = 3) -> list[str]:
    """Outline of a JSON value's structure: one line per key path with its type and a sample.

    Makes no assumptions about field names, so it can be used to confirm them.
    """
    out = [] if out is None else out
    if isinstance(value, dict):
        out.append(f"{path}: object, {len(value)} keys")
        for k, v in value.items():
            describe(v, f"{path}.{k}", out, max_items)
    elif isinstance(value, list):
        kinds = sorted({type(v).__name__ for v in value})
        head = ", ".join(json.dumps(v)[:40] for v in value[:max_items])
        tail = f" ... last={json.dumps(value[-1])[:40]}" if len(value) > max_items else ""
        out.append(f"{path}: array[{len(value)}] of {'/'.join(kinds) or '-'}: [{head}{tail}]")
    else:
        out.append(f"{path}: {type(value).__name__} = {json.dumps(value)[:80]}")
    return out


def _attempt_log(result: FetchResult) -> list[dict]:
    return [
        {"attempt": a.number, "fetched_at": a.fetched_at, "status": a.status, "error": a.error}
        for a in result.attempts
    ]


def cmd_fetch_fixtures(args: argparse.Namespace) -> int:
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    targets = [("summary", lambda c: c.summary())] + [
        (f"history_{i}", lambda c, i=i: c.history(i)) for i in INTERVALS
    ]
    log_entries: dict[str, list[dict]] = {}
    failed = []
    with PapertradeClient() as client:
        for name, fetch in targets:
            result = fetch(client)
            log_entries[name] = _attempt_log(result)
            if not result.ok:
                failed.append(name)
                print(f"!! {name}: {result.final.status or result.final.error}", file=sys.stderr)
                continue
            # Save the body byte-for-byte as received; parsing happens later.
            (out_dir / f"{name}.json").write_text(result.body, encoding="utf-8")
            print(f"== {name} ({result.endpoint}, {len(result.attempts)} attempt(s))")
            try:
                print("\n".join(describe(json.loads(result.body))))
            except json.JSONDecodeError as e:
                print(f"   not JSON: {e}")
    (out_dir / "fetch_log.json").write_text(json.dumps(log_entries, indent=2) + "\n", encoding="utf-8")
    statuses = [e["status"] for entries in log_entries.values() for e in entries]
    n_5xx = sum(1 for s in statuses if s is not None and s >= 500)
    print(f"\n{len(statuses)} attempts, {n_5xx} returned 5xx, {statuses.count(None)} had no response")
    return 1 if failed else 0


def cmd_poll(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    with Store(args.db) as store, PapertradeClient() as client:
        report = poll.run(store, client, config, daily=args.daily)
    for endpoint, statuses in report.fetched.items():
        print(f"{endpoint}: {' -> '.join(str(s) for s in statuses)}")
    for change in report.semantics_changes:
        print("column type changed: {} {} -> {}".format(*change))
    for mismatch in report.totals_mismatches:
        print(f"totals mismatch: {mismatch}")
    print(f"{report.metric_rows} metric rows updated, {len(report.alerts)} alert(s)")
    for alert in report.alerts:
        print(f"ALERT {alert.kind}: {alert.message}")
    # Fail only if nothing was fetched; a partial poll still stored what it got.
    return 0 if len(report.failed) < len(report.fetched) else 1


def cmd_status(args: argparse.Namespace) -> int:
    with Store(args.db) as store:
        print(display.format_status(store, metrics.from_store(store, load_config(args.config))))
    return 0


def cmd_build_dashboard(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    with Store(args.db) as store:
        out = dashboard.build(store, metrics.from_store(store, config), config, Path(args.out))
    print(f"wrote {out}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    db = Path(args.db)
    serve.serve(db, db.parent / "site" / "index.html", load_config(args.config),
                host=args.host, port=args.port, minute=args.minute)
    return 0


def main(argv: list[str] | None = None) -> int:
    load_env()  # before parsing, so .env can set PAPERTRACKER_DATA_DIR
    parser = argparse.ArgumentParser(prog="papertracker")
    parser.add_argument("-v", "--verbose", action="store_true", help="log every HTTP attempt")
    parser.add_argument("--db", default=str(data_dir() / "papertracker.sqlite"),
                        help="SQLite database path (default: $PAPERTRACKER_DATA_DIR/papertracker.sqlite)")
    parser.add_argument("--config", default=str(CONFIG_PATH), help="config.yaml path")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("poll", help="fetch, store, compute metrics and send alerts (run hourly)")
    p.add_argument("--daily", action="store_true", help="also fetch 1d and 1m history (run once a day)")
    p.set_defaults(func=cmd_poll)

    p = sub.add_parser("status", help="print the latest headline and diagnostics")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("build-dashboard", help="write the self-contained HTML dashboard")
    p.add_argument("--out", default=str(dashboard.DEFAULT_OUT), help="output file")
    p.set_defaults(func=cmd_build_dashboard)

    p = sub.add_parser("serve", help="poll hourly and serve the dashboard over HTTP (for Railway)")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")), help="default: $PORT or 8080")
    p.add_argument("--minute", type=int, default=2, choices=range(60), metavar="0-59",
                   help="minute past the hour to poll (default 2, just after the hour closes)")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("fetch-fixtures", help="fetch every endpoint once and save the raw JSON (build step 1)")
    p.add_argument("--out", default=str(FIXTURES_DIR), help="directory to write fixtures to")
    p.set_defaults(func=cmd_fetch_fixtures)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(asctime)s %(message)s")
    if args.command == "serve":
        # Hosted logs should show every poll and HTTP attempt (SPEC 2.3).
        logging.getLogger("papertracker").setLevel(logging.INFO)
    # httpx logs full request URLs at INFO; the Telegram URL holds the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
