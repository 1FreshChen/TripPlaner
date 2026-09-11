import asyncio

import pytest

from app.models.schemas import Attraction, DayPlan, Location, TripPlan
from app.services.amap_service import AmapService, ServiceResult
from app.services.attraction_image_service import (
    AttractionVerificationError,
    attraction_name_similarity,
    enrich_attraction_images,
)


def _attraction(name: str, image_url: str | None = None, **kwargs) -> Attraction:
    return Attraction(
        name=name,
        address="Test Road",
        location=Location(longitude=116.397128, latitude=39.916527),
        visit_duration=120,
        description="Test attraction",
        image_url=image_url,
        **kwargs,
    )


def _plan(*attractions: Attraction) -> TripPlan:
    return TripPlan(
        city="北京",
        start_date="2026-08-20",
        end_date="2026-08-20",
        days=[
            DayPlan(
                date="2026-08-20",
                day_index=0,
                description="Test day",
                transportation="公共交通",
                accommodation="经济型酒店",
                attractions=list(attractions),
            )
        ],
        overall_suggestions="Test suggestions",
    )


class FakeUnsplashService:
    def __init__(self, responses=None, enabled=True):
        self.responses = responses or {}
        self.enabled = enabled
        self.queries = []

    def get_photo_url(self, query):
        self.queries.append(query)
        value = self.responses.get(query)
        if isinstance(value, Exception):
            raise value
        return value


def test_known_poi_image_is_copied_without_unsplash_call():
    service = FakeUnsplashService()
    plan = _plan(_attraction("故宫"))
    source = [
        _attraction(
            "故宫",
            "https://example.test/amap-palace.jpg",
            poi_id="poi-palace",
            data_source="amap_mcp",
            image_source="amap",
            coordinate_verified=True,
        )
    ]

    enriched = asyncio.run(enrich_attraction_images(plan, source, service))

    assert enriched.days[0].attractions[0].image_url == "https://example.test/amap-palace.jpg"
    assert enriched.days[0].attractions[0].poi_id == "poi-palace"
    assert enriched.days[0].attractions[0].data_source == "amap_mcp"
    assert enriched.days[0].attractions[0].coordinate_verified is True
    assert service.queries == []
    assert plan.days[0].attractions[0].image_url is None


def test_unsplash_fills_missing_image_and_reuses_it_for_duplicate_names():
    service = FakeUnsplashService(
        {"北京 故宫": "https://example.test/unsplash-palace.jpg"}
    )
    plan = _plan(_attraction("故宫"), _attraction("故宫"))

    enriched = asyncio.run(enrich_attraction_images(plan, [], service))

    assert [item.image_url for item in enriched.days[0].attractions] == [
        "https://example.test/unsplash-palace.jpg",
        "https://example.test/unsplash-palace.jpg",
    ]
    assert service.queries == ["北京 故宫"]
    assert all(item.image_source == "unsplash" for item in enriched.days[0].attractions)
    assert all(item.data_source == "llm" for item in enriched.days[0].attractions)


def test_image_lookup_failure_keeps_plan_valid_without_image():
    service = FakeUnsplashService({"北京 故宫": RuntimeError("image API unavailable")})
    plan = _plan(_attraction("故宫"))

    enriched = asyncio.run(enrich_attraction_images(plan, [], service))

    assert enriched.days[0].attractions[0].image_url is None
    assert service.queries == ["北京 故宫"]


def test_fuzzy_name_matching_binds_common_scenic_suffixes_to_real_amap_poi():
    service = FakeUnsplashService(enabled=False)
    plan = _plan(_attraction("玄武湖景区", data_source="llm"))
    source = [
        _attraction(
            "玄武湖风景区",
            poi_id="amap-xuanwu",
            data_source="amap_mcp",
            coordinate_verified=True,
        )
    ]

    enriched = asyncio.run(
        enrich_attraction_images(plan, source, service, require_verified=True)
    )

    attraction = enriched.days[0].attractions[0]
    assert attraction.name == "玄武湖风景区"
    assert attraction.poi_id == "amap-xuanwu"
    assert attraction.data_source == "amap_mcp"
    assert attraction.coordinate_verified is True
    assert attraction_name_similarity("明孝陵", "明孝陵景区") >= 0.9
    assert attraction_name_similarity("明孝陵博物馆", "明孝陵景区") < 0.68


def test_strict_verification_searches_amap_for_unmatched_planner_attraction():
    amap = AmapService("test-key")
    amap.search_pois = lambda keywords, city, offset=10: ServiceResult(
        data=[
            {
                "id": "amap-mingxiaoling",
                "name": "明孝陵景区",
                "address": "南京市玄武区石象路7号",
                "location": "118.835,32.059",
                "type": "风景名胜",
                "_source": "amap_http_fallback",
            }
        ],
        source="amap_http_fallback",
    )

    enriched = asyncio.run(
        enrich_attraction_images(
            _plan(_attraction("明孝陵")),
            [],
            FakeUnsplashService(enabled=False),
            amap_service=amap,
            preferences="历史文化",
            require_verified=True,
        )
    )

    attraction = enriched.days[0].attractions[0]
    assert attraction.name == "明孝陵景区"
    assert attraction.poi_id == "amap-mingxiaoling"
    assert attraction.data_source == "amap_http_fallback"
    assert attraction.coordinate_verified is True


def test_strict_verification_rejects_unresolved_or_failed_amap_search():
    amap = AmapService("test-key")
    amap.search_pois = lambda keywords, city, offset=10: ServiceResult(
        error="INVALID_USER_KEY",
        error_kind="provider",
        source="amap_http",
    )

    with pytest.raises(AttractionVerificationError, match="INVALID_USER_KEY"):
        asyncio.run(
            enrich_attraction_images(
                _plan(_attraction("模型虚构景点")),
                [],
                FakeUnsplashService(enabled=False),
                amap_service=amap,
                require_verified=True,
            )
        )
