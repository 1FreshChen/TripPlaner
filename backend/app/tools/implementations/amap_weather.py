from __future__ import annotations

import asyncio
from typing import Any, Dict

from app.services.amap_service import AmapService, ServiceResult
from app.tools.base import BaseTool


class AmapWeatherTool(BaseTool):
    name = "amap_weather"
    description = "查询指定城市未来数天的天气预报，包含白天/夜间天气、温度、风力风向。"
    parameters = {
        "type": "object",
        "properties": {"city": {"type": "string", "description": "城市名称，如 '杭州'"}},
        "required": ["city"],
    }

    def __init__(self, amap_service: AmapService):
        self._amap = amap_service

    async def execute(self, city: str, **kwargs: Any) -> Dict[str, Any]:
        raw_result = await asyncio.to_thread(self._amap.get_weather, city)
        result = raw_result if isinstance(raw_result, ServiceResult) else ServiceResult(data=raw_result, source="legacy")
        forecasts = [item.model_dump() for item in result.data]
        return {
            "weather": forecasts,
            "count": len(forecasts),
            "city": city,
            "success": not result.is_error,
            "source": result.source,
            "fallback_from": result.fallback_from,
            "error": result.error,
        }
