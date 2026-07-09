from __future__ import annotations

from typing import Any, Dict

from app.models.schemas import Budget
from app.services.budget import estimate_transportation_cost
from app.tools.base import BaseTool


class BudgetCalculatorTool(BaseTool):
    name = "budget_calculator"
    description = "根据每日行程、交通方式和住宿天数计算旅行预算。"
    parameters = {
        "type": "object",
        "properties": {
            "days": {"type": "integer", "description": "旅行天数", "minimum": 1},
            "transportation": {
                "type": "string",
                "description": "交通方式：步行|公共交通|地铁|打车|自驾",
                "enum": ["步行", "公共交通", "地铁", "打车", "自驾"],
            },
            "attraction_count": {"type": "integer", "description": "景点总数"},
            "avg_ticket_price": {"type": "integer", "description": "平均门票价格（元）"},
            "hotel_price_per_night": {"type": "integer", "description": "每晚酒店费用（元）"},
            "meal_level": {
                "type": "string",
                "description": "餐饮档次：经济|中等|舒适|高",
                "enum": ["经济", "中等", "舒适", "高"],
            },
        },
        "required": ["days", "transportation", "attraction_count", "hotel_price_per_night", "meal_level"],
    }

    async def execute(
        self,
        days: int,
        transportation: str,
        attraction_count: int,
        hotel_price_per_night: int,
        meal_level: str,
        avg_ticket_price: int = 0,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        meal_cost_per_day = {"经济": 118, "中等": 183, "舒适": 268, "高": 438}.get(meal_level, 183)
        total_attractions = max(attraction_count, 0) * max(avg_ticket_price, 0)
        total_hotels = max(days - 1, 0) * max(hotel_price_per_night, 0)
        total_meals = max(days, 1) * meal_cost_per_day
        total_transportation = estimate_transportation_cost(transportation) * max(days, 1)
        budget = Budget(
            total_attractions=total_attractions,
            total_hotels=total_hotels,
            total_meals=total_meals,
            total_transportation=total_transportation,
            total=total_attractions + total_hotels + total_meals + total_transportation,
        )
        return {"budget": budget.model_dump(), "success": True}
