from decimal import Decimal

import pytest

from papertracker import metrics
from papertracker.config import load_config
from papertracker.metrics import NOT_STARTED, Calculator, cost_per_paper, summary_decimals
from papertracker.semantics import CUMULATIVE, LEVEL, PER_INTERVAL, classify_all
from papertracker.store import Store, flatten_summary

H = metrics.HOUR_MS
T0 = 1_791_324_000_000


@pytest.fixture
def config():
    return load_config()


def synthetic(n=200):
    """Rewards +10/h, staked 1000, supply +50/h, trader PnL -20/h, volume 1000/h."""
    ts = [T0 + i * H for i in range(n)]
    cols = {
        "stakingRewards": [10 * i for i in range(n)],
        "paperStaked": [1000] * n,
        "paperSupply": [2000 + 50 * i for i in range(n)],
        "traderPnl": [-20 * i for i in range(n)],
        "volume": [1000] * n,
        "tvl": [5_000_000] * n,
        "users": [100 + i for i in range(n)],
    }
    kinds = {"stakingRewards": CUMULATIVE, "paperStaked": LEVEL, "paperSupply": LEVEL,
             "traderPnl": CUMULATIVE, "volume": PER_INTERVAL, "tvl": LEVEL, "users": LEVEL}
    return ts, cols, kinds


def calc(config, n=200, measured="2.4", **kw):
    ts, cols, kinds = synthetic(n)
    config = {**config, "measuredCostPerPaper": measured}
    return Calculator(ts, cols, kinds=kinds, config=config, **kw)


def val(row, name):
    return row.values[name].value


@pytest.mark.parametrize("granularity", ["1h", "24h", "7d"])
def test_headline_ratio_and_payback(config, granularity):
    row = calc(config).latest(granularity)
    assert val(row, "reward_per_staked_per_day") == Decimal("0.24")
    assert val(row, "cost_per_paper") == Decimal("2.4")
    assert val(row, "live_ratio_pct_per_day") == 10
    assert val(row, "payback_days") == 10


def test_flows_per_window(config):
    c = calc(config)
    h1, d1, w1 = c.latest("1h"), c.latest("24h"), c.latest("7d")
    assert (val(h1, "rewards"), val(d1, "rewards"), val(w1, "rewards")) == (10, 240, 240)
    assert val(d1, "minted") == 1200
    assert val(d1, "net_trader_loss") == 480
    assert val(d1, "volume") == 24000
    assert val(d1, "net_loss_per_volume") == Decimal("0.02")
    assert val(d1, "rewards_per_new_paper") == Decimal("0.2")
    supply_now = 2000 + 50 * 199
    assert val(d1, "dilution_pct_per_day") == Decimal(1200) / supply_now * 100
    assert val(h1, "dilution_pct_per_day") == val(d1, "dilution_pct_per_day")


def test_levels(config):
    c = calc(config)
    assert val(c.latest("1h"), "users") == 299
    assert val(c.latest("24h"), "users") == 299
    assert val(c.latest("7d"), "users") == Decimal(sum(100 + i for i in range(32, 200))) / 168
    assert val(c.latest("24h"), "staked") == 1000


def test_marginal_cost_used_when_measured_not_set(config, summary):
    c = calc(config, measured=None, summaries=[(T0, summary_decimals(flatten_summary(summary)))])
    row = c.latest("24h")
    assert row.values["cost_measured"].note == "n/a — not set"
    assert val(row, "cost_per_paper") == val(row, "cost_marginal")
    assert val(row, "live_ratio_pct_per_day") == Decimal("0.24") / val(row, "cost_marginal") * 100


def test_cost_from_real_summary(config, summary):
    s = summary_decimals(flatten_summary(summary))
    auto = cost_per_paper(s, config)
    assert auto.mint_rate.value == 98  # balances.queue is 0: queue empty, 2% loss fee
    assert any("balances.queue" in f for f in auto.flags)
    active = cost_per_paper(s, {**config, "queueState": "active"})
    assert active.mint_rate.value == 100
    assert abs(active.marginal.value - Decimal("0.00129")) < Decimal("0.000005")


# Zero handling (SPEC section 9) ------------------------------------------

