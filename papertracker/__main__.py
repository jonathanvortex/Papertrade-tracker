"""CLI entry point: ``python -m papertracker <command>``.

Only ``fetch-fixtures`` (build step 1) exists so far; ``poll``, ``status`` and
``build-dashboard`` come with later build steps.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .client import INTERVALS, FetchResult, PapertradeClient

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="papertracker")
    parser.add_argument("-v", "--verbose", action="store_true", help="log every HTTP attempt")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("fetch-fixtures", help="fetch every endpoint once and save the raw JSON (build step 1)")
    p.add_argument("--out", default=str(FIXTURES_DIR), help="directory to write fixtures to")
    p.set_defaults(func=cmd_fetch_fixtures)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(asctime)s %(message)s")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
