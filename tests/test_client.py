import random

import httpx
import pytest

from papertracker.client import (
    BACKOFF_MAX,
    FetchError,
    PapertradeClient,
    backoff_delay,
)


class Alternating:
    """Fake server whose responses alternate between two statuses, like the load balancer."""

    def __init__(self, first: int, second: int):
        self.statuses = (first, second)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        status = self.statuses[len(self.requests) % 2]
        self.requests.append(request)
        body = {"ok": True} if status == 200 else None
        return httpx.Response(status, json=body) if body else httpx.Response(status, text="Service Unavailable")


def make_client(handler, sleeps=None, **kw):
    sleeps = [] if sleeps is None else sleeps
    return PapertradeClient(
        transport=httpx.MockTransport(handler),
        sleep=sleeps.append,
        rng=random.Random(0),
        **kw,
    )


@pytest.mark.parametrize("first", [503, 200])
def test_alternating_200_503_every_call_succeeds_within_two_attempts(first):
    server = Alternating(first, 503 if first == 200 else 200)
    with make_client(server) as client:
        results = [client.summary() for _ in range(6)]
    for r in results:
        assert r.ok
        assert len(r.attempts) <= 2
        assert r.final.status == 200
        assert r.body == '{"ok":true}'


def test_every_attempt_is_recorded_with_status_and_body():
    server = Alternating(503, 200)
    with make_client(server) as client:
        r = client.history("1h")
    assert [a.status for a in r.attempts] == [503, 200]
    assert [a.number for a in r.attempts] == [1, 2]
    assert r.attempts[0].body == "Service Unavailable"
    assert r.endpoint == "/query/protocol/history?interval=1h"
    assert server.requests[0].url.params["interval"] == "1h"


def test_gives_up_after_six_attempts_and_backs_off():
    sleeps = []
    with make_client(lambda req: httpx.Response(503), sleeps) as client:
        r = client.summary()
    assert not r.ok
    assert len(r.attempts) == 6
    assert len(sleeps) == 5
    with pytest.raises(FetchError, match="6 attempt"):
        r.body


def test_retries_timeouts_and_transport_errors():
    calls = iter([httpx.ReadTimeout("slow"), httpx.ConnectError("refused"), httpx.Response(502), httpx.Response(200, text="{}")])

    def handler(request):
        item = next(calls)
        if isinstance(item, Exception):
            raise item
        return item

    with make_client(handler) as client:
        r = client.summary()
    assert r.ok
    assert [a.status for a in r.attempts] == [None, None, 502, 200]
    assert "ReadTimeout" in r.attempts[0].error


def test_4xx_is_not_retried():
    sleeps = []
    with make_client(lambda req: httpx.Response(404), sleeps) as client:
        r = client.summary()
    assert not r.ok
    assert len(r.attempts) == 1
    assert sleeps == []


def test_backoff_grows_from_half_second_to_cap_with_jitter():
    rng = random.Random(42)
    bases = [0.5, 1, 2, 4, 8, 8, 8]
    for attempt, base in enumerate(bases, start=1):
        for _ in range(50):
            d = backoff_delay(attempt, rng)
            assert base / 2 <= d <= base
            assert d <= BACKOFF_MAX


def test_rejects_unknown_interval():
    with make_client(lambda req: httpx.Response(200)) as client:
        with pytest.raises(ValueError):
            client.history("5m")
