from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import requests

from app.models.schemas import Location

logger = logging.getLogger(__name__)


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

    def search_pois(
        self,
        keywords: str,
        city: str,
        tag: str | None = None,
        sort_by: str | None = None,
        offset: int = 10,
    ) -> List[Dict[str, Any]]:
        if not self.enabled:
            return []
        params: Dict[str, Any] = {
            "query": keywords,
            "region": city,
            "scope": 2,
            "output": "json",
            "ak": self.api_key,
            "page_size": max(1, min(offset, 20)),
            "page_num": 0,
        }
        if tag:
            params["tag"] = tag
        if sort_by:
            params["sort_name"] = sort_by
        try:
            response = requests.get(f"{self.place_url}/search", params=params, timeout=10)
            response.raise_for_status()
            payload = response.json()
            return payload.get("results", []) or []
        except Exception as exc:
            logger.warning("Baidu POI request failed: %s", exc)
            return []

    def poi_to_restaurant(self, poi: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        location = self.parse_location(poi.get("location"))
        if location is None:
            return None
        detail_info = poi.get("detail_info") or {}
        return {
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
        }

    def geo_city(self, city: str) -> Optional[Location]:
        if not self.enabled:
            return None
        try:
            response = requests.get(
                self.geocoding_url,
                params={"address": city, "output": "json", "ak": self.api_key},
                timeout=10,
            )
            response.raise_for_status()
            payload = response.json()
            return self.parse_location((payload.get("result") or {}).get("location"))
        except Exception as exc:
            logger.warning("Baidu geocoding request failed: %s", exc)
            return None

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
        try:
            response = requests.get(
                f"{self.direction_url}/{mode}",
                params={
                    "origin": self._format_route_location(origin_location),
                    "destination": self._format_route_location(destination_location),
                    "ak": self.api_key,
                },
                timeout=10,
            )
            response.raise_for_status()
            payload = response.json()
            routes = (payload.get("result") or {}).get("routes", []) or []
            parsed_routes = []
            for route in routes:
                parsed_route = self._route_to_dict(route)
                if parsed_route is not None:
                    parsed_routes.append(parsed_route)
            return parsed_routes
        except Exception as exc:
            logger.warning("Baidu direction request failed: %s", exc)
            return []

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
