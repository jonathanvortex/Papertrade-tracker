"""Alerts (SPEC 6.3), sent to Telegram when ``TELEGRAM_BOT_TOKEN`` and ``TELEGRAM_CHAT_ID`` are set.

Each check keeps what it last saw in the store's ``alert_state`` table, so
re-running a poll doesn't repeat an alert. Every alert is logged in the
``alerts`` table whether or not it was delivered.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Mapping

import httpx

from .metrics import Calculator, summary_from_row

log = logging.getLogger(__name__)

# The summary's PAPER emission settings (SPEC 2.2).
EMISSION_FIELDS = ("paper_cliff", "paper_cap", "paper_rate", "paper_decay")


@dataclass(frozen=True)
class Alert:
    kind: str
    message: str


def check_rewards_started(store, calc: Calculator) -> list[Alert]:
    """Rewards flow turns non-zero for the first time."""
    if store.get_state("rewards_started"):
        return []
    series = calc.series.get("stakingRewards")
    if not series or not any(series.started):
        return []
    i = series.started.index(True)
    store.set_state("rewards_started", str(calc.ts[i]))
    return [Alert("rewards_started", "Staking rewards have started: the rewards series is non-zero for the first time.")]


def check_live_ratio(store, calc: Calculator, threshold_pct: Decimal) -> list[Alert]:
    """The 24 h live ratio crosses the threshold, either way. The first reading only sets the side."""
    row = calc.latest("24h")
    if row is None or not row.values["live_ratio_pct_per_day"].ok:
        return []
    ratio = row.values["live_ratio_pct_per_day"].value
    side = "above" if ratio >= threshold_pct else "below"
    previous = store.get_state("live_ratio_side")
    store.set_state("live_ratio_side", side)
    if previous is None or previous == side:
        return []
    return [Alert("live_ratio", f"Live ratio is now {side} {threshold_pct}%/day: {ratio:.2f}%/day over the last 24 h.")]


def check_emission_settings(store) -> list[Alert]:
    """Any emission setting in the latest summary differs from the last value seen."""
    latest = store.latest_summary()
    if latest is None:
        return []
    values = summary_from_row(latest)
    alerts = []
    for field in EMISSION_FIELDS:
        if field not in values:
            continue
        now = str(values[field])
        before = store.get_state(f"emission:{field}")
        if before != now:
            store.set_state(f"emission:{field}", now)
            if before is not None:
                name = field.removeprefix("paper_")
                alerts.append(Alert("emission_setting", f"Emission setting {name} changed from {before} to {now}."))
    return alerts


def check_semantics(changes: Iterable[tuple[str, str, str]]) -> list[Alert]:
    """A column's detected type (SPEC 4) changed."""
    return [Alert("column_semantics", f"Column {col} now reads as {new} (was {old}).") for col, old, new in changes]


def check_stale(store, stale_after_hours: float, now: datetime | None = None) -> list[Alert]:
    """No successful poll for ``stale_after_hours``. Fires once per outage."""
    last = store.last_success()
    if last is None:
        return []
    now = now or datetime.now(timezone.utc)
    hours = (now - datetime.fromisoformat(last)).total_seconds() / 3600
    if hours < stale_after_hours or store.get_state("stale_alerted_for") == last:
        return []
    store.set_state("stale_alerted_for", last)
    return [Alert("stale", f"No successful poll for {hours:.1f} h (last success {last}).")]


def evaluate(
    store,
    calc: Calculator,
    config: Mapping[str, Any],
    semantics_changes: Iterable[tuple[str, str, str]] = (),
    now: datetime | None = None,
) -> list[Alert]:
    cfg = config.get("alerts", {})
    threshold = Decimal(str(cfg.get("liveRatioThresholdPctPerDay", 1)))
    return [
        *check_rewards_started(store, calc),
        *check_live_ratio(store, calc, threshold),
        *check_emission_settings(store),
        *check_semantics(semantics_changes),
        *check_stale(store, float(cfg.get("staleAfterHours", 3)), now),
    ]


class Notifier:
    """Sends alerts to Telegram if configured; otherwise only logs them."""

    def __init__(self, token: str | None = None, chat_id: str | None = None, *, transport: httpx.BaseTransport | None = None):
        self.token = token if token is not None else os.environ.get("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id if chat_id is not None else os.environ.get("TELEGRAM_CHAT_ID")
        self.transport = transport

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, store, alerts: Iterable[Alert]) -> None:
        for alert in alerts:
            log.warning("ALERT %s: %s", alert.kind, alert.message)
            if not self.configured:
                store.log_alert(alert.kind, alert.message, delivered=False, error="telegram not configured")
                continue
            try:
                with httpx.Client(transport=self.transport, timeout=20) as http:
                    resp = http.post(
                        f"https://api.telegram.org/bot{self.token}/sendMessage",
                        json={"chat_id": self.chat_id, "text": f"PAPER tracker: {alert.message}"},
                    )
                resp.raise_for_status()
                store.log_alert(alert.kind, alert.message, delivered=True)
            except httpx.HTTPError as e:
                # Don't log the URL: it contains the bot token.
                error = f"HTTP {e.response.status_code}" if isinstance(e, httpx.HTTPStatusError) else type(e).__name__
                log.error("telegram delivery failed: %s", error)
                store.log_alert(alert.kind, alert.message, delivered=False, error=error)
