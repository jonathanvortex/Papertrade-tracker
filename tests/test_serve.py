import json
import threading
import urllib.request
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer

import pytest

from papertracker import alerts, serve
from papertracker.config import data_dir, load_config

from .test_poll import FakeServer, client


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def test_next_run():
    assert serve.next_run(utc(2026, 10, 9, 9, 1), 2) == utc(2026, 10, 9, 9, 2)
    assert serve.next_run(utc(2026, 10, 9, 9, 2), 2) == utc(2026, 10, 9, 10, 2)
    assert serve.next_run(utc(2026, 10, 9, 23, 30), 2) == utc(2026, 10, 10, 0, 2)


def test_scheduler_runs_daily_at_start_then_hourly_with_daily_after_midnight():
    calls = []
    stop = threading.Event()
    clock = iter([utc(2026, 10, 9, 23, 30), utc(2026, 10, 9, 23, 30),   # -> 00:02 next day
                  utc(2026, 10, 10, 0, 5), utc(2026, 10, 10, 0, 5)])     # -> 01:02

    def cycle(daily):
        calls.append(daily)
        if len(calls) == 3:
            stop.set()

    sched = serve.Scheduler(cycle, 2, stop, now=lambda: next(clock))
    sched.stop.wait = lambda timeout: stop.is_set()  # don't actually sleep
    sched.run()
    assert calls == [True, True, False]


def test_scheduler_survives_a_failing_cycle():
    calls = []
    stop = threading.Event()

    def cycle(daily):
        calls.append(daily)
        if len(calls) == 2:
            stop.set()
        raise RuntimeError("boom")

    sched = serve.Scheduler(cycle, 2, stop, now=lambda: utc(2026, 10, 9, 9, 0))
    sched.stop.wait = lambda timeout: stop.is_set()
    sched.run()
    assert len(calls) == 2


def test_run_cycle_polls_and_writes_dashboard(tmp_path):
    db, site = tmp_path / "db.sqlite", tmp_path / "site" / "index.html"
    report = serve.run_cycle(db, site, load_config(), daily=True,
                             client_factory=lambda: client(FakeServer()), notifier=alerts.Notifier("", ""))
    assert report.ok
    assert "Is minting PAPER paying for itself?" in site.read_text()


@pytest.fixture
def http(tmp_path):
    db, site = tmp_path / "db.sqlite", tmp_path / "site" / "index.html"
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), serve.make_handler(db, site, load_config()))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def get(path):
        try:
            with urllib.request.urlopen(base + path, timeout=10) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    yield get, db, site
    httpd.shutdown()
    httpd.server_close()


def test_http_before_first_poll(http):
    get, _, _ = http
    assert get("/healthz") == (200, json.dumps({"ok": True, "last_success": None}))
    status, body = get("/")
    assert status == 200 and "first poll is still running" in body
    assert get("/nope")[0] == 404


def test_http_after_a_poll(http):
    get, db, site = http
    serve.run_cycle(db, site, load_config(), daily=False,
                    client_factory=lambda: client(FakeServer()), notifier=alerts.Notifier("", ""))
    status, body = get("/healthz")
    assert status == 200 and json.loads(body)["last_success"] is not None
    assert "Is minting PAPER paying for itself?" in get("/")[1]
    status, text = get("/status")
    assert status == 200 and "Latest closed hour" in text and "n/a — not started" in text


def test_data_dir_env(monkeypatch, tmp_path):
    monkeypatch.delenv("PAPERTRACKER_DATA_DIR", raising=False)
    monkeypatch.delenv("RAILWAY_VOLUME_MOUNT_PATH", raising=False)
    assert data_dir().name == "data"
    monkeypatch.setenv("RAILWAY_VOLUME_MOUNT_PATH", "/vol")
    assert str(data_dir()) == "/vol"
    monkeypatch.setenv("PAPERTRACKER_DATA_DIR", str(tmp_path))
    assert data_dir() == tmp_path
