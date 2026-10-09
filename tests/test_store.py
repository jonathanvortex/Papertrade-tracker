import copy
from decimal import Decimal

import pytest

from papertracker.client import Attempt, FetchResult
from papertracker.store import Store, flatten_summary


@pytest.fixture
def store():
    with Store(":memory:") as s:
        yield s


def test_repolling_same_hour_twice_gives_one_row(store, history):
    h = history["1h"]
    store.upsert_history("1h", h)
    store.upsert_history("1h", h)
    rows = store.history("1h")
    assert len(rows) == len(h["columns"]["tvl"]) == 60
    assert len({r["ts"] for r in rows}) == 60


def test_partial_point_is_overwritten_then_closed(store, history):
    h = copy.deepcopy(history["1h"])
    store.upsert_history("1h", h)
    last_ts = h["startMs"] + 59 * h["intervalMs"]
    rows = store.history("1h")
    assert rows[-1]["ts"] == last_ts and rows[-1]["is_partial"] == 1
    assert all(r["is_partial"] == 0 for r in rows[:-1])

    # Same hour, later in the hour: the open point changes in place.
    h["columns"]["tvl"][-1] = 15_000_000.0
    h["source"]["asOfMs"] += 60_000
    store.upsert_history("1h", h)
    rows = store.history("1h")
    assert len(rows) == 60
    assert rows[-1]["tvl"] == 15_000_000.0 and rows[-1]["is_partial"] == 1

    # Next hour starts: one new partial point, the previous one is closed.
    h["startMs"] += h["intervalMs"]
    for col in h["columns"].values():
        col.pop(0)
        col.append(col[-1])
    h["source"]["asOfMs"] = last_ts + h["intervalMs"] + 60_000
    store.upsert_history("1h", h)
    rows = store.history("1h")
    assert len(rows) == 61
    assert [r["is_partial"] for r in rows[-2:]] == [0, 1]
    assert rows[0]["ts"] == history["1h"]["startMs"]  # points no longer returned are kept


def test_every_interval_stores_and_values_round_trip(store, history):
    for interval, h in history.items():
        store.upsert_history(interval, h)
        ts, cols = store.history_series(interval)
        assert ts[0] == h["startMs"]
        assert ts[1] - ts[0] == h["intervalMs"]
        for name, values in h["columns"].items():
            assert cols[name] == [float(v) for v in values]


def test_new_history_column_is_added(store, history):
    h = copy.deepcopy(history["1d"])
    h["columns"]["newSeries"] = [1, 2, 3, 4]
    store.upsert_history("1d", h)
    assert [r["newSeries"] for r in store.history("1d")] == [1, 2, 3, 4]


def test_mismatched_column_lengths_rejected(store, history):
    h = copy.deepcopy(history["1d"])
    h["columns"]["tvl"].pop()
    with pytest.raises(ValueError, match="different lengths"):
        store.upsert_history("1d", h)


def test_flatten_summary_scales_18_decimals_exactly(summary):
    f = flatten_summary(summary)
    assert f["balances_tvl"] == ("14831637903488000000000000", "14831637.903488")
    assert Decimal(f["paper_rate"][1]) == 100
    assert Decimal(f["paper_decay"][1]) == 120_000_000
    assert Decimal(f["paper_cliff"][1]) == 2_000_000
    assert f["activity_open"] == ("0", "0")  # plain int, not scaled
    assert f["ok"] == ("true", None)
    assert not any(k.startswith("source") for k in f)


def test_summary_snapshot_stores_every_field_once(store, summary):
    assert store.insert_summary(summary) is True
    assert store.insert_summary(summary) is False
    rows = store.summaries()
    assert len(rows) == 1
    row = rows[0]
    assert row["source_block"] == 48068869
    assert row["source_ts"] == summary["source"]["at"]
    assert row["source_genesis"] == summary["source"]["genesis"]
    for name, (text, dec) in flatten_summary(summary).items():
        assert row[f"{name}_text"] == text
        assert row[name] == dec


def test_raw_responses_record_every_attempt(store):
    result = FetchResult("/query/protocol/summary", [
        Attempt(1, "t1", 503, "Service Unavailable"),
        Attempt(2, "t2", None, None, error="ReadTimeout"),
        Attempt(3, "t3", 200, '{"ok":true}'),
    ])
    store.record_fetch(result)
    rows = store.raw_responses("/query/protocol/summary")
    assert [(r["attempt"], r["http_status"]) for r in rows] == [(1, 503), (2, None), (3, 200)]
    assert list(store.iter_raw_bodies("/query/protocol/summary")) == [("t3", '{"ok":true}')]


def test_store_on_disk_reopens(tmp_path, history):
    path = tmp_path / "sub" / "t.sqlite"
    with Store(path) as s:
        s.upsert_history("1d", history["1d"])
    with Store(path) as s:
        s.upsert_history("1d", history["1d"])
        assert len(s.history("1d")) == 4
