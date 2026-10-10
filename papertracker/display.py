"""Labels and number formatting shared by ``status`` and the dashboard."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from . import metrics
from .metrics import Value

# metric -> (label, format kind)
LABELS: dict[str, tuple[str, str]] = {
    "rewards": ("Rewards flow", "usd"),
    "staked": ("Staked supply (PAPER)", "amount"),
    "reward_per_staked_per_day": ("Reward per staked PAPER per day", "usd_small"),
    "cost_marginal": ("Cost per PAPER, marginal", "usd_small"),
    "cost_measured": ("Cost per PAPER, measured", "usd_small"),
    "cost_per_paper": ("Cost per PAPER (used)", "usd_small"),
    "live_ratio_pct_per_day": ("Live ratio (%/day)", "pct"),
    "payback_days": ("Payback (days)", "days"),
    "minted": ("PAPER minted", "amount"),
    "dilution_pct_per_day": ("Dilution rate (%/day)", "pct"),
    "rewards_per_new_paper": ("Rewards per new PAPER", "usd_small"),
    "net_trader_loss": ("Net trader loss", "usd"),
    "liquidations": ("Liquidations", "usd"),
    "volume": ("Volume", "usd"),
    "volume_btc": ("Volume, BTC", "usd"),
    "volume_eth": ("Volume, ETH", "usd"),
    "net_loss_per_volume": ("Net loss per $ volume", "fraction_pct"),
    "users": ("Users", "count"),
    "trades": ("Trades", "count"),
    "tvl": ("TVL", "usd"),
    "protocol_revenue": ("Protocol revenue", "amount"),
    "pending_rewards": ("Pending rewards", "amount"),
    "mint_rate": ("Mint rate r (PAPER per $ loss)", "amount"),
    "k": ("k(m), share kept", "ratio"),
}

HEADLINE = (
    "rewards", "staked", "reward_per_staked_per_day", "cost_marginal", "cost_measured",
    "cost_per_paper", "live_ratio_pct_per_day", "payback_days",
)
DIAGNOSTICS = tuple(m for m in LABELS if m not in HEADLINE)

WINDOW_LABELS = {"1h": "Last hour", "24h": "Last 24 h", "7d": "7-day avg"}
WINDOW_NOTE = "Flows are totals for the hour and the 24 h, and per-day averages in the 7-day column."


def _group(x: Decimal, places: int) -> str:
    return f"{x:,.{places}f}"


def fmt_number(value: Decimal, kind: str) -> str:
    if kind == "pct":
        return f"{_group(value, 2)}%"
    if kind == "fraction_pct":
        return f"{_group(value * 100, 3)}%"
    if kind == "days":
        return f"{_group(value, 1)} d"
    if kind == "count":
        return _group(value, 0)
    if kind == "ratio":
        return f"{value:.4f}"
    if kind == "usd_small":
        # Three significant digits without scientific notation: $0.00132.
        places = 4 if value == 0 or abs(value) >= 1 else max(2, 2 - value.adjusted())
        return f"-${_group(-value, places)}" if value < 0 else f"${_group(value, places)}"
    if kind == "usd":
        return f"-${_group(-value, 2)}" if value < 0 else f"${_group(value, 2)}"
    return _group(value, 2)


def fmt(metric: str, v: Value) -> str:
    if not v.ok:
        return v.note or "n/a"
    return fmt_number(v.value, LABELS[metric][1])


def utc(ms: int | None) -> str:
    if ms is None:
        return "never"
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def age(seconds: float | None) -> str:
    if seconds is None:
        return "never"
    if seconds < 90:
        return f"{seconds:.0f} s ago"
    if seconds < 90 * 60:
        return f"{seconds / 60:.0f} min ago"
    if seconds < 48 * 3600:
        return f"{seconds / 3600:.1f} h ago"
    return f"{seconds / 86400:.1f} d ago"


def format_status(store, calc: metrics.Calculator, now: datetime | None = None) -> str:
    """The latest headline and diagnostics as a text table."""
    now = now or datetime.now(timezone.utc)
    rows = {g: calc.latest(g) for g in metrics.WINDOWS}
    if rows["1h"] is None:
        return "No closed hours stored yet. Run `python -m papertracker poll` first."
    label_w = max(len(LABELS[m][0]) for m in LABELS)
    cells = {
        g: {m: fmt(m, rows[g].values[m]) for m in LABELS} for g in metrics.WINDOWS
    }
    col_w = {g: max(len(WINDOW_LABELS[g]), *(len(v) for v in cells[g].values())) for g in metrics.WINDOWS}

    def line(label: str, values: list[str]) -> str:
        return f"  {label:<{label_w}}  " + "  ".join(f"{v:>{col_w[g]}}" for g, v in zip(metrics.WINDOWS, values))

    ts = rows["1h"].ts
    last = store.last_success()
    last_age = (now - datetime.fromisoformat(last)).total_seconds() if last else None
    summary = store.latest_summary()
    out = [
        f"Latest closed hour: {utc(ts)} to {utc(ts + metrics.HOUR_MS)[11:]}",
        f"Data age: last successful poll {age(last_age)}; "
        + (f"summary block {summary['source_block']} at {utc(summary['source_ts'])}" if summary else "no summary"),
        "",
        line("", [WINDOW_LABELS[g] for g in metrics.WINDOWS]),
    ]
    for title, names in (("Headline", HEADLINE), ("Diagnostics", DIAGNOSTICS)):
        out.append(title)
        out.extend(line(LABELS[m][0], [cells[g][m] for g in metrics.WINDOWS]) for m in names)
    out += ["", WINDOW_NOTE]
    flags = sorted({f for r in rows.values() for f in r.flags})
    if flags:
        out += ["", "Assumptions:"] + [f"  - {f}" for f in flags]
    stats = store.attempt_stats()
    if stats["total"]:
        out += ["", f"HTTP attempts: {stats['total']}, 5xx: {stats['server_errors']} "
                    f"({stats['server_errors'] / stats['total']:.1%}), no response: {stats['no_response']}"]
    return "\n".join(out)
