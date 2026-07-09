from app.models.schemas import Budget, DayPlan


TRANSPORTATION_COST_PER_DAY = {
    "步行": 10,
    "公共交通": 35,
    "地铁": 35,
    "公交": 30,
    "打车": 120,
    "自驾": 180,
}


def estimate_transportation_cost(transportation: str) -> int:
    for keyword, value in TRANSPORTATION_COST_PER_DAY.items():
        if keyword in transportation:
            return value
    return 50


def calculate_budget(days: list[DayPlan], transportation: str) -> Budget:
    total_attractions = sum(attraction.ticket_price for day in days for attraction in day.attractions)
    total_meals = sum(meal.estimated_cost for day in days for meal in day.meals)
    hotel_nights = max(len(days) - 1, 0)
    first_hotel_cost = next((day.hotel.estimated_cost for day in days if day.hotel), 0)
    total_hotels = first_hotel_cost * hotel_nights
    total_transportation = estimate_transportation_cost(transportation) * len(days)
    total = total_attractions + total_hotels + total_meals + total_transportation
    return Budget(
        total_attractions=total_attractions,
        total_hotels=total_hotels,
        total_meals=total_meals,
        total_transportation=total_transportation,
        total=total,
    )
