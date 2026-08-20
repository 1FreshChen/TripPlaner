from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Generic, List, Literal, Optional, TypeVar

import requests

from app.config import get_settings
from app.models.schemas import Attraction, Hotel, Location, WeatherInfo

logger = logging.getLogger(__name__)
T = TypeVar("T")
ServiceErrorKind = Literal["configuration", "timeout", "connection", "provider", "unexpected"]


@dataclass
class ServiceResult(Generic[T]):
    """Structured external-service result that preserves failure provenance."""

    data: List[T] = field(default_factory=list)
    error: str | None = None
    error_kind: ServiceErrorKind | None = None
    source: str = "amap_http"
    fallback_from: str | None = None

    @property
    def is_error(self) -> bool:
        return self.error is not None

    @property
    def is_empty(self) -> bool:
        return not self.data and self.error is None


class ExternalServiceError(RuntimeError):
    pass


def unwrap_service_result(result: ServiceResult[T] | List[T] | None) -> List[T]:
    """Return data or raise a retry-compatible exception for an external failure."""
    if result is None:
        return []
    if isinstance(result, list):
        return result
    if not result.is_error:
        return result.data
    if result.error_kind == "timeout":
        raise TimeoutError(result.error)
    if result.error_kind == "connection":
        raise ConnectionError(result.error)
    raise ExternalServiceError(result.error or "External service failed")


def is_qps_limit_error(payload: Any) -> bool:
    """Detect AMap QPS quota rejections (error 10021) in payloads or raised errors."""
    text = str(payload or "").lower()
    return any(token in text for token in ("cuqps", "qps", "10021", "rate limit", "rate_limit"))


class AmapRateLimiter:
    """Process-local token bucket that paces AMap calls under the key's QPS quota.

    Calls reserve ``cost / budget`` seconds of the shared time line, so bursts are
    spread across workers without any cross-process coordination. A budget of 0
    disables pacing entirely.
    """

    def __init__(self, budget: float, clock=time.monotonic, sleeper=time.sleep):
        self._budget = budget
        self._clock = clock
        self._sleeper = sleeper
        self._lock = threading.Lock()
        self._next_free_at = 0.0

    @property
    def budget(self) -> float:
        return self._budget

    def wait(self, cost: float = 1.0) -> None:
        if self._budget <= 0 or cost <= 0:
            return
        with self._lock:
            now = self._clock()
            earliest = self._next_free_at
            self._next_free_at = max(now, earliest) + cost / self._budget
        delay = earliest - now
        if delay > 0:
            self._sleeper(delay)


_rate_limiter: Optional[AmapRateLimiter] = None
_rate_limiter_lock = threading.Lock()


def get_amap_rate_limiter() -> AmapRateLimiter:
    global _rate_limiter
    budget = get_settings().amap_qps_budget
    if _rate_limiter is None or _rate_limiter.budget != budget:
        with _rate_limiter_lock:
            if _rate_limiter is None or _rate_limiter.budget != budget:
                _rate_limiter = AmapRateLimiter(budget)
    return _rate_limiter


def reset_amap_rate_limiter() -> None:
    global _rate_limiter
    with _rate_limiter_lock:
        _rate_limiter = None


