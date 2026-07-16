from __future__ import annotations

import asyncio
from typing import Any, Dict

from app.services.baidu_map_service import BaiduMapService
from app.tools.base import BaseTool


class BaiduDirectionTool(BaseTool):
    name = "baidu_direction"
    description = "计算两个地点之间的公交/步行/驾车/骑行路线、距离和耗时。"
    parameters = {
        "type": "object",
        "properties": {
            "origin": {"type": "string", "description": "起点名称或地址"},
            "destination": {"type": "string", "description": "终点名称或地址"},
            "city": {"type": "string", "description": "城市名称，如 '北京'"},
            "mode": {
                "type": "string",
                "description": "路线方式，可选 driving/walking/transit/riding",
                "default": "transit",
            },
        },
        "required": ["origin", "destination", "city"],
    }
    _valid_modes = {"driving", "walking", "transit", "riding"}

    def __init__(self, baidu_service: BaiduMapService):
        self._baidu = baidu_service

    async def execute(
        self,
        origin: str,
        destination: str,
        city: str,
        mode: str = "transit",
        **kwargs: Any,
    ) -> Dict[str, Any]:
        normalized_mode = mode if mode in self._valid_modes else "transit"
        routes = await asyncio.to_thread(
            self._baidu.get_direction,
            origin,
            destination,
            city,
            mode=normalized_mode,
        )
        return {"routes": routes, "count": len(routes), "city": city, "success": True}
