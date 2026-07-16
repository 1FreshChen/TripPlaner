from __future__ import annotations

import asyncio
from typing import Any, Dict

from app.services.baidu_map_service import BaiduMapService
from app.tools.base import BaseTool


class BaiduPOISearchTool(BaseTool):
    name = "baidu_poi_search"
    description = (
        "用百度地图搜索餐厅/美食，可返回口味评分、服务评分、环境评分、人均消费、"
        "营业时间等 AMap 不具备的细节。擅长按评分排序和人均过滤，与 amap_poi_search 互补。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "keywords": {"type": "string", "description": "搜索关键词，如 '川菜'、'火锅'、'咖啡'"},
            "city": {"type": "string", "description": "城市名称，如 '北京'、'上海'"},
            "tag": {
                "type": "string",
                "description": "可选分类标签，如 '川菜'、'火锅'、'日料'",
                "default": "",
            },
            "sort_by": {
                "type": "string",
                "description": "排序字段，可选 overall_rating/taste_rating/service_rating/price",
                "default": "overall_rating",
            },
            "limit": {
                "type": "integer",
                "description": "返回结果数量，默认 10，最大 20",
                "default": 10,
                "minimum": 1,
                "maximum": 20,
            },
        },
        "required": ["keywords", "city"],
    }

    def __init__(self, baidu_service: BaiduMapService):
        self._baidu = baidu_service

    async def execute(
        self,
        keywords: str,
        city: str,
        tag: str = "",
        sort_by: str = "overall_rating",
        limit: int = 10,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        normalized_limit = max(1, min(limit, 20))
        pois = await asyncio.to_thread(
            self._baidu.search_pois,
            keywords,
            city,
            tag=tag or None,
            sort_by=sort_by or None,
            offset=normalized_limit,
        )
        restaurants = [
            restaurant
            for restaurant in (self._baidu.poi_to_restaurant(poi) for poi in pois)
            if restaurant is not None
        ]
        return {"restaurants": restaurants, "count": len(restaurants), "city": city, "success": True}
