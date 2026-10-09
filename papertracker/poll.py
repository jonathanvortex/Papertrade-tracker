"""One polling run (SPEC 3): fetch, store raw, upsert, classify, compute metrics, alert."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

from . import alerts, metrics
from .client import FetchResult, PapertradeClient
from .semantics import check_totals, classify_all
from .store import Store

log = logging.getLogger(__name__)

# Metrics rows recomputed on each poll: the last 8 days covers every row
# whose 7-day window can still change.
RECOMPUTE_HOURS = 8 * 24


@dataclass
class PollReport:
    fetched: dict[str, list[int | None]] = field(default_factory=dict)  # endpoint -> status per attempt
    failed: list[str] = field(default_factory=list)
    semantics_changes: list[tuple[str, str, str]] = field(default_factory=list)
    totals_mismatches: list[str] = field(default_factory=list)
    metric_rows: int = 0
    alerts: list[alerts.Alert] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed


def _payload(result: FetchResult, report: PollReport) -> dict[str, Any] | None:
    report.fetched[result.endpoint] = [a.status for a in result.attempts]
    if not result.ok:
        report.failed.append(result.endpoint)
        return None
    try:
        return json.loads(result.body)
    except json.JSONDecodeError as e:
        log.error("%s: body is not JSON (%s); raw response kept", result.endpoint, e)
        report.failed.append(result.endpoint)
        return None


def run(
    store: Store,
    client: PapertradeClient,
    config: Mapping[str, Any],
    *,
    daily: bool = False,
    notifier: alerts.Notifier | None = None,
    now: datetime | None = None,
) -> PollReport:
    report = PollReport()

    result = client.summary()
    store.record_fetch(result)
    if (payload := _payload(result, report)) is not None:
        store.insert_summary(payload, fetched_at=result.final.fetched_at)

    history_1h = None
    for interval in ("1h", "1d", "1m") if daily else ("1h",):
        result = client.history(interval)
        store.record_fetch(result)
        if (payload := _payload(result, report)) is not None:
            store.upsert_history(interval, payload)
            if interval == "1h":
                history_1h = payload

    _, columns = store.history_series("1h")
    kinds = classify_all(columns)
    report.semantics_changes = store.save_semantics(kinds.values())
    if history_1h is not None:
        for check in check_totals(history_1h, kinds, full_history=True):
            if check.ok is False:
                report.totals_mismatches.append(f"{check.key}={check.total} vs {check.column}={check.series_value}")

    calc = metrics.from_store(store, config)
    last_ts = store.db.execute("SELECT MAX(ts) FROM metrics").fetchone()[0]
    start = 0
    if last_ts is not None:
        cutoff = last_ts - RECOMPUTE_HOURS * metrics.HOUR_MS
        start = next((i for i, t in enumerate(calc.ts) if t >= cutoff), len(calc.ts))
    report.metric_rows = store.upsert_metrics(calc.row(i, g) for i in range(start, len(calc.ts)) for g in metrics.WINDOWS)

    report.alerts = alerts.evaluate(store, calc, config, report.semantics_changes, now)
    (notifier or alerts.Notifier()).send(store, report.alerts)
    return report
