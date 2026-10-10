import copy
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from papertracker import alerts
from papertracker.client import Attempt, FetchResult
from papertracker.config import load_config
from papertracker.metrics import Calculator
from papertracker.semantics import CUMULATIVE, LEVEL
from papertracker.store import Store

H = 3_600_000


@pytest.fixture
def store():
    with Store(":memory:") as s:
        yield s


@pytest.fixture
def config():
    return load_config()


def calc_with(config, rewards, staked=1000, measured="1"):
    n = len(rewards)
    ts = [1_791_324_000_000 + i * H for i in range(n)]
    cols = {"stakingRewards": rewards, "paperStaked": [staked] * n, "paperSupply": [staked] * n}
    kinds = {"stakingRewards": CUMULATIVE, "paperStaked": LEVEL, "paperSupply": LEVEL}
    return Calculator(ts, cols, kinds=kinds, config={**config, "measuredCostPerPaper": measured})


def test_emission_setting_change_fires_once(store, summary):
    store.insert_summary(summary)
    assert alerts.check_emission_settings(store) == []  # first sighting only records
    changed = copy.deepcopy(summary)
    changed["source"]["block"] = str(int(summary["source"]["block"]) + 100)
    changed["source"]["at"] += 60_000
    changed["paper"]["rate"] = str(90 * 10**18)
    store.insert_summary(changed)
    fired = alerts.check_emission_settings(store)
    assert [a.kind for a in fired] == ["emission_setting"]
    assert "rate changed from 100 to 90" in fired[0].message
    assert alerts.check_emission_settings(store) == []  # not repeated


def test_rewards_started_fires_once(store, config):
    assert alerts.check_rewards_started(store, calc_with(config, [0] * 30)) == []
    fired = alerts.check_rewards_started(store, calc_with(config, [0] * 29 + [5]))
    assert [a.kind for a in fired] == ["rewards_started"]
    assert alerts.check_rewards_started(store, calc_with(config, [0] * 29 + [5])) == []


def test_live_ratio_crossing_both_ways(store, config):
    # 24 h rewards of 240 on 1000 staked at cost $1 = 24%/day; 0 rewards = 0%/day.
    high = calc_with(config, [10 * i for i in range(30)])
    low = calc_with(config, [0] + [1] * 29)  # started, then flat: 0%/day
    from decimal import Decimal
    threshold = Decimal(5)
    assert alerts.check_live_ratio(store, high, threshold) == []  # first reading sets the side
    assert alerts.check_live_ratio(store, high, threshold) == []
    down = alerts.check_live_ratio(store, low, threshold)
    assert len(down) == 1 and "below" in down[0].message
    up = alerts.check_live_ratio(store, high, threshold)
    assert len(up) == 1 and "above" in up[0].message


def test_live_ratio_not_started_does_nothing(store, config):
    from decimal import Decimal
    assert alerts.check_live_ratio(store, calc_with(config, [0] * 30), Decimal(1)) == []
    assert store.get_state("live_ratio_side") is None


def test_semantics_change_alert():
    fired = alerts.check_semantics([("trades", "cumulative", "per_interval")])
    assert fired[0].kind == "column_semantics" and "trades" in fired[0].message


def test_stale_fires_once_per_outage(store):
    now = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)
    ok_at = (now - timedelta(hours=4)).isoformat()
    store.record_fetch(FetchResult("/x", [Attempt(1, ok_at, 200, "{}")]))
    assert alerts.check_stale(store, 3, now - timedelta(hours=2)) == []
    assert len(alerts.check_stale(store, 3, now)) == 1
    assert alerts.check_stale(store, 3, now + timedelta(hours=1)) == []
    store.record_fetch(FetchResult("/x", [Attempt(1, now.isoformat(), 200, "{}")]))
    assert alerts.check_stale(store, 3, now + timedelta(hours=4)) != []  # a new outage


def test_telegram_delivery(store):
    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(200, json={"ok": True})

    notifier = alerts.Notifier("TOKEN", "42", transport=httpx.MockTransport(handler))
    notifier.send(store, [alerts.Alert("emission_setting", "rate changed")])
    assert sent[0].url.path == "/botTOKEN/sendMessage"
    assert json.loads(sent[0].content) == {"chat_id": "42", "text": "PAPER tracker: rate changed"}
    assert store.alerts()[0]["delivered"] == 1


def test_telegram_failure_is_logged_without_token(store):
    notifier = alerts.Notifier("SECRET", "42", transport=httpx.MockTransport(lambda r: httpx.Response(401)))
    notifier.send(store, [alerts.Alert("stale", "x")])
    row = store.alerts()[0]
    assert row["delivered"] == 0 and row["error"] == "HTTP 401"
    assert "SECRET" not in row["error"]


def test_unconfigured_notifier_only_logs(store, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    alerts.Notifier().send(store, [alerts.Alert("stale", "x")])
    assert store.alerts()[0]["error"] == "telegram not configured"
