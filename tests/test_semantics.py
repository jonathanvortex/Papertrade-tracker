import copy
from decimal import Decimal

import pytest

from papertracker.semantics import (
    CUMULATIVE,
    LEVEL,
    PER_INTERVAL,
    UNKNOWN,
    check_totals,
    classify,
    classify_all,
)
from papertracker.store import Store


# Synthetic series -------------------------------------------------------

def test_synthetic_cumulative():
    assert classify("stakingRewards", [0, 0, 1.5, 1.5, 4, 9]).kind == CUMULATIVE


def test_synthetic_per_interval():
    assert classify("volume", [0, 120, 80, 0, 300]).kind == PER_INTERVAL


def test_known_levels_stay_levels_whatever_the_shape():
    assert classify("tvl", [5, 3, 9]).kind == LEVEL
    assert classify("users", [1, 2, 3]).kind == LEVEL
    assert classify("paperSupply", [0, 0, 0]).kind == LEVEL
    assert classify("paperStaked", [0, 10, 5]).kind == LEVEL


def test_all_zero_is_unknown():
    assert classify("trades", [0, 0, 0]).kind == UNKNOWN
    assert classify("trades", []).kind == UNKNOWN
    assert classify("trades", [None, 0]).kind == UNKNOWN


def test_nones_are_skipped():
    assert classify("trades", [1, None, 2]).kind == CUMULATIVE


# Real fixtures ------------------------------------------------------------

def test_real_1h_fixture(history):
    kinds = {c.column: c.kind for c in classify_all(history["1h"]["columns"]).values()}
    assert kinds["tvl"] == LEVEL
    assert kinds["users"] == LEVEL
    assert kinds["paperSupply"] == LEVEL and kinds["paperStaked"] == LEVEL
    # Everything else was all zeros on 2026-10-09.
    for col in ("stakingRewards", "volume", "volumeBtc", "volumeEth", "traderPnl",
                "liquidation", "trades", "paperRevenue"):
        assert kinds[col] == UNKNOWN


@pytest.mark.parametrize("interval", ["1h", "1d", "1m"])
def test_real_totals_cross_check_passes(history, interval):
    h = history[interval]
    checks = {c.key: c for c in check_totals(h, classify_all(h["columns"]), full_history=interval != "1m")}
    assert checks["tvlRaw"].ok is True
    assert checks["tvlRaw"].total == Decimal("14831637.903488")
    assert checks["usersRaw"].ok is True and checks["usersRaw"].total == 2260
    assert checks["stakerFees24hRaw"].column is None and checks["stakerFees24hRaw"].ok is None
    assert all(c.ok is not False for c in checks.values())


def test_totals_mismatch_is_flagged(history):
    h = copy.deepcopy(history["1h"])
    h["totals"]["rewardsRaw"] = str(5 * 10**18)  # rewards paid, but the series is still zero
    checks = {c.key: c for c in check_totals(h, classify_all(h["columns"]), full_history=True)}
    assert checks["rewardsRaw"].ok is False


def test_per_interval_total_compares_with_sum():
    payload = {"columns": {"volume": [0, 100, 50, 25]}, "totals": {"volumeRaw": str(175 * 10**18)}}
    (check,) = check_totals(payload, classify_all(payload["columns"]), full_history=True)
    assert check.ok is True and check.series_value == 175
    (check,) = check_totals(payload, classify_all(payload["columns"]), full_history=False)
    assert check.ok is None


def test_cumulative_total_compares_with_last():
    payload = {"columns": {"volume": [0, 100, 150, 175]}, "totals": {"volumeRaw": str(175 * 10**18)}}
    (check,) = check_totals(payload, classify_all(payload["columns"]), full_history=True)
    assert check.ok is True and check.note == CUMULATIVE


# Storage and change detection --------------------------------------------

def test_reclassification_is_reported_once():
    with Store(":memory:") as store:
        assert store.save_semantics(classify_all({"trades": [0, 0], "tvl": [1, 2]}).values()) == []
        assert store.save_semantics(classify_all({"trades": [0, 0], "tvl": [1, 2]}).values()) == []
        changes = store.save_semantics(classify_all({"trades": [0, 3, 5], "tvl": [1, 2]}).values())
        assert changes == [("trades", UNKNOWN, CUMULATIVE)]
        changes = store.save_semantics(classify_all({"trades": [0, 3, 1], "tvl": [1, 2]}).values())
        assert changes == [("trades", CUMULATIVE, PER_INTERVAL)]
        assert store.semantics() == {"trades": PER_INTERVAL, "tvl": LEVEL}


def test_changed_at_moves_only_on_change():
    with Store(":memory:") as store:
        store.save_semantics(classify_all({"trades": [0]}).values())
        first = store.db.execute("SELECT changed_at FROM column_semantics").fetchone()[0]
        store.db.execute("UPDATE column_semantics SET changed_at = 'old'")
        store.save_semantics(classify_all({"trades": [0]}).values())
        assert store.db.execute("SELECT changed_at FROM column_semantics").fetchone()[0] == "old"
        store.save_semantics(c for c in classify_all({"trades": [1]}).values())  # generator input
        row = store.db.execute("SELECT kind, changed_at FROM column_semantics").fetchone()
        assert row[0] == CUMULATIVE and row[1] not in ("old", None) and first
