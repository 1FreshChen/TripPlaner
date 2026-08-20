import pytest

from app.config import get_settings
from app.services import amap_service
from app.services.amap_service import (
    AmapRateLimiter,
    AmapService,
    is_qps_limit_error,
    reset_amap_rate_limiter,
)


class FakeClock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_is_qps_limit_error_detects_amap_qps_rejections():
    assert is_qps_limit_error("CUQPS_HAS_EXCEEDED_THE_LIMIT")
    assert is_qps_limit_error("Get poi detail failed: CUQPS_HAS_EXCEEDED_THE_LIMIT")
    assert is_qps_limit_error("QPS_HAS_EXCEEDED_THE_LIMIT")
    assert is_qps_limit_error({"status": "0", "infocode": "10021"})
    assert not is_qps_limit_error("INVALID_USER_KEY")
    assert not is_qps_limit_error("")
    assert not is_qps_limit_error(None)


def test_rate_limiter_spaces_calls_by_budget():
    clock = FakeClock()
    sleeps = []
    limiter = AmapRateLimiter(budget=2.0, clock=clock, sleeper=sleeps.append)

    limiter.wait(cost=3.0)
    assert sleeps == []
    limiter.wait(cost=3.0)
    assert sleeps == [1.5]
    clock.advance(1.5)
    limiter.wait(cost=1.0)
    assert sleeps == [1.5, 1.5]


def test_rate_limiter_passthrough_when_budget_is_zero():
    def fail_on_sleep(delay):
        raise AssertionError(f"unexpected sleep: {delay}")

    limiter = AmapRateLimiter(budget=0.0, clock=FakeClock(), sleeper=fail_on_sleep)
    limiter.wait(cost=5.0)
    limiter.wait(cost=0.0)


def test_search_pois_retries_qps_limit_then_succeeds(monkeypatch):
    monkeypatch.setenv("AMAP_QPS_RETRY_ATTEMPTS", "1")
    get_settings.cache_clear()
    reset_amap_rate_limiter()
    payloads = [
        {"status": "0", "info": "CUQPS_HAS_EXCEEDED_THE_LIMIT", "infocode": "10021"},
        {"status": "1", "pois": [{"id": "poi-1", "name": "Museum"}]},
    ]
    calls = []

    def fake_get(url, params, timeout):
        calls.append(url)
        return FakeResponse(payloads.pop(0))

    monkeypatch.setattr(amap_service.requests, "get", fake_get)

    result = AmapService("test-key").search_pois("museum", "TestCity")

    assert len(calls) == 2
    assert result.data == [{"id": "poi-1", "name": "Museum"}]


def test_search_pois_does_not_retry_non_qps_errors(monkeypatch):
    monkeypatch.setenv("AMAP_QPS_RETRY_ATTEMPTS", "2")
    get_settings.cache_clear()
    reset_amap_rate_limiter()
    calls = []

    def fake_get(url, params, timeout):
        calls.append(url)
        return FakeResponse({"status": "0", "info": "INVALID_USER_KEY"})

    monkeypatch.setattr(amap_service.requests, "get", fake_get)

    result = AmapService("test-key").search_pois("museum", "TestCity")

    assert len(calls) == 1
    assert result.is_error
    assert result.error_kind == "provider"


def test_search_pois_exhausts_qps_retries_then_reports_provider_error(monkeypatch):
    monkeypatch.setenv("AMAP_QPS_RETRY_ATTEMPTS", "2")
    get_settings.cache_clear()
    reset_amap_rate_limiter()
    calls = []

    def fake_get(url, params, timeout):
        calls.append(url)
        return FakeResponse({"status": "0", "info": "CUQPS_HAS_EXCEEDED_THE_LIMIT"})

    monkeypatch.setattr(amap_service.requests, "get", fake_get)

    result = AmapService("test-key").search_pois("museum", "TestCity")

    assert len(calls) == 3
    assert result.is_error
    assert result.error_kind == "provider"
    assert "CUQPS" in result.error