def test_real_fixtures_all_zero_series_are_not_started(config, history, summary):
    h = history["1h"]
    n = len(h["columns"]["tvl"])
    c = Calculator([h["startMs"] + i * H for i in range(n)], h["columns"],
                   summaries=[(summary["source"]["at"], summary_decimals(flatten_summary(summary)))], config=config)
    rows = c.rows()  # every hour, every window: no exception
    assert len(rows) == 3 * n
    row = c.latest("24h")
    for name in ("rewards", "staked", "reward_per_staked_per_day", "live_ratio_pct_per_day", "payback_days",
                 "minted", "dilution_pct_per_day", "rewards_per_new_paper", "net_trader_loss",
                 "liquidations", "volume", "trades", "protocol_revenue", "pending_rewards"):
        assert row.values[name].note == NOT_STARTED, name
    assert val(row, "tvl") == Decimal("14831637.903487999")
    assert val(row, "cost_per_paper") > 0


def test_zero_rewards_in_window_after_start(config):
    ts, cols, kinds = synthetic(60)
    cols["stakingRewards"] = [0] * 10 + [50] * 50  # paid once at hour 10, nothing since
    row = Calculator(ts, cols, kinds=kinds, config={**config, "measuredCostPerPaper": "1"}).latest("24h")
    assert val(row, "rewards") == 0
    assert val(row, "live_ratio_pct_per_day") == 0
    assert row.values["payback_days"].note == "n/a — no rewards in window"


def test_zero_supply_and_staked(config):
    ts, cols, kinds = synthetic(60)
    cols["paperStaked"] = [0] * 60
    cols["paperSupply"] = [0] * 30 + [10] * 29 + [0]  # minted, then burned to zero
    row = Calculator(ts, cols, kinds=kinds, config=config).latest("24h")
    assert row.values["dilution_pct_per_day"].note == "n/a — zero supply"
    assert val(row, "rewards") == 240
    assert row.values["reward_per_staked_per_day"].ok  # supply fallback, mean over window is non-zero
    assert any("paperSupply" in f for f in row.flags)


def test_staked_falls_back_to_supply_when_zero(config):
    ts, cols, kinds = synthetic(60)
    cols["paperStaked"] = [0] * 60
    row = Calculator(ts, cols, kinds=kinds, config=config).latest("1h")
    assert val(row, "staked") == 2000 + 50 * 59
    assert any("using paperSupply" in f for f in row.flags)


def test_short_history_window(config):
    row = calc(config, n=60).latest("7d")
    assert row.values["rewards"].note == "n/a — needs 168 h of history"


def test_gap_in_hours_breaks_window(config):
    ts, cols, kinds = synthetic(60)
    ts = ts[:30] + [t + H for t in ts[30:]]  # one missing hour
    c = Calculator(ts, cols, kinds=kinds, config=config)
    assert c.row(40, "24h").values["rewards"].note == "n/a — needs 24 h of history"
    assert c.row(59, "24h").values["rewards"].ok


def test_latest_skips_partial_hour(config):
    ts, cols, kinds = synthetic(30)
    c = Calculator(ts, cols, kinds=kinds, partial=[False] * 29 + [True], config=config)
    assert c.latest("1h").ts == ts[-2]
    assert c.latest("1h", include_partial=True).ts == ts[-1]


def test_summary_far_from_hour_is_flagged(config, summary):
    s = summary_decimals(flatten_summary(summary))
    ts, cols, kinds = synthetic(30)
    c = Calculator(ts, cols, kinds=kinds, config=config, summaries=[(ts[-1] + H + 60_000, s)])
    assert not any("summary from" in f for f in c.row(29, "1h").flags)
    assert any("summary from" in f and "after" in f for f in c.row(0, "1h").flags)


# Store --------------------------------------------------------------------

def test_metrics_round_trip_through_store(config, history, summary):
    with Store(":memory:") as store:
        store.upsert_history("1h", history["1h"])
        store.insert_summary(summary)
        _, cols = store.history_series("1h")
        store.save_semantics(classify_all(cols).values())
        c = metrics.from_store(store, config)
        store.upsert_metrics(c.rows())
        store.upsert_metrics(c.rows())
        rows = store.metrics("24h")
        assert len(rows) == 60
        last = rows[-1]
        assert last["is_partial"] == 1
        assert last["rewards"] is None
        assert NOT_STARTED in last["notes"]
        assert Decimal(last["cost_per_paper"]) == c.latest("24h", include_partial=True).values["cost_per_paper"].value
