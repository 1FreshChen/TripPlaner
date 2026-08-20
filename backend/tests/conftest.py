import asyncio
import sys

import pytest

from app.api.middlewares.rate_limit import limiter
from app.config import get_settings
from app.services.amap_service import reset_amap_rate_limiter


if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def reset_limiter():
    if hasattr(limiter, "reset"):
        limiter.reset()
        return
    storage = getattr(getattr(limiter, "_limiter", None), "storage", None)
    if hasattr(storage, "reset"):
        storage.reset()


@pytest.fixture(autouse=True)
def reset_rate_limiter_between_tests():
    reset_limiter()
    yield
    reset_limiter()


@pytest.fixture(autouse=True)
def disable_amap_qps_pacing_between_tests(monkeypatch):
    monkeypatch.setenv("AMAP_QPS_BUDGET", "0")
    monkeypatch.setenv("AMAP_QPS_RETRY_DELAY_SECONDS", "0")
    get_settings.cache_clear()
    reset_amap_rate_limiter()
    yield
    get_settings.cache_clear()
    reset_amap_rate_limiter()
