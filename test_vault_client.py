# Copyright (c) 2026, qBraid Development Team
# All rights reserved.

"""Tests for the vault client's rate-limit handling.

Two behaviours are covered, both of which exist so a burst of probes slows
down instead of erroring partway through a loop:

* the sliding-window pacer that keeps requests under the server's bucket, and
* the 429 backoff that reads ``Retry-After`` off the failing response.

Neither talks to the network. The pacer is driven through injected clocks so
the window arithmetic is asserted exactly rather than by sleeping, and the
backoff is driven by a fake session that raises the same exception shape
``QbraidSession`` does (``RequestsApiError`` raised ``from`` a ``requests``
error, with the response reachable on ``__cause__``).

Run with: pytest test_vault_client.py
"""

import pytest

import vault_client
from vault_client import _parse_retry_after, _RateLimiter, _retry_after_seconds


class FakeClock:
    """Monotonic clock whose ``sleep`` jumps time instead of spending it."""

    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(vault_client.time, "monotonic", fake.monotonic)
    monkeypatch.setattr(vault_client.time, "sleep", fake.sleep)
    return fake


class TestRateLimiter:
    def test_lets_a_burst_under_the_limit_through_untouched(self, clock):
        limiter = _RateLimiter(max_requests=3, window=60.0)
        for _ in range(3):
            limiter.acquire()
        assert clock.slept == []

    def test_waits_only_until_the_oldest_request_ages_out(self, clock):
        limiter = _RateLimiter(max_requests=3, window=60.0)
        limiter.acquire()  # t=1000
        clock.now += 10
        limiter.acquire()  # t=1010
        clock.now += 10
        limiter.acquire()  # t=1020
        clock.now += 5  # t=1025, bucket full

        limiter.acquire()

        # The oldest (t=1000) leaves the window at t=1060, i.e. in 35s. A
        # fixed-bucket limiter would have waited the full 60.
        assert clock.slept == [35.0]

    def test_a_burst_after_a_quiet_spell_is_not_slowed(self, clock):
        limiter = _RateLimiter(max_requests=2, window=60.0)
        limiter.acquire()
        limiter.acquire()
        clock.now += 61  # everything ages out

        limiter.acquire()
        limiter.acquire()

        assert clock.slept == []

    def test_sustained_calls_settle_into_the_allowed_rate(self, clock):
        limiter = _RateLimiter(max_requests=5, window=60.0)
        for _ in range(15):
            limiter.acquire()
        # 15 requests at 5 per 60s: the first 5 are free, the rest pace out.
        elapsed = clock.now - 1000.0
        assert elapsed == pytest.approx(120.0)


class TestParseRetryAfter:
    def test_reads_the_delay_seconds_form(self):
        assert _parse_retry_after("60") == 60.0

    def test_reads_the_http_date_form(self, monkeypatch):
        monkeypatch.setattr(vault_client.time, "time", lambda: 0.0)
        # 1970-01-01T00:00:30Z, i.e. 30 seconds after the patched "now".
        assert _parse_retry_after("Thu, 01 Jan 1970 00:00:30 GMT") == pytest.approx(30.0)

    def test_a_date_in_the_past_waits_zero_rather_than_going_negative(
        self, monkeypatch
    ):
        monkeypatch.setattr(vault_client.time, "time", lambda: 1_000_000.0)
        assert _parse_retry_after("Thu, 01 Jan 1970 00:00:30 GMT") == 0.0

    @pytest.mark.parametrize("header", ["", "   ", "soon", "not-a-date"])
    def test_falls_back_to_the_window_when_the_hint_is_unusable(self, header):
        # A 429 is a real signal even when its hint is not; waiting the full
        # window is the safe direction.
        assert _parse_retry_after(header) == vault_client._WINDOW_SECONDS

    def test_caps_an_absurd_hint(self):
        # A misconfigured server must not be able to hang the caller.
        assert _parse_retry_after("999999") == vault_client._MAX_BACKOFF_SECONDS


