"""CLI entry point: ``python -m papertracker <command>``.

Commands: ``poll`` (the hourly job; ``--daily`` adds 1d and 1m history),
``status``, ``build-dashboard`` and ``fetch-fixtures`` (build step 1).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import dashboard, display, metrics, poll
from .client import INTERVALS, FetchResult, PapertradeClient
from .config import DEFAULT_PATH as CONFIG_PATH
from .config import load_config, load_env
from .store import DEFAULT_PATH as DB_PATH
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


def format_status(store: Store, calc: metrics.Calculator, now: datetime | None = None) -> str:
    """The latest headline and diagnostics as a text table."""
    now = now or datetime.now(timezone.utc)
    rows = {g: calc.latest(g) for g in metrics.WINDOWS}
    if rows["1h"] is None:
        return "No closed hours stored yet. Run `python -m papertracker poll` first."
    label_w = max(len(display.LABELS[m][0]) for m in display.LABELS)
    cells = {
        g: {m: display.fmt(m, rows[g].values[m]) for m in display.LABELS} for g in metrics.WINDOWS
    }
    col_w = {g: max(len(display.WINDOW_LABELS[g]), *(len(v) for v in cells[g].values())) for g in metrics.WINDOWS}

    def line(label: str, values: list[str]) -> str:
        return f"  {label:<{label_w}}  " + "  ".join(f"{v:>{col_w[g]}}" for g, v in zip(metrics.WINDOWS, values))

    ts = rows["1h"].ts
    last = store.last_success()
    last_age = (now - datetime.fromisoformat(last)).total_seconds() if last else None
    summary = store.latest_summary()
    out = [
        f"Latest closed hour: {display.utc(ts)} to {display.utc(ts + metrics.HOUR_MS)[11:]}",
        f"Data age: last successful poll {display.age(last_age)}; "
        + (f"summary block {summary['source_block']} at {display.utc(summary['source_ts'])}" if summary else "no summary"),
        "",
        line("", [display.WINDOW_LABELS[g] for g in metrics.WINDOWS]),
    ]
    for title, names in (("Headline", display.HEADLINE), ("Diagnostics", display.DIAGNOSTICS)):
        out.append(title)
        out.extend(line(display.LABELS[m][0], [cells[g][m] for g in metrics.WINDOWS]) for m in names)
    out += ["", display.WINDOW_NOTE]
    flags = sorted({f for r in rows.values() for f in r.flags})
    if flags:
        out += ["", "Assumptions:"] + [f"  - {f}" for f in flags]
    stats = store.attempt_stats()
    if stats["total"]:
        out += ["", f"HTTP attempts: {stats['total']}, 5xx: {stats['server_errors']} "
                    f"({stats['server_errors'] / stats['total']:.1%}), no response: {stats['no_response']}"]
    return "\n".join(out)


def cmd_status(args: argparse.Namespace) -> int:
    with Store(args.db) as store:
        print(format_status(store, metrics.from_store(store, load_config(args.config))))
    return 0


def cmd_build_dashboard(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    with Store(args.db) as store:
        out = dashboard.build(store, metrics.from_store(store, config), config, Path(args.out))
    print(f"wrote {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="papertracker")
    parser.add_argument("-v", "--verbose", action="store_true", help="log every HTTP attempt")
    parser.add_argument("--db", default=str(DB_PATH), help="SQLite database path")
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

    p = sub.add_parser("fetch-fixtures", help="fetch every endpoint once and save the raw JSON (build step 1)")
    p.add_argument("--out", default=str(FIXTURES_DIR), help="directory to write fixtures to")
    p.set_defaults(func=cmd_fetch_fixtures)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(asctime)s %(message)s")
    # httpx logs full request URLs at INFO; the Telegram URL holds the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    load_env()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
