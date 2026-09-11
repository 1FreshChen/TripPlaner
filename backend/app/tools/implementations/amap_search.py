from __future__ import annotations

import asyncio
from typing import Any, Dict

from app.services.amap_service import AmapService, ServiceResult
from app.tools.base import BaseTool


class AmapPOISearchTool(BaseTool):
    name = "amap_poi_search"
    description = "搜索指定城市的 POI（景点/餐厅/商场等），返回名称、地址、坐标、评分等信息。"
    parameters = {
        "type": "object",
        "properties": {
            "keywords": {
                "type": "string",
                "description": "搜索关键词，如 '博物馆'、'公园'、'美食街'。多个关键词用 | 分隔",
            },
            "city": {"type": "string", "description": "城市名称，如 '北京'、'上海'"},
            "offset": {
                "type": "integer",
                "description": "返回结果数量上限，默认 10，最大 25",
                "default": 10,
                "minimum": 1,
                "maximum": 25,
            },
        },
        "required": ["keywords", "city"],
    }

    def __init__(self, amap_service: AmapService):
        self._amap = amap_service

    async def execute(self, keywords: str, city: str, offset: int = 10, **kwargs: Any) -> Dict[str, Any]:
        normalized_offset = max(1, min(offset, 25))
        raw_result = await asyncio.to_thread(self._amap.search_pois, keywords, city, normalized_offset)
        result = raw_result if isinstance(raw_result, ServiceResult) else ServiceResult(data=raw_result, source="legacy")
        return {
            "pois": result.data,
            "count": len(result.data),
            "city": city,
            "success": not result.is_error,
            "source": result.source,
            "fallback_from": result.fallback_from,
            "error": result.error,
        }
