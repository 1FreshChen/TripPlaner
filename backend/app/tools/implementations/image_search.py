from __future__ import annotations

import asyncio
from typing import Any, Dict

from app.services.unsplash_service import UnsplashService
from app.tools.base import BaseTool


class UnsplashImageTool(BaseTool):
    name = "unsplash_image"
    description = "搜索旅游景点相关的图片，用于行程可视化展示。"
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "图片搜索关键词（景点名称 + 城市名效果最佳）"},
            "count": {
                "type": "integer",
                "description": "返回图片数量",
                "default": 1,
                "minimum": 1,
                "maximum": 5,
            },
        },
        "required": ["query"],
    }

    def __init__(self, unsplash_service: UnsplashService):
        self._unsplash = unsplash_service

    async def execute(self, query: str, count: int = 1, **kwargs: Any) -> Dict[str, Any]:
        normalized_count = max(1, min(count, 5))
        images = await asyncio.to_thread(
            self._unsplash.search_photos,
            query,
            per_page=normalized_count,
        )
        return {"images": images, "count": len(images), "query": query, "success": True}
