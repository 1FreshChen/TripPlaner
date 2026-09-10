from __future__ import annotations

import asyncio
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from app.config import get_settings
from app.models.schemas import Attraction, Hotel, TripPlanRequest, WeatherInfo
from app.orchestration.base import BaseAgent
from app.services.amap_service import AmapService, unwrap_service_result
from app.services.amap_mcp_service import get_amap_mcp_service
from app.services.mock_data import build_mock_attractions, build_mock_hotels, build_mock_weather

class AttractionSearchAgent(BaseAgent):
    """景点搜索专家。"""

    name = "attraction_search"

    def __init__(self, amap_service: Optional[AmapService] = None, enable_external_services: Optional[bool] = None):
        settings = get_settings()
        self.amap_service = amap_service or get_amap_mcp_service()
        self.mcp_tool = getattr(self.amap_service, "mcp_tool", None)
        self.enable_external_services = (
            settings.enable_external_services if enable_external_services is None else enable_external_services
        )

    def run(self, request: TripPlanRequest) -> List[Attraction]:
        keyword = self._keyword_from_preferences(request.preferences)
        if self.enable_external_services:
            pois = unwrap_service_result(
                self.amap_service.search_pois(keyword, request.city, offset=max(request.days * 3, 10))
            )
            attractions = [
                attraction
                for attraction in (self.amap_service.poi_to_attraction(poi, request.preferences) for poi in pois)
                if attraction is not None
            ]
            if attractions:
                return attractions[: max(request.days * 3, 6)]
        return build_mock_attractions(request.city, request.preferences, request.days)

    async def execute(self, context: Dict[str, Any]) -> List[Attraction]:
        return await asyncio.to_thread(self.run, context["request"])

    @staticmethod
    def _keyword_from_preferences(preferences: str) -> str:
        if "历史" in preferences or "文化" in preferences:
            return "博物馆 历史 景点"
        if "自然" in preferences or "风光" in preferences:
            return "公园 风景区"
        if "美食" in preferences:
            return "美食街 景点"
        return "景点"

class WeatherQueryAgent(BaseAgent):
    """天气查询专家。"""

    name = "weather_query"

    def __init__(self, amap_service: Optional[AmapService] = None, enable_external_services: Optional[bool] = None):
        settings = get_settings()
        self.amap_service = amap_service or get_amap_mcp_service()
        self.mcp_tool = getattr(self.amap_service, "mcp_tool", None)
        self.enable_external_services = (
            settings.enable_external_services if enable_external_services is None else enable_external_services
        )

    def run(self, request: TripPlanRequest) -> List[WeatherInfo]:
        if self.enable_external_services:
            weather = unwrap_service_result(self.amap_service.get_weather(request.city))
            if weather:
                matching_weather = self._select_weather_for_request(weather, request)
                if matching_weather:
                    return matching_weather
        return build_mock_weather(request.start_date, request.days)

    async def execute(self, context: Dict[str, Any]) -> List[WeatherInfo]:
        return await asyncio.to_thread(self.run, context["request"])

    @staticmethod
    def _select_weather_for_request(
        weather: List[WeatherInfo],
        request: TripPlanRequest,
    ) -> List[WeatherInfo]:
        start = date.fromisoformat(request.start_date)
        expected_dates = [
            (start + timedelta(days=offset)).isoformat()
            for offset in range(request.days)
        ]
        weather_by_date = {item.date: item for item in weather}
        if all(day in weather_by_date for day in expected_dates):
            return [weather_by_date[day] for day in expected_dates]
        return []

class HotelAgent(BaseAgent):
    """酒店推荐专家。"""

    name = "hotel_recommendation"

    def __init__(self, amap_service: Optional[AmapService] = None, enable_external_services: Optional[bool] = None):
        settings = get_settings()
        self.amap_service = amap_service or get_amap_mcp_service()
        self.mcp_tool = getattr(self.amap_service, "mcp_tool", None)
        self.enable_external_services = (
            settings.enable_external_services if enable_external_services is None else enable_external_services
        )

    def run(self, request: TripPlanRequest) -> List[Hotel]:
        if self.enable_external_services:
            pois = unwrap_service_result(
                self.amap_service.search_pois(f"{request.accommodation} 酒店", request.city, offset=5)
            )
            hotels = [
                hotel
                for hotel in (self.amap_service.poi_to_hotel(poi, request.accommodation) for poi in pois)
                if hotel is not None
            ]
            if hotels:
                return hotels
        return build_mock_hotels(request.city, request.accommodation, request.budget)

    async def execute(self, context: Dict[str, Any]) -> List[Hotel]:
        return await asyncio.to_thread(self.run, context["request"])
