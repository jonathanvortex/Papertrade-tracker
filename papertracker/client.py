"""HTTP client for Papertrade's public dashboard endpoints, with retry.

Read-only: plain GETs against the public query endpoints, nothing else.

Both endpoints alternate between 200 and 503 (SPEC 2.3), so every call is
retried on 5xx, timeouts and transport errors: up to 6 attempts with
exponential backoff from 0.5 s to 8 s plus jitter. Every attempt, failed or
not, is recorded so callers can store the raw response and log the 503 rate.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

import httpx

log = logging.getLogger(__name__)

BASE_URL = "https://exchange.papertrade.xyz"
SUMMARY_PATH = "/query/protocol/summary"
HISTORY_PATH = "/query/protocol/history"
INTERVALS = ("1m", "1h", "1d")

MAX_ATTEMPTS = 6
BACKOFF_START = 0.5
BACKOFF_MAX = 8.0
TIMEOUT = 20.0


@dataclass
class Attempt:
    """One HTTP attempt. ``status`` is None when no response arrived."""

    number: int
    fetched_at: str
    status: int | None
    body: str | None
    error: str | None = None


@dataclass
class FetchResult:
    endpoint: str
    attempts: list[Attempt] = field(default_factory=list)

    @property
    def final(self) -> Attempt:
        return self.attempts[-1]

    @property
    def ok(self) -> bool:
        return bool(self.attempts) and self.final.status == 200

    @property
    def body(self) -> str:
        if not self.ok:
            raise FetchError(self)
        return self.final.body or ""


class FetchError(Exception):
    def __init__(self, result: FetchResult):
        self.result = result
        last = result.final if result.attempts else None
        detail = f"status {last.status}" if last and last.status else (last.error if last else "no attempts")
        super().__init__(f"{result.endpoint}: failed after {len(result.attempts)} attempt(s), last {detail}")


def backoff_delay(attempt: int, rng: random.Random | None = None) -> float:
    """Delay before retry number ``attempt`` (1-based): 0.5, 1, 2, 4, 8, 8 ... with jitter.

    Jitter draws uniformly from [base/2, base] so retries from several
    pollers don't line up, while the delay never exceeds the cap.
    """
    base = min(BACKOFF_MAX, BACKOFF_START * 2 ** (attempt - 1))
    return (rng or random).uniform(base / 2, base)


def _retryable(status: int) -> bool:
    return status >= 500


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class PapertradeClient:
    def __init__(
        self,
        base_url: str = BASE_URL,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = TIMEOUT,
        max_attempts: int = MAX_ATTEMPTS,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ):
        self._http = httpx.Client(
            base_url=base_url,
            transport=transport,
            timeout=timeout,
            headers={"Accept": "application/json", "User-Agent": "papertracker/0.1"},
        )
        self.max_attempts = max_attempts
        self._sleep = sleep
        self._rng = rng

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> PapertradeClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def get(self, path: str, params: dict[str, str] | None = None) -> FetchResult:
        """GET ``path`` with retry. Returns every attempt; check ``.ok`` or read ``.body``."""
        endpoint = path + (f"?{httpx.QueryParams(params)}" if params else "")
        result = FetchResult(endpoint=endpoint)
        for n in range(1, self.max_attempts + 1):
            fetched_at = _now()
            try:
                resp = self._http.get(path, params=params)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                attempt = Attempt(n, fetched_at, None, None, error=f"{type(e).__name__}: {e}")
            else:
                attempt = Attempt(n, fetched_at, resp.status_code, resp.text)
            result.attempts.append(attempt)
            log.info(
                "GET %s attempt %d/%d -> %s",
                endpoint, n, self.max_attempts, attempt.status if attempt.status is not None else attempt.error,
            )
            if attempt.status is not None and not _retryable(attempt.status):
                return result
            if n < self.max_attempts:
                self._sleep(backoff_delay(n, self._rng))
        log.warning("GET %s gave up after %d attempts", endpoint, self.max_attempts)
        return result

    def summary(self) -> FetchResult:
        return self.get(SUMMARY_PATH)

    def history(self, interval: str) -> FetchResult:
        if interval not in INTERVALS:
            raise ValueError(f"interval must be one of {INTERVALS}, got {interval!r}")
        return self.get(HISTORY_PATH, {"interval": interval})
