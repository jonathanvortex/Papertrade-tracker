"""Cumulative / per-interval / level detection (SPEC section 4).

Columns are classified from the 1h series. ``history.totals`` can't decide
the type (different key names, mixed units, current values rather than
sums), so it is only used as a cross-check through an explicit key map.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Sequence

log = logging.getLogger(__name__)

LEVEL = "level"
CUMULATIVE = "cumulative"
PER_INTERVAL = "per_interval"
UNKNOWN = "unknown"

LEVELS = frozenset({"tvl", "users", "paperSupply", "paperStaked"})

# totals key -> (column, decimals). rewardsRaw -> stakingRewards and the
# units of every all-zero key are unconfirmed as of the 2026-10-09 fixtures.
TOTALS_MAP: dict[str, tuple[str, int]] = {
    "tvlRaw": ("tvl", 18),
    "rewardsRaw": ("stakingRewards", 18),
    "volumeRaw": ("volume", 18),
    "liquidationRaw": ("liquidation", 18),
    "tradesRaw": ("trades", 0),
    "usersRaw": ("users", 0),
}
TOLERANCE = Decimal("0.001")


@dataclass(frozen=True)
class Classification:
    column: str
    kind: str
    reason: str


def classify(column: str, values: Sequence[float | int | None]) -> Classification:
    if column in LEVELS:
        return Classification(column, LEVEL, "known level")
    vals = [v for v in values if v is not None]
    if not any(vals):
        return Classification(column, UNKNOWN, "all zeros")
    if all(b >= a for a, b in zip(vals, vals[1:])):
        return Classification(column, CUMULATIVE, "never decreases")
    log.warning("%s decreases: treating it as per-interval", column)
    return Classification(column, PER_INTERVAL, "decreases")


def classify_all(columns: dict[str, Sequence[float | int | None]]) -> dict[str, Classification]:
    return {name: classify(name, values) for name, values in columns.items()}


@dataclass(frozen=True)
class TotalsCheck:
    key: str
    column: str | None
    total: Decimal
    series_value: Decimal | None
    ok: bool | None  # None when there is nothing to compare against
    note: str = ""


def _close(a: Decimal, b: Decimal) -> bool:
    if a == b:
        return True
    return abs(a - b) <= TOLERANCE * max(abs(a), abs(b))


def check_totals(payload: dict[str, Any], kinds: dict[str, Classification], *, full_history: bool) -> list[TotalsCheck]:
    """Compare each ``totals`` key with its column.

    Levels and cumulative columns compare with their latest value,
    per-interval columns with the sum of the series. That sum is only a
    total when the series covers the full history (``1h``/``1d``, not
    ``1m``), so ``full_history`` says whether to compare it.
    """
    columns = payload.get("columns", {})
    checks: list[TotalsCheck] = []
    for key, raw in payload.get("totals", {}).items():
        if key not in TOTALS_MAP:
            checks.append(TotalsCheck(key, None, Decimal(raw), None, None, "no column"))
            continue
        column, decimals = TOTALS_MAP[key]
        total = Decimal(int(raw)) / (Decimal(10) ** decimals)
        values = columns.get(column)
        if not values:
            checks.append(TotalsCheck(key, column, total, None, False, "column missing"))
            continue
        kind = kinds[column].kind if column in kinds else UNKNOWN
        if kind == PER_INTERVAL:
            if not full_history:
                checks.append(TotalsCheck(key, column, total, None, None, "series doesn't cover full history"))
                continue
            series_value = sum((Decimal(str(v)) for v in values if v is not None), Decimal(0))
        else:
            series_value = Decimal(str(values[-1]))
        ok = _close(total, series_value)
        if not ok:
            log.warning("totals.%s = %s but %s (%s) = %s", key, total, column, kind, series_value)
        checks.append(TotalsCheck(key, column, total, series_value, ok, kind))
    return checks
