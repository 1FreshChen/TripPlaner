import asyncio
import sys

import pytest

from app.api.middlewares.rate_limit import limiter


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
