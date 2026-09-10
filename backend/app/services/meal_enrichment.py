"""Replace planner-generated meals with verified Baidu HTTP POI recommendations."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from app.models.schemas import DayPlan, Location, Meal, TripPlan
from app.config import get_settings
from app.services.baidu_map_service import BaiduMapService
from app.services.budget import calculate_budget

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _MealSlot:
    meal_type: str
    query: str
    label: str
    fallback_budget: int


_MEAL_SLOTS = (
    _MealSlot("breakfast", "早餐", "早餐", 30),
    _MealSlot("lunch", "中餐", "午餐", 60),
    _MealSlot("dinner", "特色餐厅", "晚餐", 90),
)


async def enrich_meals_with_baidu(plan: TripPlan, baidu: BaiduMapService) -> TripPlan:
    """Build every final meal from Baidu POIs instead of LLM meal content.

    The planner's ``days[].meals`` are deliberately ignored. Each day receives
    deterministic breakfast/lunch/dinner slots. Searches prefer an 8 km area
    around the day's hotel or attractions, then fall back to a city search.
    """
    search_requests: list[tuple[int, _MealSlot, Location | None]] = []
    for day_index, day in enumerate(plan.days):
        for slot in _MEAL_SLOTS:
            search_requests.append((day_index, slot, _meal_center(day, slot.meal_type)))

    if not baidu.enabled:
        _replace_with_unavailable_meals(plan, reason="百度地图服务未配置")
        _recalculate_budget(plan)
        logger.warning(
            "Baidu meal recommendation unavailable: service_disabled days=%d",
            len(plan.days),
        )
        return plan

    semaphore = asyncio.Semaphore(get_settings().baidu_meal_concurrency)

    async def search(
        day_index: int,
        slot: _MealSlot,
        center: Location | None,
    ) -> tuple[int, _MealSlot, Location | None, list[dict[str, Any]]]:
        async with semaphore:
            pois = await asyncio.to_thread(
                baidu.search_pois,
                slot.query,
                plan.city,
                tag="美食",
                offset=10,
                location=center,
                radius=8000,
            )
            if not pois and center is not None:
                pois = await asyncio.to_thread(
                    baidu.search_pois,
                    slot.query,
                    plan.city,
                    tag="美食",
                    offset=10,
                )
        return day_index, slot, center, pois

    search_results = await asyncio.gather(
        *(search(day_index, slot, center) for day_index, slot, center in search_requests)
    )

    used_pois: set[str] = set()
    selected: dict[tuple[int, str], tuple[_MealSlot, Location | None, dict[str, Any]]] = {}
    raw_candidate_count = 0
    for day_index, slot, center, pois in search_results:
        raw_candidate_count += len(pois)
        candidate = _select_restaurant_candidate(pois, baidu, used_pois)
        if candidate is None:
            continue
        used_pois.add(_poi_identity(candidate))
        selected[(day_index, slot.meal_type)] = (slot, center, candidate)

    async def fetch_detail(
        key: tuple[int, str],
        slot: _MealSlot,
        center: Location | None,
        poi: dict[str, Any],
    ) -> tuple[tuple[int, str], _MealSlot, Location | None, dict[str, Any]]:
        uid = str(poi.get("uid") or "").strip()
        async with semaphore:
            detail = await asyncio.to_thread(baidu.get_poi_detail, uid) if uid else None
        return key, slot, center, _merge_poi_detail(poi, detail)

    detailed_results = await asyncio.gather(
        *(
            fetch_detail(key, slot, center, poi)
            for key, (slot, center, poi) in selected.items()
        )
    )
    detailed_by_slot = {
        key: (slot, center, poi) for key, slot, center, poi in detailed_results
    }

    selected_count = 0
    for day_index, day in enumerate(plan.days):
        verified_meals: list[Meal] = []
        for slot in _MEAL_SLOTS:
            selected_item = detailed_by_slot.get((day_index, slot.meal_type))
            if selected_item is None:
                verified_meals.append(_unavailable_meal(slot, "百度地图未返回可用餐厅"))
                continue
            selected_slot, center, poi = selected_item
            restaurant = baidu.poi_to_restaurant(poi)
            if restaurant is None:
                verified_meals.append(_unavailable_meal(slot, "百度地图餐厅缺少有效坐标"))
                continue
            verified_meals.append(
                _verified_meal(
                    selected_slot,
                    restaurant,
                    reference=_reference_name(day, center),
                )
            )
            selected_count += 1
        day.meals = verified_meals

    _recalculate_budget(plan)
    logger.info(
        "Baidu HTTP meal recommendation: searches=%d raw_candidates=%d selected=%d missing=%d city=%s",
        len(search_requests),
        raw_candidate_count,
        selected_count,
        len(search_requests) - selected_count,
        plan.city,
    )
    return plan


def _meal_center(day: DayPlan, meal_type: str) -> Location | None:
    if meal_type == "breakfast" and day.hotel and day.hotel.location:
        return day.hotel.location
    attractions = day.attractions
    if not attractions:
        return day.hotel.location if day.hotel else None
    if meal_type == "breakfast":
        return attractions[0].location
    if meal_type == "lunch":
        return attractions[len(attractions) // 2].location
    return attractions[-1].location


def _select_restaurant_candidate(
    pois: list[dict[str, Any]],
    baidu: BaiduMapService,
    used_pois: set[str],
) -> dict[str, Any] | None:
    usable = [
        poi
        for poi in pois
        if _poi_identity(poi) not in used_pois and baidu.poi_to_restaurant(poi) is not None
    ]
    if not usable:
        return None
    return max(usable, key=_restaurant_score)


def _restaurant_score(poi: dict[str, Any]) -> tuple[int, float, int, int]:
    detail = poi.get("detail_info") or {}
    rating = _to_float(detail.get("overall_rating")) or 0.0
    comments = _to_int(detail.get("comment_num")) or 0
    distance = _to_int(detail.get("distance"))
    return (
        1 if rating > 0 else 0,
        rating,
        min(comments, 100000),
        -(distance if distance is not None else 50000),
    )


def _poi_identity(poi: dict[str, Any]) -> str:
    uid = str(poi.get("uid") or "").strip()
    if uid:
        return f"uid:{uid}"
    return "name:" + _normalized_name(
        f"{poi.get('name', '')}|{poi.get('address', '')}"
    )


def _merge_poi_detail(
    search_poi: dict[str, Any],
    detail_poi: dict[str, Any] | None,
) -> dict[str, Any]:
    if not detail_poi:
        return dict(search_poi)
    merged = {**search_poi, **detail_poi}
    merged["uid"] = detail_poi.get("uid") or search_poi.get("uid")
    merged["detail_info"] = {
        **(search_poi.get("detail_info") or {}),
        **(detail_poi.get("detail_info") or {}),
    }
    return merged


def _verified_meal(
    slot: _MealSlot,
    restaurant: dict[str, Any],
    *,
    reference: str,
) -> Meal:
    price = _to_int(restaurant.get("price"))
    return Meal(
        type=slot.meal_type,
        name=str(restaurant.get("name") or "百度地图餐厅"),
        address=str(restaurant.get("address") or "") or None,
        location=restaurant.get("location"),
        description=(
            f"百度地图在{reference}周边检索到的餐厅。"
            "评分、人均和营业时间仅展示百度本次返回的数据，出发前建议再次确认。"
        ),
        estimated_cost=price if price is not None else slot.fallback_budget,
        rating=_to_float(restaurant.get("overall_rating")),
        price_per_person=price,
        shop_hours=str(restaurant.get("shop_hours") or "") or None,
        comment_num=_to_int(restaurant.get("comment_num")),
        data_source="baidu",
        poi_id=str(restaurant.get("uid") or "") or None,
    )


def _unavailable_meal(slot: _MealSlot, reason: str) -> Meal:
    return Meal(
        type=slot.meal_type,
        name=f"{slot.label}：未找到可核验餐厅",
        description=f"{reason}；未采用大模型生成的餐厅名称、评分、地址或价格。",
        estimated_cost=slot.fallback_budget,
        data_source="unavailable",
    )


def _replace_with_unavailable_meals(plan: TripPlan, *, reason: str) -> None:
    for day in plan.days:
        day.meals = [_unavailable_meal(slot, reason) for slot in _MEAL_SLOTS]


def _reference_name(day: DayPlan, center: Location | None) -> str:
    if center is None:
        return "当日行程区域"
    if day.hotel and day.hotel.location == center:
        return day.hotel.name
    for attraction in day.attractions:
        if attraction.location == center:
            return attraction.name
    return "当日行程区域"


def _recalculate_budget(plan: TripPlan) -> None:
    transportation = plan.days[0].transportation if plan.days else ""
    plan.budget = calculate_budget(plan.days, transportation)


def _normalized_name(value: str) -> str:
    return "".join(character.lower() for character in str(value or "") if character.isalnum())


def _to_float(value: Any) -> float | None:
    if value in (None, "", []):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    if value in (None, "", []):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None
