import copy
import json
import random

import httpx
import pytest

from papertracker import alerts, poll
from papertracker.client import PapertradeClient
from papertracker.config import load_config
from papertracker.store import Store

from .conftest import FIXTURES


class FakeServer:
    """Serves the saved fixtures, alternating 503 and 200 like the real load balancer."""

    def __init__(self, summary=None):
        self.n = 0
        self.summary = summary

    def __call__(self, request):
        self.n += 1
        if self.n % 2:
            return httpx.Response(503, text="Service Unavailable")
        if request.url.path.endswith("/summary"):
            if self.summary is not None:
                return httpx.Response(200, json=self.summary)
            return httpx.Response(200, text=(FIXTURES / "summary.json").read_text())
        interval = request.url.params["interval"]
        return httpx.Response(200, text=(FIXTURES / f"history_{interval}.json").read_text())


def client(server):
    return PapertradeClient(transport=httpx.MockTransport(server), sleep=lambda s: None, rng=random.Random(0))


@pytest.fixture
def quiet():
    return alerts.Notifier("", "")


def test_daily_poll_stores_everything_and_rerun_adds_no_rows(quiet):
    config = load_config()
    with Store(":memory:") as store:
        report = poll.run(store, client(FakeServer()), config, daily=True, notifier=quiet)
        assert report.ok
        assert all(statuses == [503, 200] for statuses in report.fetched.values())
        assert len(report.fetched) == 4
        counts = {i: len(store.history(i)) for i in ("1m", "1h", "1d")}
        assert counts == {"1m": 1440, "1h": 60, "1d": 4}
        assert len(store.summaries()) == 1
        assert len(store.metrics("24h")) == 60
        assert store.semantics()["tvl"] == "level"
        assert report.totals_mismatches == []
        stats = store.attempt_stats()
        assert stats == {"total": 8, "ok": 4, "server_errors": 4, "no_response": 0}

        poll.run(store, client(FakeServer()), config, daily=True, notifier=quiet)
        assert {i: len(store.history(i)) for i in ("1m", "1h", "1d")} == counts
        assert len(store.summaries()) == 1
        assert len(store.metrics("24h")) == 60


def test_hourly_poll_fetches_summary_and_1h_only(quiet):
    with Store(":memory:") as store:
        report = poll.run(store, client(FakeServer()), load_config(), notifier=quiet)
        assert set(report.fetched) == {"/query/protocol/summary", "/query/protocol/history?interval=1h"}
        assert store.history("1m") == []


def test_poll_alerts_on_emission_change(quiet, summary):
    config = load_config()
    with Store(":memory:") as store:
        assert poll.run(store, client(FakeServer()), config, notifier=quiet).alerts == []
        changed = copy.deepcopy(summary)
        changed["source"]["at"] += 3_600_000
        changed["source"]["block"] = "48070000"
        changed["paper"]["decay"] = str(100_000_000 * 10**18)
        report = poll.run(store, client(FakeServer(changed)), config, notifier=quiet)
        assert [a.kind for a in report.alerts] == ["emission_setting"]
        assert store.alerts()[0]["kind"] == "emission_setting"


def test_failed_endpoint_still_stores_the_rest(quiet):
    def server(request):
        if request.url.path.endswith("/summary"):
            return httpx.Response(503)
        return httpx.Response(200, text=(FIXTURES / "history_1h.json").read_text())

    with Store(":memory:") as store:
        report = poll.run(store, client(server), load_config(), notifier=quiet)
        assert report.failed == ["/query/protocol/summary"]
        assert len(store.history("1h")) == 60
        assert len(store.raw_responses("/query/protocol/summary")) == 6
        assert store.metrics("24h")[-1]["cost_per_paper"] is None  # no summary: cost is n/a, no crash
