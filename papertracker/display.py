"""Labels and number formatting shared by ``status`` and the dashboard."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

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
