import asyncio

from app.models.schemas import Attraction, DayPlan, Location, Meal, TripPlan
from app.services.baidu_map_service import BaiduMapService
from app.services.meal_enrichment import enrich_meals_with_baidu


def _attraction(name="布达拉宫") -> Attraction:
    return Attraction(
        name=name,
        address="测试地址",
        location=Location(longitude=91.13, latitude=29.65),
        visit_duration=120,
        description="测试景点",
    )


def _plan(*meals: Meal) -> TripPlan:
    return TripPlan(
        city="拉萨",
        start_date="2026-08-20",
        end_date="2026-08-20",
        days=[
            DayPlan(
                date="2026-08-20",
                day_index=0,
                description="测试",
                transportation="步行",
                accommodation="酒店",
                attractions=[_attraction()],
                meals=list(meals),
            )
        ],
        overall_suggestions="测试",
    )


class FakeBaidu:
    def __init__(self, results=None, details=None, *, enabled=True):
        self.enabled = enabled
        self.results = results or {}
        self.details = details or {}
        self.search_calls = []
        self.detail_calls = []
        self._converter = BaiduMapService("test-key")

    def search_pois(
        self,
        keywords,
        city,
        tag=None,
        sort_by=None,
        offset=10,
        *,
        location=None,
        radius=5000,
    ):
        self.search_calls.append(
            {
                "keywords": keywords,
                "city": city,
                "tag": tag,
                "offset": offset,
                "location": location,
                "radius": radius,
            }
        )
        return [dict(item) for item in self.results.get(keywords, [])]

    def get_poi_detail(self, uid):
        self.detail_calls.append(uid)
        detail = self.details.get(uid)
        return dict(detail) if detail else None

    def poi_to_restaurant(self, poi):
        return self._converter.poi_to_restaurant(poi)


def _baidu_poi(uid, name, *, rating=4.7, price=88, comments=321):
    return {
        "uid": uid,
        "name": name,
        "address": "北京东路8号",
        "location": {"lng": 91.13, "lat": 29.65},
        "detail_info": {
            "overall_rating": rating,
            "price": price,
            "shop_hours": "10:00-22:00",
            "comment_num": comments,
            "distance": 500,
        },
    }


def test_baidu_http_replaces_all_llm_meals_with_verified_pois():
    llm_meal = Meal(
        type="dinner",
        name="模型编造餐厅",
        estimated_cost=999,
        rating=5.0,
        price_per_person=999,
        data_source="llm",
    )
    baidu = FakeBaidu(
        {
            "早餐": [_baidu_poi("uid-breakfast", "藏式早餐店", price=25)],
            "中餐": [_baidu_poi("uid-lunch", "雪域餐厅", price=68)],
            "特色餐厅": [_baidu_poi("uid-dinner", "拉萨特色餐厅", price=98)],
        }
    )

    enriched = asyncio.run(enrich_meals_with_baidu(_plan(llm_meal), baidu))
    meals = enriched.days[0].meals

    assert [meal.name for meal in meals] == ["藏式早餐店", "雪域餐厅", "拉萨特色餐厅"]
    assert [meal.data_source for meal in meals] == ["baidu", "baidu", "baidu"]
    assert [meal.poi_id for meal in meals] == ["uid-breakfast", "uid-lunch", "uid-dinner"]
    assert all(meal.name != "模型编造餐厅" for meal in meals)
    assert enriched.budget.total_meals == 25 + 68 + 98
    assert len(baidu.search_calls) == 3
    assert set(baidu.detail_calls) == {"uid-breakfast", "uid-lunch", "uid-dinner"}


def test_searches_around_day_location_with_city_fallback_only_when_empty():
    baidu = FakeBaidu(
        {
            "早餐": [_baidu_poi("uid-breakfast", "早餐店")],
            "中餐": [_baidu_poi("uid-lunch", "中餐厅")],
            "特色餐厅": [_baidu_poi("uid-dinner", "晚餐厅")],
        }
    )

    asyncio.run(enrich_meals_with_baidu(_plan(), baidu))

    assert len(baidu.search_calls) == 3
    assert all(call["location"] == Location(longitude=91.13, latitude=29.65) for call in baidu.search_calls)
    assert all(call["radius"] == 8000 for call in baidu.search_calls)


def test_same_baidu_poi_is_not_reused_for_multiple_meals():
    repeated = _baidu_poi("same-uid", "同一家餐厅")
    baidu = FakeBaidu(
        {"早餐": [repeated], "中餐": [repeated], "特色餐厅": [repeated]}
    )

    enriched = asyncio.run(enrich_meals_with_baidu(_plan(), baidu))

    assert [meal.data_source for meal in enriched.days[0].meals] == [
        "baidu",
        "unavailable",
        "unavailable",
    ]


def test_no_baidu_result_removes_llm_values_and_marks_unavailable():
    llm_meal = Meal(
        type="lunch",
        name="模型餐厅",
        estimated_cost=500,
        rating=4.9,
        price_per_person=500,
    )
    baidu = FakeBaidu()

    enriched = asyncio.run(enrich_meals_with_baidu(_plan(llm_meal), baidu))

    assert all(meal.data_source == "unavailable" for meal in enriched.days[0].meals)
    assert all(meal.rating is None for meal in enriched.days[0].meals)
    assert all(meal.price_per_person is None for meal in enriched.days[0].meals)
    assert all("模型餐厅" not in meal.name for meal in enriched.days[0].meals)
    assert len(baidu.search_calls) == 6


def test_disabled_baidu_never_preserves_llm_restaurant():
    plan = _plan(Meal(type="lunch", name="模型餐厅", estimated_cost=200, rating=4.8))

    enriched = asyncio.run(
        enrich_meals_with_baidu(plan, FakeBaidu(enabled=False))
    )

    assert all(meal.data_source == "unavailable" for meal in enriched.days[0].meals)
    assert all(meal.name != "模型餐厅" for meal in enriched.days[0].meals)
