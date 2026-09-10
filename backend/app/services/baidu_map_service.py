from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional

import requests

from app.config import get_settings
from app.models.schemas import Location

logger = logging.getLogger(__name__)


def is_baidu_qps_limit_error(payload: Any) -> bool:
    if isinstance(payload, dict):
        status = str(payload.get("status") or "")
        message = str(payload.get("message") or "")
        if status == "401" and any(token in message for token in ("并发", "配额", "限制访问")):
            return True
    text = str(payload or "").lower()
    return any(token in text for token in ("并发配额", "并发量", "qps", "rate limit", "rate_limit"))


class BaiduRateLimiter:
    """Process-wide paced request timeline for Baidu Web APIs."""

    def __init__(self, budget: float, clock=time.monotonic, sleeper=time.sleep):
        self._budget = budget
        self._clock = clock
        self._sleeper = sleeper
        self._lock = threading.Lock()
        self._next_free_at = 0.0

    @property
    def budget(self) -> float:
        return self._budget

    def wait(self) -> None:
        if self._budget <= 0:
            return
        with self._lock:
            now = self._clock()
            earliest = self._next_free_at
            self._next_free_at = max(now, earliest) + 1.0 / self._budget
        delay = earliest - now
        if delay > 0:
            self._sleeper(delay)


_rate_limiter: BaiduRateLimiter | None = None
_rate_limiter_lock = threading.Lock()


def get_baidu_rate_limiter() -> BaiduRateLimiter:
    global _rate_limiter
    budget = get_settings().baidu_qps_budget
    if _rate_limiter is None or _rate_limiter.budget != budget:
        with _rate_limiter_lock:
            if _rate_limiter is None or _rate_limiter.budget != budget:
                _rate_limiter = BaiduRateLimiter(budget)
    return _rate_limiter


def reset_baidu_rate_limiter() -> None:
    global _rate_limiter
    with _rate_limiter_lock:
        _rate_limiter = None


