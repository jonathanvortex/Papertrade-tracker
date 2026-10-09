"""SQLite storage (SPEC section 3): raw responses, history upserts, summary snapshots.

Every raw response is stored before it is parsed, so parsing bugs can be
fixed and history re-parsed later. History tables are upserted by
timestamp: re-polling overwrites a point instead of adding a row, and the
point whose interval is still open is marked ``is_partial = 1``.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Iterator

from .client import FetchResult

log = logging.getLogger(__name__)

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "data" / "papertracker.sqlite"

HISTORY_INTERVALS = ("1m", "1h", "1d")
# Columns seen in the 2026-10-09 fixtures. A column the API adds later is
# added to the table on first sight.
HISTORY_COLUMNS = (
    "tvl", "stakingRewards", "volume", "volumeBtc", "volumeEth", "traderPnl",
    "users", "liquidation", "trades", "paperSupply", "paperStaked", "paperRevenue",
)
WAD = Decimal(10) ** 18

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _ident(name: str) -> str:
    """Quote a column name taken from API data, refusing anything odd."""
    if not _IDENT.match(name):
        raise ValueError(f"unexpected field name {name!r}")
    return f'"{name}"'


def history_table(interval: str) -> str:
    if interval not in HISTORY_INTERVALS:
        raise ValueError(f"interval must be one of {HISTORY_INTERVALS}, got {interval!r}")
    return f"history_{interval}"


def flatten_summary(payload: dict[str, Any]) -> dict[str, tuple[str, str | None]]:
    """Every summary field as ``{column: (text, decimal)}``.

    Nested keys join with ``_`` (``paper.supply`` → ``paper_supply``). Strings
    of digits are raw 18-decimal integers and are scaled by 1e18 with
    Decimal. Plain JSON ints (``activity.open``) are kept unscaled. Anything
    else has no decimal value. ``source`` is stored in its own columns.
    """
    out: dict[str, tuple[str, str | None]] = {}

    def walk(value: Any, path: list[str]) -> None:
        if isinstance(value, dict):
            for k, v in value.items():
                walk(v, path + [k])
            return
        name = "_".join(path)
        if isinstance(value, bool) or value is None:
            out[name] = (json.dumps(value), None)
        elif isinstance(value, int):
            out[name] = (str(value), str(value))
        elif isinstance(value, str) and re.fullmatch(r"-?\d+", value):
            out[name] = (value, str(Decimal(int(value)) / WAD))
        else:
            out[name] = (value if isinstance(value, str) else json.dumps(value), None)

    walk({k: v for k, v in payload.items() if k != "source"}, [])
    return out


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Store:
    def __init__(self, path: str | Path = DEFAULT_PATH):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self._create()

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _create(self) -> None:
        with self.db:
            self.db.execute(
                """CREATE TABLE IF NOT EXISTS raw_responses (
                    id INTEGER PRIMARY KEY,
                    fetched_at TEXT NOT NULL,
                    endpoint TEXT NOT NULL,
                    http_status INTEGER,
                    attempt INTEGER NOT NULL,
                    body TEXT,
                    error TEXT
                )"""
            )
            cols = ", ".join(f"{_ident(c)} REAL" for c in HISTORY_COLUMNS)
            for interval in HISTORY_INTERVALS:
                self.db.execute(
                    f"""CREATE TABLE IF NOT EXISTS {history_table(interval)} (
                        ts INTEGER PRIMARY KEY,
                        {cols},
                        is_partial INTEGER NOT NULL DEFAULT 0,
                        updated_at TEXT NOT NULL
                    )"""
                )
            self.db.execute(
                """CREATE TABLE IF NOT EXISTS summary_snapshots (
                    id INTEGER PRIMARY KEY,
                    fetched_at TEXT NOT NULL,
                    source_block INTEGER,
                    source_ts INTEGER,
                    source_genesis INTEGER,
                    UNIQUE (source_block, source_ts)
                )"""
            )
            self.db.execute(
                """CREATE TABLE IF NOT EXISTS metrics (
                    ts INTEGER NOT NULL,
                    granularity TEXT NOT NULL,
                    is_partial INTEGER NOT NULL,
                    notes TEXT NOT NULL,
                    flags TEXT NOT NULL,
                    computed_at TEXT NOT NULL,
                    PRIMARY KEY (ts, granularity)
                )"""
            )
            self.db.execute(
                """CREATE TABLE IF NOT EXISTS column_semantics (
                    "column" TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    checked_at TEXT NOT NULL,
                    changed_at TEXT NOT NULL
                )"""
            )

    def _columns(self, table: str) -> set[str]:
        return {row["name"] for row in self.db.execute(f"PRAGMA table_info({table})")}

    def _ensure_columns(self, table: str, names: Iterable[str], sql_type: str) -> None:
        """Add any missing columns, warning only when the table already holds data."""
        existing = self._columns(table)
        has_rows = self.db.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None
        for name in names:
            if name not in existing:
                if has_rows:
                    log.warning("%s: new field %r, adding a column", table, name)
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {_ident(name)} {sql_type}")
                existing.add(name)

    # Raw responses -------------------------------------------------------

    def record_fetch(self, result: FetchResult) -> None:
        """Store every attempt of a fetch, failed ones included."""
        with self.db:
            self.db.executemany(
                "INSERT INTO raw_responses (fetched_at, endpoint, http_status, attempt, body, error) VALUES (?, ?, ?, ?, ?, ?)",
                [(a.fetched_at, result.endpoint, a.status, a.number, a.body, a.error) for a in result.attempts],
            )

    def raw_responses(self, endpoint: str | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM raw_responses"
        args: tuple = ()
        if endpoint:
            sql += " WHERE endpoint = ?"
            args = (endpoint,)
        return self.db.execute(sql + " ORDER BY id", args).fetchall()

    # History -------------------------------------------------------------

    def upsert_history(self, interval: str, payload: dict[str, Any]) -> int:
        """Upsert every point of a ``history`` response. Returns the number of points.

        A point is partial while its interval is open, judged against the
        response's ``source.asOfMs``.
        """
        table = history_table(interval)
        start, step = int(payload["startMs"]), int(payload["intervalMs"])
        columns: dict[str, list] = payload["columns"]
        lengths = {len(v) for v in columns.values()}
        if len(lengths) != 1:
            raise ValueError(f"{table}: columns have different lengths {sorted(lengths)}")
        (n,) = lengths
        as_of = payload.get("source", {}).get("asOfMs")
        names = list(columns)
        now = _utcnow()
        rows = []
        for i in range(n):
            ts = start + i * step
            partial = 1 if as_of is None or ts + step > int(as_of) else 0
            rows.append((ts, *(columns[c][i] for c in names), partial, now))
        cols = ", ".join(_ident(c) for c in names)
        marks = ", ".join("?" * (len(names) + 3))
        updates = ", ".join(f"{_ident(c)} = excluded.{_ident(c)}" for c in names)
        with self.db:
            self._ensure_columns(table, names, "REAL")
            self.db.executemany(
                f"""INSERT INTO {table} (ts, {cols}, is_partial, updated_at) VALUES ({marks})
                    ON CONFLICT (ts) DO UPDATE SET {updates},
                        is_partial = excluded.is_partial, updated_at = excluded.updated_at""",
                rows,
            )
        return n

    def history(self, interval: str) -> list[sqlite3.Row]:
        return self.db.execute(f"SELECT * FROM {history_table(interval)} ORDER BY ts").fetchall()

    def history_series(self, interval: str) -> tuple[list[int], dict[str, list[float | None]]]:
        """Stored history as ``(timestamps, {column: values})``."""
        rows = self.history(interval)
        skip = {"ts", "is_partial", "updated_at"}
        names = [c for c in rows[0].keys() if c not in skip] if rows else []
        return [r["ts"] for r in rows], {c: [r[c] for r in rows] for c in names}

    # Summary -------------------------------------------------------------

    def insert_summary(self, payload: dict[str, Any], fetched_at: str | None = None) -> bool:
        """Store a summary snapshot. Returns False if this snapshot (same block and time) is already stored.

        Each field gets two columns: ``<field>_text`` (as returned) and
        ``<field>`` (the decimal value as text, so no precision is lost).
        """
        source = payload.get("source", {})
        fields = flatten_summary(payload)
        cols = [c for name in fields for c in (name, f"{name}_text")]
        values = [v for text, dec in fields.values() for v in (dec, text)]
        source_vals = [
            int(source["block"]) if source.get("block") is not None else None,
            source.get("at"),
            source.get("genesis"),
        ]
        names = ", ".join(_ident(c) for c in cols)
        marks = ", ".join("?" * (len(cols) + 4))
        with self.db:
            self._ensure_columns("summary_snapshots", cols, "TEXT")
            cur = self.db.execute(
                f"""INSERT OR IGNORE INTO summary_snapshots
                    (fetched_at, source_block, source_ts, source_genesis, {names}) VALUES ({marks})""",
                [fetched_at or _utcnow(), *source_vals, *values],
            )
        return cur.rowcount == 1

    def summaries(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM summary_snapshots ORDER BY source_ts, id").fetchall()

    def latest_summary(self) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM summary_snapshots ORDER BY source_ts DESC, id DESC LIMIT 1").fetchone()

    # Metrics -------------------------------------------------------------

    def upsert_metrics(self, rows: Iterable[Any]) -> int:
        """Upsert ``metrics.MetricRow``s by (ts, granularity).

        Each metric is a column holding its Decimal value as text, NULL when
        n/a; the n/a reasons go in ``notes`` as JSON.
        """
        rows = list(rows)
        if not rows:
            return 0
        names = list(rows[0].values)
        cols = ", ".join(_ident(c) for c in names)
        marks = ", ".join("?" * (len(names) + 6))
        updates = ", ".join(f"{_ident(c)} = excluded.{_ident(c)}" for c in names)
        now = _utcnow()
        data = [
            (
                r.ts, r.granularity, int(r.is_partial),
                json.dumps({k: v.note for k, v in r.values.items() if v.note}, ensure_ascii=False),
                json.dumps(list(r.flags), ensure_ascii=False), now,
                *(None if r.values[c].value is None else str(r.values[c].value) for c in names),
            )
            for r in rows
        ]
        with self.db:
            self._ensure_columns("metrics", names, "TEXT")
            self.db.executemany(
                f"""INSERT INTO metrics (ts, granularity, is_partial, notes, flags, computed_at, {cols}) VALUES ({marks})
                    ON CONFLICT (ts, granularity) DO UPDATE SET {updates}, is_partial = excluded.is_partial,
                        notes = excluded.notes, flags = excluded.flags, computed_at = excluded.computed_at""",
                data,
            )
        return len(rows)

    def metrics(self, granularity: str) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM metrics WHERE granularity = ? ORDER BY ts", (granularity,)).fetchall()

    # Column semantics ----------------------------------------------------

    def save_semantics(self, classifications: Iterable[Any]) -> list[tuple[str, str, str]]:
        """Store each column's classification. Returns ``(column, old, new)`` for every change.

        A column seen for the first time is not a change.
        """
        classifications = list(classifications)
        now = _utcnow()
        old = {r["column"]: r["kind"] for r in self.db.execute('SELECT "column", kind FROM column_semantics')}
        changes = [(c.column, old[c.column], c.kind) for c in classifications if c.column in old and old[c.column] != c.kind]
        with self.db:
            self.db.executemany(
                """INSERT INTO column_semantics ("column", kind, reason, checked_at, changed_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT ("column") DO UPDATE SET
                       changed_at = CASE WHEN kind = excluded.kind THEN changed_at ELSE excluded.checked_at END,
                       kind = excluded.kind, reason = excluded.reason, checked_at = excluded.checked_at""",
                [(c.column, c.kind, c.reason, now, now) for c in classifications],
            )
        for col, before, after in changes:
            log.warning("column %s changed from %s to %s", col, before, after)
        return changes

    def semantics(self) -> dict[str, str]:
        return {r["column"]: r["kind"] for r in self.db.execute('SELECT "column", kind FROM column_semantics')}

    def iter_raw_bodies(self, endpoint: str) -> Iterator[tuple[str, str]]:
        """Successful raw bodies for ``endpoint``, oldest first, for re-parsing."""
        for row in self.db.execute(
            "SELECT fetched_at, body FROM raw_responses WHERE endpoint = ? AND http_status = 200 ORDER BY id", (endpoint,)
        ):
            yield row["fetched_at"], row["body"]
