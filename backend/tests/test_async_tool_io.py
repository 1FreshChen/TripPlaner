import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.tools.implementations.amap_search import AmapPOISearchTool
from app.tools.implementations.amap_weather import AmapWeatherTool
from app.tools.implementations.baidu_direction import BaiduDirectionTool
from app.tools.implementations.baidu_poi_search import BaiduPOISearchTool
from app.tools.implementations.hotel_search import HotelSearchTool
from app.tools.implementations.image_search import UnsplashImageTool


def test_amap_search_offloads_sync_client_call():
    service = SimpleNamespace(search_pois=lambda *args: [])
    with patch("app.tools.implementations.amap_search.asyncio.to_thread", new=AsyncMock(return_value=[])) as offload:
        result = asyncio.run(AmapPOISearchTool(service).execute("博物馆", "北京", 10))

    offload.assert_awaited_once_with(service.search_pois, "博物馆", "北京", 10)
    assert result["pois"] == []


def test_amap_weather_offloads_sync_client_call():
    service = SimpleNamespace(get_weather=lambda *args: [])
    with patch("app.tools.implementations.amap_weather.asyncio.to_thread", new=AsyncMock(return_value=[])) as offload:
        result = asyncio.run(AmapWeatherTool(service).execute("北京"))

    offload.assert_awaited_once_with(service.get_weather, "北京")
    assert result["weather"] == []


def test_hotel_search_offloads_sync_client_call():
    service = SimpleNamespace(search_pois=lambda *args: [], poi_to_hotel=lambda *args: None)
    with patch("app.tools.implementations.hotel_search.asyncio.to_thread", new=AsyncMock(return_value=[])) as offload:
        result = asyncio.run(HotelSearchTool(service).execute("北京", "经济型酒店", 5))

    offload.assert_awaited_once_with(service.search_pois, "经济型酒店 酒店", "北京", 5)
    assert result["count"] > 0


def test_baidu_search_offloads_sync_client_call():
    service = SimpleNamespace(search_pois=lambda *args, **kwargs: [], poi_to_restaurant=lambda *args: None)
    with patch("app.tools.implementations.baidu_poi_search.asyncio.to_thread", new=AsyncMock(return_value=[])) as offload:
        result = asyncio.run(BaiduPOISearchTool(service).execute("火锅", "成都", limit=8))

    offload.assert_awaited_once_with(
        service.search_pois,
        "火锅",
        "成都",
        tag=None,
        sort_by="overall_rating",
        offset=8,
    )
    assert result["restaurants"] == []


def test_baidu_direction_offloads_sync_client_call():
    service = SimpleNamespace(get_direction=lambda *args, **kwargs: [])
    with patch(
        "app.tools.implementations.baidu_direction.asyncio.to_thread",
        new=AsyncMock(return_value=[]),
    ) as offload:
        result = asyncio.run(
            BaiduDirectionTool(service).execute("天安门", "故宫", "北京", mode="walking")
        )

    offload.assert_awaited_once_with(
        service.get_direction,
        "天安门",
        "故宫",
        "北京",
        mode="walking",
    )
    assert result["routes"] == []


def test_image_search_offloads_sync_client_call():
    service = SimpleNamespace(search_photos=lambda *args, **kwargs: [])
    with patch("app.tools.implementations.image_search.asyncio.to_thread", new=AsyncMock(return_value=[])) as offload:
        result = asyncio.run(UnsplashImageTool(service).execute("故宫", 2))

    offload.assert_awaited_once_with(service.search_photos, "故宫", per_page=2)
    assert result["images"] == []
