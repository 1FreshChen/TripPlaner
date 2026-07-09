from __future__ import annotations

from app.config import get_settings
from app.services.amap_service import AmapService
from app.services.baidu_map_service import BaiduMapService
from app.services.unsplash_service import UnsplashService
from app.tools.implementations import (
    AmapPOISearchTool,
    AmapWeatherTool,
    BaiduDirectionTool,
    BaiduPOISearchTool,
    BudgetCalculatorTool,
    HotelSearchTool,
    UnsplashImageTool,
)
from app.tools.registry import ToolRegistry


def bootstrap_tools(
    amap_service: AmapService | None = None,
    unsplash_service: UnsplashService | None = None,
    baidu_service: BaiduMapService | None = None,
    reset: bool = False,
) -> ToolRegistry:
    settings = get_settings()
    amap = amap_service or AmapService(settings.amap_api_key)
    unsplash = unsplash_service or UnsplashService(settings.unsplash_access_key)
    baidu = baidu_service or BaiduMapService(settings.baidu_map_api_key)

    registry = ToolRegistry()
    if reset:
        registry.clear()

    for tool in [
        AmapPOISearchTool(amap),
        AmapWeatherTool(amap),
        HotelSearchTool(amap),
        BaiduPOISearchTool(baidu),
        BaiduDirectionTool(baidu),
        BudgetCalculatorTool(),
        UnsplashImageTool(unsplash),
    ]:
        if registry.get(tool.name) is None:
            registry.register(tool)

    return registry
