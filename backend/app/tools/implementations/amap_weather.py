from __future__ import annotations

import asyncio
from typing import Any, Dict

from app.services.amap_service import AmapService
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
        weather = await asyncio.to_thread(self._amap.get_weather, city)
        forecasts = [item.model_dump() for item in weather]
        return {"weather": forecasts, "count": len(forecasts), "city": city, "success": True}