class AmapService:
    """高德地图 Web 服务封装。

    调用结果会保留来源和错误，上层 Agent 决定是否重试或使用 fallback。
    """

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = "https://restapi.amap.com/v3"

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def _request_json(self, path: str, params: Dict[str, Any]) -> Any:
        settings = get_settings()
        last_error: Optional[Exception] = None
        for attempt in range(settings.amap_qps_retry_attempts + 1):
            get_amap_rate_limiter().wait()
            if attempt > 0:
                time.sleep(settings.amap_qps_retry_delay_seconds)
            try:
                response = requests.get(
                    f"{self.base_url}{path}",
                    params=params,
                    timeout=10,
                )
                response.raise_for_status()
                payload = response.json()
            except Exception as exc:
                last_error = exc
                break
            if is_qps_limit_error(payload) and attempt < settings.amap_qps_retry_attempts:
                last_error = ExternalServiceError(f"AMap QPS limit exceeded: {payload}")
                logger.warning("Amap QPS limit hit; retrying %s: %s", path, payload)
                continue
            return payload
        if last_error is not None:
            raise last_error
        raise ExternalServiceError("AMap request failed")

    def search_pois(self, keywords: str, city: str, offset: int = 10) -> ServiceResult[Dict]:
        if not self.enabled:
            return ServiceResult(
                error="AMap API key is not configured",
                error_kind="configuration",
            )
        try:
            payload = self._request_json(
                "/place/text",
                {
                    "keywords": keywords,
                    "city": city,
                    "key": self.api_key,
                    "output": "json",
                    "offset": offset,
                    "page": 1,
                },
            )
            if payload.get("status") != "1":
                logger.warning("Amap POI search failed: %s", payload)
                return ServiceResult(
                    error=f"AMap API error: {payload.get('info') or 'unknown error'}",
                    error_kind="provider",
                )
            return ServiceResult(data=payload.get("pois", []))
        except requests.Timeout as exc:
            logger.warning("Amap POI request timed out: %s", exc)
            return ServiceResult(error="AMap API timeout", error_kind="timeout")
        except requests.ConnectionError as exc:
            logger.warning("Amap POI connection failed: %s", exc)
            return ServiceResult(error="AMap API connection failed", error_kind="connection")
        except Exception as exc:
            logger.warning("Amap POI request failed: %s", exc)
            return ServiceResult(
                error=f"AMap API unexpected error: {exc}",
                error_kind="unexpected",
            )

    def get_weather(self, city: str) -> ServiceResult[WeatherInfo]:
        if not self.enabled:
            return ServiceResult(
                error="AMap API key is not configured",
                error_kind="configuration",
            )
        try:
            payload = self._request_json(
                "/weather/weatherInfo",
                {"city": city, "key": self.api_key, "extensions": "all", "output": "json"},
            )
            if payload.get("status") not in (None, "1"):
                logger.warning("Amap weather query failed: %s", payload)
                return ServiceResult(
                    error=f"AMap API error: {payload.get('info') or 'unknown error'}",
                    error_kind="provider",
                )
            forecasts = payload.get("forecasts", [])
            if not forecasts:
                return ServiceResult()
            casts = forecasts[0].get("casts", [])
            return ServiceResult(
                data=[
                    WeatherInfo(
                        date=item.get("date", ""),
                        day_weather=item.get("dayweather", "未知"),
                        night_weather=item.get("nightweather", "未知"),
                        day_temp=item.get("daytemp", 0),
                        night_temp=item.get("nighttemp", 0),
                        wind_direction=item.get("daywind", "未知"),
                        wind_power=item.get("daypower", "未知"),
                    )
                    for item in casts
                ]
            )
        except requests.Timeout as exc:
            logger.warning("Amap weather request timed out: %s", exc)
            return ServiceResult(error="AMap API timeout", error_kind="timeout")
        except requests.ConnectionError as exc:
            logger.warning("Amap weather connection failed: %s", exc)
            return ServiceResult(error="AMap API connection failed", error_kind="connection")
        except Exception as exc:
            logger.warning("Amap weather request failed: %s", exc)
            return ServiceResult(
                error=f"AMap API unexpected error: {exc}",
                error_kind="unexpected",
            )

    @staticmethod
    def parse_location(raw_location: Optional[str]) -> Optional[Location]:
        if not raw_location or "," not in raw_location:
            return None
        try:
            longitude, latitude = raw_location.split(",", 1)
            return Location(longitude=float(longitude), latitude=float(latitude))
        except ValueError:
            return None

    @staticmethod
    def _safe_biz_ext(poi: Dict) -> Dict:
        biz_ext = poi.get("biz_ext") or {}
        if isinstance(biz_ext, list):
            return {}
        return biz_ext

    def poi_to_attraction(self, poi: Dict, preferences: str) -> Optional[Attraction]:
        location = self.parse_location(poi.get("location"))
        if not location:
            return None
        rating = self._safe_biz_ext(poi).get("rating")
        return Attraction(
            name=poi.get("name", "未命名景点"),
            address=poi.get("address") or poi.get("pname") or "",
            location=location,
            visit_duration=120,
            description=f"根据{preferences}偏好从高德地图搜索得到的目的地。",
            category=poi.get("type", "景点"),
            rating=float(rating) if rating not in (None, "", []) else None,
            ticket_price=0,
        )

    def poi_to_hotel(self, poi: Dict, accommodation: str) -> Optional[Hotel]:
        location = self.parse_location(poi.get("location"))
        if not location:
            return None
        rating = self._safe_biz_ext(poi).get("rating")
        return Hotel(
            name=poi.get("name", "未命名酒店"),
            address=poi.get("address") or "",
            location=location,
            price_range="以实际预订平台为准",
            rating=str(rating or ""),
            distance="以地图路线为准",
            type=accommodation,
            estimated_cost=0,
        )