class FakeResponse:
    def __init__(self, status_code, headers=None, payload=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._payload = payload or {"data": {"ok": True}}

    def json(self):
        return self._payload


class FakeRequestsError(Exception):
    """Stands in for ``requests.HTTPError``: carries the response."""

    def __init__(self, response):
        super().__init__("HTTP error")
        self.response = response


class FakeApiError(Exception):
    """Stands in for ``RequestsApiError``, which drops the response."""


def rate_limited(retry_after="60"):
    """The exception shape QbraidSession raises for a 429."""
    cause = FakeRequestsError(FakeResponse(429, {"Retry-After": retry_after}))
    err = FakeApiError("Too many requests.")
    err.__cause__ = cause
    return err


class TestRetryAfterSeconds:
    def test_finds_the_hint_through_the_cause_chain(self):
        # The response is not on the raised exception -- it survives only on
        # __cause__, which is why the walk exists.
        assert _retry_after_seconds(rate_limited("45")) == 45.0

    def test_returns_none_for_a_non_429(self):
        err = FakeApiError("Bad request.")
        err.__cause__ = FakeRequestsError(FakeResponse(400))
        assert _retry_after_seconds(err) is None

    def test_returns_none_when_there_is_no_response_at_all(self):
        assert _retry_after_seconds(ValueError("boom")) is None

    def test_terminates_on_a_self_referential_cause_chain(self):
        err = FakeApiError("looping")
        err.__cause__ = err
        assert _retry_after_seconds(err) is None


class FakeSession:
    """Session that fails with a 429 a set number of times, then succeeds."""

    def __init__(self, failures=0, retry_after="60"):
        self.remaining_failures = failures
        self.retry_after = retry_after
        self.calls = 0

    def _respond(self):
        self.calls += 1
        if self.remaining_failures > 0:
            self.remaining_failures -= 1
            raise rate_limited(self.retry_after)
        return FakeResponse(200)

    def get(self, *args, **kwargs):
        return self._respond()

    def post(self, *args, **kwargs):
        return self._respond()


def make_client(session):
    client = vault_client.VaultClient.__new__(vault_client.VaultClient)
    client._session = session
    client._pace = True
    client._action_limiter = _RateLimiter(30)
    client._read_limiter = _RateLimiter(60)
    return client


class TestSendBackoff:
    def test_waits_out_a_429_and_succeeds(self, clock, capsys):
        session = FakeSession(failures=1, retry_after="60")
        client = make_client(session)

        result = client.state()

        assert result == {"ok": True}
        assert session.calls == 2
        assert clock.slept == [60.0]
        # The wait is announced, so a pause never looks like a hang.
        assert "waiting 60s" in capsys.readouterr().out

    def test_gives_up_after_the_retry_budget_and_re_raises(self, clock):
        session = FakeSession(failures=99)
        client = make_client(session)

        with pytest.raises(FakeApiError):
            client.state()

        # Initial attempt plus _MAX_RETRIES retries.
        assert session.calls == vault_client.VaultClient._MAX_RETRIES + 1

    def test_does_not_retry_a_non_429(self, clock):
        session = FakeSession()

        def boom(*args, **kwargs):
            session.calls += 1
            err = FakeApiError("Bad request.")
            err.__cause__ = FakeRequestsError(FakeResponse(400))
            raise err

        session.get = boom
        client = make_client(session)

        with pytest.raises(FakeApiError):
            client.state()

        assert session.calls == 1
        assert clock.slept == []

    def test_reads_and_actions_use_separate_buckets(self, clock):
        # The server keys them separately: polling the leaderboard must not
        # eat into the probe/attack allowance.
        client = make_client(FakeSession())
        for _ in range(30):
            client._read_limiter.acquire()
        client._action_limiter.acquire()
        assert clock.slept == []
