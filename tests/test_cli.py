import re
from datetime import datetime, timezone

from papertracker import dashboard, display, metrics
from papertracker.__main__ import main
from papertracker.display import format_status
from papertracker.config import load_config
from papertracker.semantics import classify_all
from papertracker.store import Store


def loaded_store(history, summary):
    store = Store(":memory:")
    store.upsert_history("1h", history["1h"])
    store.insert_summary(summary)
    _, cols = store.history_series("1h")
    store.save_semantics(classify_all(cols).values())
    return store


def test_status_shows_every_metric_and_not_started(history, summary):
    store = loaded_store(history, summary)
    out = format_status(store, metrics.from_store(store, load_config()), datetime(2026, 10, 9, 10, tzinfo=timezone.utc))
    for label, _ in display.LABELS.values():
        assert label in out
    assert "n/a — not started" in out
    assert "$0.00132" in out
    assert "Latest closed hour: 2026-10-09 08:00 UTC to 09:00 UTC" in out
    assert "Data age:" in out


def test_status_with_empty_db(tmp_path, capsys):
    assert main(["--db", str(tmp_path / "x.sqlite"), "status"]) == 0
    assert "No closed hours stored yet" in capsys.readouterr().out


def test_dashboard_is_one_self_contained_file(tmp_path, history, summary):
    store = loaded_store(history, summary)
    out = dashboard.build(store, metrics.from_store(store, load_config()), load_config(), tmp_path / "site" / "index.html")
    html = out.read_text()
    assert list((tmp_path / "site").iterdir()) == [out]
    assert not re.search(r'(src|href)=["\']', html)  # nothing loaded from elsewhere
    assert "<style>" in html and "<script>" in html
    for tile in ("Live ratio, last 24 h", "Payback", "Cost per PAPER", "Reward per staked PAPER per day"):
        assert tile in html
    assert html.count('class="empty"') == 4  # every chart's series is not started yet
    assert "TVL" in html and "HTTP, last 24 h: no attempts" in html


def test_dashboard_draws_charts_when_data_exists(tmp_path):
    n = 60
    ts = [1_791_324_000_000 + i * 3_600_000 for i in range(n)]
    cols = {"stakingRewards": [10 * i for i in range(n)], "paperStaked": [1000] * n,
            "paperSupply": [1000 + 5 * i for i in range(n)]}
    calc = metrics.Calculator(ts, cols, config={**load_config(), "measuredCostPerPaper": "1"})
    with Store(":memory:") as store:
        html = dashboard.render(dashboard.gather(store, calc, load_config()))
    assert html.count("<svg") == 4
    assert 'class="empty"' not in html
    assert "never" in html  # no poll recorded in this store


def test_nice_ticks():
    assert dashboard.nice_ticks(0, 87) == [0, 25, 50, 75, 100]
    assert dashboard.nice_ticks(0, 0) == [0, 0.25, 0.5, 0.75, 1.0]


def test_format_small_usd():
    from decimal import Decimal
    assert display.fmt_number(Decimal("0.001320635"), "usd_small") == "$0.00132"
    assert display.fmt_number(Decimal("0.0000123"), "usd_small") == "$0.0000123"
    assert display.fmt_number(Decimal("2.4"), "usd_small") == "$2.4000"


def test_dashboard_footer_shows_503_rate(history, summary):
    from papertracker.client import Attempt, FetchResult
    store = loaded_store(history, summary)
    now = datetime.now(timezone.utc)
    store.record_fetch(FetchResult("/s", [Attempt(1, now.isoformat(), 503, ""), Attempt(2, now.isoformat(), 200, "{}")]))
    html = dashboard.render(dashboard.gather(store, metrics.from_store(store, load_config()), load_config(), now))
    assert "2 attempts, 1 returned 5xx (50.0%)" in html
    assert "Last successful poll: 0 s ago" in html


def test_tick_labels_never_scientific():
    assert dashboard._tick_label(10000, "pct") == "10,000%"
    assert dashboard._tick_label(2.5, "amount") == "2.5"
    assert dashboard._tick_label(0.0025, "usd") == "$0.0025"
    assert dashboard._tick_label(0, "usd") == "$0"
    assert dashboard._tick_label(-1500, "usd") == "-$1,500"
