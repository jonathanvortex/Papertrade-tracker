"""SPEC section 9 economics tests, plus edge cases."""

from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from papertracker.economics import (
    BTC,
    ETH,
    ImpactParams,
    effective_mint_rate,
    hedged_round,
    k,
    k_before_fee,
    marginal_cost,
    mint_rate,
)

ROOT = Path(__file__).resolve().parent.parent


def test_k_001_btc():
    assert abs(k("0.01") - Decimal("0.8638")) <= Decimal("0.0005")


def test_k_0001_btc_before_fee_matches_docs_74pct():
    assert abs(k_before_fee("0.001") - Decimal("0.739")) <= Decimal("0.0005")


def test_k_0016_btc_and_marginal_cost_at_r_100():
    kv = k("0.016")
    assert abs(kv - Decimal("0.871")) <= Decimal("0.0005")
    cost = marginal_cost(kv, 100)
    assert abs(cost - Decimal("0.00129")) <= Decimal("0.000005")


def test_k_00107_round_50x_50k_per_side():
    assert abs(k("0.0107") - Decimal("0.8650")) <= Decimal("0.00005")
    rnd = hedged_round("0.0107", 50, 50_000, 100)
    assert rnd.loss == Decimal("26750")
    assert abs(rnd.cost - 3611) < 1
    assert rnd.paper == 2_675_000


def test_r_in_tail():
    r = mint_rate(rate=100, decay=120_000_000, tail_progress=100_000_000, tracked_lp=3_000_000, cliff=2_000_000)
    assert abs(r.r - Decimal("29.75")) <= Decimal("0.005")
    assert r.flags == ()


def test_r_flat_below_cliff_ignores_tail_progress():
    r = mint_rate(rate=100, decay=120_000_000, tail_progress=100_000_000, tracked_lp=1_000_000, cliff=2_000_000)
    assert r.r == 100


def test_r_without_tracked_lp_is_flat_at_zero_tail_and_flagged():
    r = mint_rate(rate=100, decay=120_000_000, tail_progress=0)
    assert r.r == 100
    assert r.flags and "tracked LP unknown" in r.flags[0]


def test_queue_empty_mints_on_98pct_of_loss():
    assert effective_mint_rate(100, queue_active=False) == (Decimal("98.00"), ())
    assert effective_mint_rate(100, queue_active=True) == (Decimal(100), ())
    r, flags = effective_mint_rate(100, queue_active=None)
    assert r == 100 and flags


def test_zero_mint_rate_gives_no_cost():
    assert marginal_cost(k("0.016"), 0) is None


def test_move_inside_deadband_rejected():
    with pytest.raises(ValueError):
        k("0.00002")


def test_eth_keeps_less_than_btc():
    # Smaller position multiplier means more impact, so less is kept.
    assert k("0.016", ETH) < k("0.016", BTC)


def test_config_yaml_defaults_match_launch_values():
    config = yaml.safe_load((ROOT / "config.yaml").read_text())
    assert ImpactParams.from_config(config) == BTC
    assert ImpactParams.from_config(config, "ETH") == ETH
    assert Decimal(str(config["m"])) == Decimal("0.016")
