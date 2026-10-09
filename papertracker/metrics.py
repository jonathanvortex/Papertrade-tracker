"""Section 5 metrics, computed from the hourly history.

Every hourly point gets three rows of metrics:

- ``1h``: the hour ending at that point
- ``24h``: the 24 hours ending at that point
- ``7d``: the 168 hours ending at that point, flows shown as per-day averages

Flows are summed over the window. Staked supply is averaged over the
window, as the reward-per-staked denominator. ``users`` and ``tvl`` show
the latest value for ``1h``/``24h`` and the 7-day mean for ``7d``.

A metric that depends on a series that has never been non-zero is
``n/a — not started``. Missing history and zero denominators are ``n/a``
with their own reason. Nothing raises on zeros.

History values arrive as floats and are converted through ``str`` to
Decimal; all arithmetic is Decimal.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Mapping, Sequence

from . import economics
from .semantics import PER_INTERVAL, UNKNOWN, classify_all

NOT_STARTED = "n/a — not started"
HOUR_MS = 3_600_000
WINDOWS = {"1h": 1, "24h": 24, "7d": 168}
HOURS_PER_DAY = Decimal(24)
SUMMARY_MAX_GAP_MS = 2 * HOUR_MS

# Output order. Headline first, then diagnostics (SPEC 5.1, 5.3).
METRICS = (
    "rewards",
    "staked",
    "reward_per_staked_per_day",
    "cost_marginal",
    "cost_measured",
    "cost_per_paper",
    "live_ratio_pct_per_day",
    "payback_days",
    "minted",
    "dilution_pct_per_day",
    "rewards_per_new_paper",
    "net_trader_loss",
    "liquidations",
    "volume",
    "volume_btc",
    "volume_eth",
    "net_loss_per_volume",
    "users",
    "trades",
    "tvl",
    "protocol_revenue",
    "pending_rewards",
    "mint_rate",
    "k",
)


@dataclass(frozen=True)
class Value:
    value: Decimal | None
    note: str = ""

    @property
    def ok(self) -> bool:
        return self.value is not None


def na(reason: str) -> Value:
    return Value(None, reason)


def _div(a: Value, b: Value, zero_note: str) -> Value:
    if not a.ok:
        return a
    if not b.ok:
        return b
    if b.value == 0:
        return na(zero_note)
    return Value(a.value / b.value)


def _scale(a: Value, factor: Decimal) -> Value:
    return Value(a.value * factor, a.note) if a.ok else a


def _per_day(a: Value, hours: int) -> Value:
    """Window total -> per day. Multiplies before dividing so 7-day windows stay exact."""
    return Value(a.value * HOURS_PER_DAY / hours, a.note) if a.ok else a


@dataclass
class MetricRow:
    ts: int
    granularity: str
    is_partial: bool
    values: dict[str, Value]
    flags: tuple[str, ...] = field(default=())


def _dec(v: Any) -> Decimal | None:
    if v is None:
        return None
    return v if isinstance(v, Decimal) else Decimal(str(v))


def flows(ts: Sequence[int], values: Sequence[Decimal | None], kind: str) -> list[Decimal | None]:
    """Per-hour flow of a series.

    Per-interval columns are their own flow. Cumulative, level and unknown
    columns flow by their change since the previous hour (for a level that
    is its growth, e.g. PAPER minted from ``paperSupply``). A point with no
    previous hour has no flow.
    """
    out: list[Decimal | None] = [None] * len(values)
    for i, v in enumerate(values):
        if v is None:
            continue
        if kind == PER_INTERVAL:
            out[i] = v
        elif i > 0 and values[i - 1] is not None and ts[i] - ts[i - 1] == HOUR_MS:
            out[i] = v - values[i - 1]
    return out


def summary_decimals(flat: Mapping[str, tuple[str, str | None]]) -> dict[str, Decimal]:
    """``store.flatten_summary`` output as ``{field: Decimal}``, skipping non-numeric fields."""
    return {k: Decimal(dec) for k, (_, dec) in flat.items() if dec is not None}


def summary_from_row(row: Mapping[str, Any]) -> dict[str, Decimal]:
    """A ``summary_snapshots`` row as ``{field: Decimal}``."""
    keys = list(row.keys())
    return {k: Decimal(row[k]) for k in keys if f"{k}_text" in keys and row[k] is not None}


@dataclass(frozen=True)
class Cost:
    k: Value
    mint_rate: Value
    marginal: Value
    measured: Value
    per_paper: Value
    flags: tuple[str, ...]


def cost_per_paper(summary: Mapping[str, Decimal] | None, config: Mapping[str, Any]) -> Cost:
    """Cost per PAPER (SPEC 5.2) from one summary snapshot and the config."""
    flags: list[str] = []
    k = Value(economics.k(config["m"], economics.ImpactParams.from_config(config)))
    measured_cfg = config.get("measuredCostPerPaper")
    measured = Value(economics.D(measured_cfg)) if measured_cfg is not None else na("n/a — not set")
    if summary is None:
        r = na("n/a — no summary")
    else:
        missing = [f for f in ("paper_rate", "paper_decay", "paper_tailProgress") if f not in summary]
        if missing:
            r = na(f"n/a — summary lacks {', '.join(missing)}")
        else:
            mr = economics.mint_rate(
                summary["paper_rate"],
                summary["paper_decay"],
                summary["paper_tailProgress"],
                tracked_lp=summary.get("paper_trackedLp"),
                cliff=summary.get("paper_cliff"),
            )
            flags.extend(mr.flags)
            queue_active = _queue_state(summary, config.get("queueState", "auto"), flags)
            r_eff, qflags = economics.effective_mint_rate(mr.r, queue_active=queue_active, loss_fee=config.get("lossFee", "0.02"))
            flags.extend(qflags)
            r = Value(r_eff)
    marginal = _div(Value(1 - k.value), r, "n/a — mint rate is 0")
    per_paper = measured if measured.ok else marginal
    return Cost(k, r, marginal, measured, per_paper, tuple(flags))


def _queue_state(summary: Mapping[str, Decimal], setting: str, flags: list[str]) -> bool | None:
    if setting == "active":
        return True
    if setting == "empty":
        return False
    if "balances_queue" in summary:
        flags.append("queue state read from balances.queue (meaning unconfirmed)")
        return summary["balances_queue"] > 0
    return None


class _Series:
    """Hourly series of one column with its flows and a running 'started' flag."""

    def __init__(self, ts: Sequence[int], values: Sequence[Any], kind: str):
        self.values = [_dec(v) for v in values]
        self.flows = flows(ts, self.values, kind)
        self.started: list[bool] = []
        seen = False
        for v in self.values:
            seen = seen or bool(v)
            self.started.append(seen)


class Calculator:
    def __init__(
        self,
        ts: Sequence[int],
        columns: Mapping[str, Sequence[Any]],
        *,
        partial: Sequence[bool] | None = None,
        kinds: Mapping[str, str] | None = None,
        summaries: Sequence[tuple[int, Mapping[str, Decimal]]] = (),
        config: Mapping[str, Any],
    ):
        self.ts = list(ts)
        self.partial = list(partial) if partial is not None else [False] * len(self.ts)
        if kinds is None:
            kinds = {c.column: c.kind for c in classify_all(columns).values()}
        self.series = {name: _Series(self.ts, vals, kinds.get(name, UNKNOWN)) for name, vals in columns.items()}
        self.summaries = sorted(summaries, key=lambda s: s[0])
        self._summary_ts = [s[0] for s in self.summaries]
        self.config = config

    # Window helpers --------------------------------------------------------

    def _window(self, i: int, hours: int) -> range | None:
        lo = i - hours + 1
        if lo < 0 or self.ts[i] - self.ts[lo] != (hours - 1) * HOUR_MS:
            return None
        return range(lo, i + 1)

    def _flow(self, col: str, i: int, hours: int) -> Value:
        """Sum of the column's hourly flows over the window."""
        s = self.series.get(col)
        if s is None:
            return na(f"n/a — no {col} column")
        if not s.started[i]:
            return na(NOT_STARTED)
        w = self._window(i, hours)
        if w is None or any(s.flows[j] is None for j in w):
            return na(f"n/a — needs {hours} h of history")
        return Value(sum((s.flows[j] for j in w), Decimal(0)))

    def _level(self, col: str, i: int, hours: int) -> Value:
        """Mean of the column's level over the window's points."""
        s = self.series.get(col)
        if s is None:
            return na(f"n/a — no {col} column")
        if not s.started[i]:
            return na(NOT_STARTED)
        w = self._window(i, hours)
        if w is None or any(s.values[j] is None for j in w):
            return na(f"n/a — needs {hours} h of history")
        return Value(sum((s.values[j] for j in w), Decimal(0)) / len(w))

    def _summary_for(self, i: int) -> tuple[Mapping[str, Decimal] | None, list[str]]:
        """The snapshot nearest the close of hour ``i``, flagged if more than 2 h away."""
        if not self.summaries:
            return None, []
        close = self.ts[i] + HOUR_MS
        pos = bisect_right(self._summary_ts, close)
        candidates = [j for j in (pos - 1, pos) if 0 <= j < len(self.summaries)]
        j = min(candidates, key=lambda j: abs(self._summary_ts[j] - close))
        gap = self._summary_ts[j] - close
        if abs(gap) > SUMMARY_MAX_GAP_MS:
            when = "after" if gap > 0 else "before"
            return self.summaries[j][1], [f"summary from {abs(gap) / HOUR_MS:.0f} h {when} this hour"]
        return self.summaries[j][1], []

    # Metrics ---------------------------------------------------------------

    def row(self, i: int, granularity: str) -> MetricRow:
        hours = WINDOWS[granularity]

        def shown(a: Value) -> Value:  # 7d flows are shown per day
            return a if hours <= 24 else _per_day(a, hours)

        flags: list[str] = []
        v: dict[str, Value] = {}

        def flow(col: str) -> Value:
            return self._flow(col, i, hours)

        rewards = flow("stakingRewards")
        staked = self._level("paperStaked", i, hours)
        supply_now = self._level("paperSupply", i, 1)
        if staked.ok and staked.value == 0 and supply_now.ok and supply_now.value != 0:
            staked = self._level("paperSupply", i, hours)
            flags.append("paperStaked is 0: using paperSupply as staked supply")
        elif not staked.ok and staked.note == NOT_STARTED and supply_now.ok:
            staked = self._level("paperSupply", i, hours)
            flags.append("paperStaked not started: using paperSupply as staked supply")
        minted = flow("paperSupply")
        reward_per_staked = _div(_per_day(rewards, hours), staked, "n/a — zero staked supply")

        summary, sflags = self._summary_for(i)
        flags.extend(sflags)
        cost = cost_per_paper(summary, self.config)
        flags.extend(cost.flags)
        ratio = _div(reward_per_staked, cost.per_paper, "n/a — zero cost per PAPER")

        v["rewards"] = shown(rewards)
        v["staked"] = staked
        v["reward_per_staked_per_day"] = reward_per_staked
        v["cost_marginal"] = cost.marginal
        v["cost_measured"] = cost.measured
        v["cost_per_paper"] = cost.per_paper
        v["live_ratio_pct_per_day"] = _scale(ratio, Decimal(100))
        v["payback_days"] = _div(Value(Decimal(1)), ratio, "n/a — no rewards in window")

        v["minted"] = shown(minted)
        v["dilution_pct_per_day"] = _scale(
            _div(_per_day(minted, hours), supply_now, "n/a — zero supply"), Decimal(100)
        )
        v["rewards_per_new_paper"] = _div(rewards, minted, "n/a — nothing minted in window")
        pnl = flow("traderPnl")
        net_loss = _scale(pnl, Decimal(-1))
        volume = flow("volume")
        v["net_trader_loss"] = shown(net_loss)
        v["liquidations"] = shown(flow("liquidation"))
        v["volume"] = shown(volume)
        v["volume_btc"] = shown(flow("volumeBtc"))
        v["volume_eth"] = shown(flow("volumeEth"))
        v["net_loss_per_volume"] = _div(net_loss, volume, "n/a — no volume in window")
        v["users"] = self._level("users", i, 1 if hours <= 24 else hours)
        v["trades"] = shown(flow("trades"))
        v["tvl"] = self._level("tvl", i, 1 if hours <= 24 else hours)
        v["protocol_revenue"] = shown(flow("paperRevenue"))

        if summary is None or "paper_pendingRewards" not in summary:
            v["pending_rewards"] = na("n/a — no summary")
        elif summary["paper_pendingRewards"] == 0 and not self._started("stakingRewards", i):
            v["pending_rewards"] = na(NOT_STARTED)
        else:
            v["pending_rewards"] = Value(summary["paper_pendingRewards"])
        v["mint_rate"] = cost.mint_rate
        v["k"] = cost.k

        return MetricRow(self.ts[i], granularity, self.partial[i], v, tuple(dict.fromkeys(flags)))

    def _started(self, col: str, i: int) -> bool:
        s = self.series.get(col)
        return bool(s and s.started[i])

    def rows(self) -> list[MetricRow]:
        return [self.row(i, g) for i in range(len(self.ts)) for g in WINDOWS]

    def latest(self, granularity: str, *, include_partial: bool = False) -> MetricRow | None:
        for i in range(len(self.ts) - 1, -1, -1):
            if include_partial or not self.partial[i]:
                return self.row(i, granularity)
        return None


def from_store(store, config: Mapping[str, Any]) -> Calculator:
    """Calculator over the stored hourly history and summary snapshots."""
    rows = store.history("1h")
    ts, columns = store.history_series("1h")
    summaries = [(r["source_ts"], summary_from_row(r)) for r in store.summaries() if r["source_ts"] is not None]
    return Calculator(
        ts,
        columns,
        partial=[bool(r["is_partial"]) for r in rows],
        kinds=store.semantics() or None,
        summaries=summaries,
        config=config,
    )
