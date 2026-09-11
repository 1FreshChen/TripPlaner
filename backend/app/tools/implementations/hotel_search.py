from __future__ import annotations

import asyncio
from typing import Any, Dict

from app.services.amap_service import AmapService, ServiceResult
from app.services.mock_data import build_mock_hotels
from app.tools.base import BaseTool


class HotelSearchTool(BaseTool):
    name = "hotel_search"
    description = "搜索指定城市的酒店住宿信息，可按类型过滤。"
    parameters = {
        "type": "object",
        "properties": {
            "city": {"type": "string", "description": "城市名称"},
            "hotel_type": {
                "type": "string",
                "description": "酒店类型，如 '经济型酒店'、'精品酒店'、'豪华酒店'",
                "default": "经济型酒店",
            },
            "limit": {
                "type": "integer",
                "description": "返回酒店数量",
                "default": 5,
                "minimum": 1,
                "maximum": 10,
            },
        },
        "required": ["city"],
    }

    def __init__(self, amap_service: AmapService):
        self._amap = amap_service

    async def execute(
        self,
        city: str,
        hotel_type: str = "经济型酒店",
        limit: int = 5,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        normalized_limit = max(1, min(limit, 10))
        raw_result = await asyncio.to_thread(
            self._amap.search_pois,
            f"{hotel_type} 酒店",
            city,
            normalized_limit,
        )
        result = raw_result if isinstance(raw_result, ServiceResult) else ServiceResult(data=raw_result, source="legacy")
        if result.is_error:
            return {
                "hotels": [],
                "count": 0,
                "city": city,
                "success": False,
                "source": result.source,
                "fallback_from": result.fallback_from,
                "error": result.error,
            }
        hotels = [
            hotel
            for hotel in (self._amap.poi_to_hotel(poi, hotel_type) for poi in result.data)
            if hotel is not None
        ]
        source = result.source
        fallback_used = False
        if not hotels:
            hotels = build_mock_hotels(city, hotel_type, hotel_type)
            source = "mock"
            fallback_used = True
        payload = [hotel.model_dump() for hotel in hotels[:normalized_limit]]
        return {
            "hotels": payload,
            "count": len(payload),
            "city": city,
            "success": True,
            "source": source,
            "fallback_used": fallback_used,
        }