class BaiduMapService:
    """百度地图 Web 服务封装。API Key 为空时返回空结果。"""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.place_url = "https://api.map.baidu.com/place/v2"
        self.geocoding_url = "https://api.map.baidu.com/geocoding/v3"
        self.direction_url = "https://api.map.baidu.com/directionlite/v1"

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def _request_json(self, url: str, params: Dict[str, Any], *, operation: str) -> Dict[str, Any] | None:
        settings = get_settings()
        attempts = settings.baidu_qps_retry_attempts + 1
        for attempt in range(attempts):
            get_baidu_rate_limiter().wait()
            if attempt > 0 and settings.baidu_qps_retry_delay_seconds > 0:
                time.sleep(settings.baidu_qps_retry_delay_seconds)
            try:
                response = requests.get(url, params=params, timeout=10)
                response.raise_for_status()
                payload = response.json()
            except (requests.Timeout, requests.ConnectionError) as exc:
                if attempt + 1 < attempts:
                    logger.warning(
                        "Baidu %s transient failure; retrying attempt=%d/%d error=%s",
                        operation,
                        attempt + 1,
                        attempts,
                        exc,
                    )
                    continue
                logger.warning("Baidu %s request failed after retries: %s", operation, exc)
                return None
            except Exception as exc:
                logger.warning("Baidu %s request failed: %s", operation, exc)
                return None

            if is_baidu_qps_limit_error(payload) and attempt + 1 < attempts:
                logger.warning(
                    "Baidu %s QPS limit hit; retrying attempt=%d/%d",
                    operation,
                    attempt + 1,
                    attempts,
                )
                continue
            return payload if isinstance(payload, dict) else None
        return None

    def search_pois(
        self,
        keywords: str,
        city: str,
        tag: str | None = None,
        sort_by: str | None = None,
        offset: int = 10,
        *,
        location: Location | None = None,
        radius: int = 5000,
    ) -> List[Dict[str, Any]]:
        if not self.enabled:
            return []
        params: Dict[str, Any] = {
            "query": keywords,
            "scope": 2,
            "output": "json",
            "ak": self.api_key,
            "page_size": max(1, min(offset, 20)),
            "page_num": 0,
        }
        if location is None:
            params["region"] = city
        else:
            params["location"] = f"{location.latitude},{location.longitude}"
            params["radius"] = max(100, min(radius, 50000))
        if tag:
            params["tag"] = tag
        if sort_by:
            params["sort_name"] = sort_by
        payload = self._request_json(f"{self.place_url}/search", params, operation="POI search")
        if payload is None:
            return []
        if payload.get("status") not in (None, 0, "0"):
            logger.warning(
                "Baidu POI provider error: status=%s message=%s",
                payload.get("status"),
                payload.get("message"),
            )
            return []
        return payload.get("results", []) or []

    def get_poi_detail(self, uid: str) -> Optional[Dict[str, Any]]:
        """Return one scope=2 POI detail record by its Baidu UID."""
        if not self.enabled or not uid:
            return None
        payload = self._request_json(
            f"{self.place_url}/detail",
            {
                "uid": uid,
                "scope": 2,
                "output": "json",
                "ak": self.api_key,
            },
            operation="POI detail",
        )
        if payload is None:
            return None
        if payload.get("status") not in (None, 0, "0"):
            logger.warning(
                "Baidu POI detail provider error: status=%s message=%s",
                payload.get("status"),
                payload.get("message"),
            )
            return None
        result = payload.get("result")
        return result if isinstance(result, dict) else None

    def poi_to_restaurant(self, poi: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        location = self.parse_location(poi.get("location"))
        if location is None:
            return None
        detail_info = poi.get("detail_info") or {}
        return {
            "uid": str(poi.get("uid") or "") or None,
            "name": poi.get("name", "未命名餐厅"),
            "address": poi.get("address") or "",
            "location": location.model_dump(),
            "rating": self._to_float(detail_info.get("overall_rating")),
            "overall_rating": self._to_float(detail_info.get("overall_rating")),
            "taste_rating": self._to_float(detail_info.get("taste_rating")),
            "service_rating": self._to_float(detail_info.get("service_rating")),
            "environment_rating": self._to_float(detail_info.get("environment_rating")),
            "price": self._to_int(detail_info.get("price")),
            "shop_hours": detail_info.get("shop_hours") or "",
            "comment_num": self._to_int(detail_info.get("comment_num")),
            "distance": self._to_int(detail_info.get("distance")),
        }

    def geo_city(self, city: str) -> Optional[Location]:
        if not self.enabled:
            return None
        payload = self._request_json(
            self.geocoding_url,
            {"address": city, "output": "json", "ak": self.api_key},
            operation="geocoding",
        )
        if payload is None:
            return None
        return self.parse_location((payload.get("result") or {}).get("location"))

    def get_direction(
        self,
        origin: str,
        destination: str,
        city: str,
        mode: str = "transit",
    ) -> List[Dict[str, Any]]:
        if not self.enabled:
            return []
        origin_location = self._resolve_place_location(origin, city)
        destination_location = self._resolve_place_location(destination, city)
        if origin_location is None or destination_location is None:
            return []
        payload = self._request_json(
            f"{self.direction_url}/{mode}",
            {
                "origin": self._format_route_location(origin_location),
                "destination": self._format_route_location(destination_location),
                "ak": self.api_key,
            },
            operation="direction",
        )
        if payload is None:
            return []
        routes = (payload.get("result") or {}).get("routes", []) or []
        parsed_routes = []
        for route in routes:
            parsed_route = self._route_to_dict(route)
            if parsed_route is not None:
                parsed_routes.append(parsed_route)
        return parsed_routes

    @staticmethod
    def parse_location(raw_location: Any) -> Optional[Location]:
        if isinstance(raw_location, dict):
            longitude = raw_location.get("lng")
            latitude = raw_location.get("lat")
            if longitude is None:
                longitude = raw_location.get("longitude")
            if latitude is None:
                latitude = raw_location.get("latitude")
        elif isinstance(raw_location, str) and "," in raw_location:
            longitude, latitude = raw_location.split(",", 1)
        else:
            return None
        try:
            return Location(longitude=float(longitude), latitude=float(latitude))
        except (TypeError, ValueError):
            return None

    def _resolve_place_location(self, place: str, city: str) -> Optional[Location]:
        pois = self.search_pois(place, city, offset=1)
        if pois:
            location = self.parse_location(pois[0].get("location"))
            if location is not None:
                return location
        return self.geo_city(place)

    @staticmethod
    def _format_route_location(location: Location) -> str:
        return f"{location.latitude},{location.longitude}"

    @classmethod
    def _route_to_dict(cls, route: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        distance = cls._to_int(route.get("distance"))
        duration = cls._to_int(route.get("duration"))
        if distance is None or duration is None:
            return None
        return {
            "distance_m": distance,
            "duration_s": duration,
            "duration_text": cls._format_duration(duration),
        }

    @staticmethod
    def _format_duration(seconds: int) -> str:
        minutes = max(1, round(seconds / 60))
        hours, remaining_minutes = divmod(minutes, 60)
        if hours and remaining_minutes:
            return f"约{hours}小时{remaining_minutes}分钟"
        if hours:
            return f"约{hours}小时"
        return f"约{minutes}分钟"

    @staticmethod
    def _to_float(value: Any) -> Optional[float]:
        if value in (None, "", []):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _to_int(value: Any) -> Optional[int]:
        if value in (None, "", []):
            return None
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None
