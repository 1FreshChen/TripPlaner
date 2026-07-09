"""
Post-processing enrichment: populates Meal objects with real Baidu Maps restaurant data
after the LLM generates the trip plan. This ensures Baidu detail fields (rating,
price_per_person, shop_hours, comment_num) are always present when available,
regardless of whether the LLM chose to include them.
"""

from __future__ import annotations

import asyncio
import logging

from app.models.schemas import Meal, TripPlan
from app.services.baidu_map_service import BaiduMapService

logger = logging.getLogger(__name__)


async def enrich_meals_with_baidu(plan: TripPlan, baidu: BaiduMapService) -> TripPlan:
    """Enrich every Meal in the plan with Baidu restaurant detail data.

    For each meal whose name looks like a real restaurant (not a generic placeholder),
    we query Baidu POI search and backfill rating, price_per_person, shop_hours,
    and comment_num.
    """
    if not baidu.enabled:
        return plan

    generic_keywords = {"早餐", "午餐", "晚餐", "小吃", "简餐", "本地", "酒店", "附近", "特色"}
    meals_to_enrich: list[tuple[int, int, str]] = []

    for day_idx, day in enumerate(plan.days):
        for meal_idx, meal in enumerate(day.meals):
            name = (meal.name or "").strip()
            if not name:
                continue
            if _is_generic(name, generic_keywords):
                continue
            if meal.rating is not None and meal.price_per_person is not None:
                continue
            meals_to_enrich.append((day_idx, meal_idx, name))

    if not meals_to_enrich:
        return plan

    async def _enrich_one(day_idx: int, meal_idx: int, name: str) -> tuple[int, int, dict | None]:
        loop = asyncio.get_running_loop()
        pois = await loop.run_in_executor(
            None,
            lambda: baidu.search_pois(name, city=plan.city, offset=1),
        )
        if not pois:
            return (day_idx, meal_idx, None)
        restaurant = baidu.poi_to_restaurant(pois[0])
        if restaurant is None:
            return (day_idx, meal_idx, None)
        return (day_idx, meal_idx, restaurant)

    tasks = [
        _enrich_one(day_idx, meal_idx, name)
        for day_idx, meal_idx, name in meals_to_enrich
    ]
    results = await asyncio.gather(*tasks)

    enriched_count = 0
    for day_idx, meal_idx, restaurant in results:
        if restaurant is None:
            continue
        meal = plan.days[day_idx].meals[meal_idx]

        rating_raw = restaurant.get("overall_rating")
        if rating_raw is not None and meal.rating is None:
            try:
                meal.rating = float(rating_raw)
            except (TypeError, ValueError):
                pass

        price_raw = restaurant.get("price")
        if price_raw is not None and meal.price_per_person is None:
            try:
                meal.price_per_person = int(float(price_raw))
            except (TypeError, ValueError):
                pass

        shop_hours_raw = restaurant.get("shop_hours")
        if shop_hours_raw and meal.shop_hours is None:
            meal.shop_hours = str(shop_hours_raw)

        comment_raw = restaurant.get("comment_num")
        if comment_raw is not None and meal.comment_num is None:
            try:
                meal.comment_num = int(comment_raw)
            except (TypeError, ValueError):
                pass

        # Update estimated_cost based on meal type ratio if it's still at a generic value
        if meal.price_per_person and meal.price_per_person > 0:
            ratios = {"breakfast": 0.3, "lunch": 0.6, "dinner": 0.8, "snack": 0.2}
            ratio = ratios.get(meal.type, 0.5)
            meal.estimated_cost = max(meal.estimated_cost, int(meal.price_per_person * ratio))

        enriched_count += 1

    if enriched_count:
        logger.info(
            "Baido meal enrichment: %d/%d meals enriched for %s",
            enriched_count,
            len(meals_to_enrich),
            plan.city,
        )

    return plan


def _is_generic(name: str, generic_keywords: set[str]) -> bool:
    """Heuristic: a meal name is 'generic' if it contains 2+ placeholder keywords
    and doesn't look like a real restaurant name."""
    hits = sum(1 for kw in generic_keywords if kw in name)
    if hits >= 2:
        return True
    if hits >= 1 and len(name) <= 6:
        return True
    if any(name.startswith(f"{kw}") for kw in ["早餐", "午餐", "晚餐"]):
        return True
    return False
