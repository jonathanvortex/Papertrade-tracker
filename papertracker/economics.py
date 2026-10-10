"""Cost of minting PAPER (SPEC 5.2): k(m), the mint rate r, and cost per PAPER.

All arithmetic is in Decimal. Inputs may be Decimal, int or str; floats are
converted through str() so 0.016 means exactly 0.016.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Mapping

Number = Decimal | int | str | float


def D(x: Number) -> Decimal:
    if isinstance(x, Decimal):
        return x
    if isinstance(x, float):
        return Decimal(str(x))
    return Decimal(x)


@dataclass(frozen=True)
class ImpactParams:
    """Parameters of k(m). Defaults are Papertrade's BTC launch values."""

    deadband: Decimal = Decimal("0.00002")
    rate_multiplier: Decimal = Decimal("15000")
    reference_notional: Decimal = Decimal("100000")
    position_multiplier: Decimal = Decimal("814.598")
    base_rate: Decimal = Decimal("0.10")
    win_fee: Decimal = Decimal("0.02")

    @classmethod
    def from_config(cls, config: Mapping[str, Any], asset: str | None = None) -> ImpactParams:
        """Build from the parsed ``config.yaml``; ``asset`` defaults to ``config['asset']``."""
        impact = config["impact"]
        asset = asset or config["asset"]
        multipliers = impact["positionMultiplier"]
        if asset not in multipliers:
            raise KeyError(f"no positionMultiplier for asset {asset!r} in config")
        return cls(
            deadband=D(impact["deadband"]),
            rate_multiplier=D(impact["rateMultiplier"]),
            reference_notional=D(impact["referenceNotional"]),
            position_multiplier=D(multipliers[asset]),
            base_rate=D(impact["baseRate"]),
            win_fee=D(impact["winFee"]),
        )


BTC = ImpactParams()
ETH = ImpactParams(position_multiplier=Decimal("483.979"))


def k_before_fee(m: Number, p: ImpactParams = BTC) -> Decimal:
    """Share of the winning side's gain kept at close move ``m``, before the win fee."""
    m = D(m)
    m_eff = m - p.deadband
    if m_eff <= 0:
        raise ValueError(f"move m={m} must exceed the deadband {p.deadband}")
    term1 = 1 / (m_eff * p.rate_multiplier)
    term2 = p.reference_notional / (Decimal(10) ** 6 * m_eff * p.position_multiplier)
    scale = (1 - p.base_rate) / (1 + term1 + term2)
    return scale * (m_eff / m)


def k(m: Number, p: ImpactParams = BTC) -> Decimal:
    """Share of the winning side's gain kept on a hedged pair closed at move ``m``."""
    return k_before_fee(m, p) * (1 - p.win_fee)


@dataclass(frozen=True)
class MintRate:
    """PAPER minted per $1 of loss, with notes on any assumption made."""

    r: Decimal
    flags: tuple[str, ...] = field(default=())


def mint_rate(
    rate: Number,
    decay: Number,
    tail_progress: Number,
    *,
    tracked_lp: Number | None = None,
    cliff: Number | None = None,
) -> MintRate:
    """r = rate below the cliff, else rate × (decay / (decay + tailProgress))².

    When tracked LP is unknown the tail formula is used directly; it equals
    the flat rate while tailProgress is 0, and the result is flagged.
    """
    rate, decay, tail_progress = D(rate), D(decay), D(tail_progress)
    flags: list[str] = []
    if tracked_lp is not None and cliff is not None and D(tracked_lp) < D(cliff):
        return MintRate(rate)
    if tracked_lp is None or cliff is None:
        flags.append(
            "tracked LP unknown: flat rate assumed (tailProgress is 0)"
            if tail_progress == 0
            else "tracked LP unknown: tail decay applied from tailProgress"
        )
    denom = decay + tail_progress
    if denom <= 0:
        raise ValueError(f"decay + tailProgress must be positive, got {denom}")
    return MintRate(rate * (decay / denom) ** 2, tuple(flags))


def effective_mint_rate(r: Number, *, queue_active: bool | None, loss_fee: Number = "0.02") -> tuple[Decimal, tuple[str, ...]]:
    """PAPER per $1 of loss after the loss fee.

    With the queue empty the loss fee is taken first, so minting happens on
    (1 − loss_fee) of the loss. Unknown queue state defaults to active.
    """
    r = D(r)
    if queue_active is None:
        return r, ("queue state unknown: assumed active",)
    return (r if queue_active else r * (1 - D(loss_fee))), ()


def marginal_cost(k_value: Number, r_effective: Number) -> Decimal | None:
    """USDC cost per PAPER = (1 − k) ÷ r. None when r is 0 (nothing is minted)."""
    r_effective = D(r_effective)
    if r_effective == 0:
        return None
    return (1 - D(k_value)) / r_effective


@dataclass(frozen=True)
class Round:
    """One hedged round: both sides opened at ``leverage`` × ``margin``, closed at move ``m``."""

    loss: Decimal
    cost: Decimal
    paper: Decimal


def hedged_round(m: Number, leverage: Number, margin_per_side: Number, r_effective: Number, p: ImpactParams = BTC) -> Round:
    """The losing side loses notional × m; the winner keeps k of the same gain."""
    loss = D(leverage) * D(margin_per_side) * D(m)
    return Round(loss=loss, cost=loss * (1 - k(m, p)), paper=loss * D(r_effective))
