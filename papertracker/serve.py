"""Long-running mode for a host like Railway: hourly polls plus an HTTP dashboard.

One process does both, because the SQLite file lives on a volume that only
one service can mount. A scheduler thread polls at ``minute`` past every
hour (with the daily fetches on the 00:xx UTC run, and once at start-up),
rebuilds the dashboard after each poll, and the main thread serves:

- ``/``: the dashboard
- ``/status``: the ``status`` table as plain text
- ``/healthz``: 200 while the process is up, with the last successful poll
"""

from __future__ import annotations

import json
import logging
import signal
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from . import alerts, dashboard, display, metrics, poll
from .client import PapertradeClient
from .store import Store

log = logging.getLogger(__name__)

PENDING_PAGE = b"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>PAPER Tracker</title></head>
<body style="font-family: system-ui, sans-serif; padding: 24px 16px">
<p>The first poll is still running. Reload in a minute.</p></body></html>"""


def next_run(now: datetime, minute: int) -> datetime:
    """The next ``hh:minute`` strictly after ``now``."""
    at = now.replace(minute=minute, second=0, microsecond=0)
    return at if at > now else at + timedelta(hours=1)


def run_cycle(
    db_path: Path,
    site_path: Path,
    config: Mapping[str, Any],
    *,
    daily: bool,
    client_factory: Callable[[], PapertradeClient] = PapertradeClient,
    notifier: alerts.Notifier | None = None,
) -> poll.PollReport:
    """One poll, then rebuild the dashboard."""
    with Store(db_path) as store, client_factory() as client:
        report = poll.run(store, client, config, daily=daily, notifier=notifier)
        dashboard.build(store, metrics.from_store(store, config), config, site_path)
    log.info(
        "poll%s done: %d failed, %d metric rows, %d alert(s)",
        " (daily)" if daily else "", len(report.failed), report.metric_rows, len(report.alerts),
    )
    return report


class Scheduler(threading.Thread):
    """Calls ``cycle(daily)`` once at start-up and then hourly; never dies on an error."""

    def __init__(self, cycle: Callable[[bool], Any], minute: int, stop: threading.Event,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        super().__init__(name="scheduler", daemon=True)
        self.cycle, self.minute, self.stop, self.now = cycle, minute, stop, now

    def _safe(self, daily: bool) -> None:
        try:
            self.cycle(daily)
        except Exception:  # keep polling next hour whatever went wrong
            log.exception("poll cycle failed")

    def run(self) -> None:
        self._safe(daily=True)
        while not self.stop.is_set():
            at = next_run(self.now(), self.minute)
            if self.stop.wait(max(0.0, (at - self.now()).total_seconds())):
                return
            self._safe(daily=at.hour == 0)


def make_handler(db_path: Path, site_path: Path, config: Mapping[str, Any]) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "papertracker"

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            if path == "/healthz":
                with Store(db_path) as store:
                    last = store.last_success()
                self._send(200, json.dumps({"ok": True, "last_success": last}).encode(), "application/json")
            elif path in ("/", "/index.html"):
                if site_path.exists():
                    self._send(200, site_path.read_bytes(), "text/html; charset=utf-8")
                else:
                    self._send(200, PENDING_PAGE, "text/html; charset=utf-8")
            elif path == "/status":
                with Store(db_path) as store:
                    text = display.format_status(store, metrics.from_store(store, config))
                self._send(200, text.encode(), "text/plain; charset=utf-8")
            else:
                self._send(404, b"not found\n", "text/plain; charset=utf-8")

        do_HEAD = do_GET

        def log_message(self, format: str, *args: Any) -> None:
            log.debug("%s %s", self.address_string(), format % args)

    return Handler


def serve(db_path: Path, site_path: Path, config: Mapping[str, Any], *, host: str, port: int, minute: int) -> None:
    stop = threading.Event()
    Store(db_path).close()  # create the schema before the first request
    httpd = ThreadingHTTPServer((host, port), make_handler(db_path, site_path, config))
    scheduler = Scheduler(lambda daily: run_cycle(db_path, site_path, config, daily=daily), minute, stop)

    def shutdown(signum: int, _frame: Any) -> None:
        log.warning("signal %d: shutting down", signum)
        stop.set()
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    log.warning("serving on %s:%d, polling at :%02d past each hour; data in %s", host, port, minute, db_path.parent)
    scheduler.start()
    try:
        httpd.serve_forever()
    finally:
        stop.set()
        httpd.server_close()
        scheduler.join(timeout=30)
