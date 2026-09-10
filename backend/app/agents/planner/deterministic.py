from __future__ import annotations

import asyncio
from datetime import date, timedelta
from typing import Any, Dict, List

from app.models.schemas import Attraction, DayPlan, Hotel, TripPlan, TripPlanRequest, WeatherInfo
from app.orchestration.base import BaseAgent
from app.services.budget import calculate_budget
from app.services.mock_data import build_mock_meals
from app.agents.planner.validation import validate_planner_output

class PlannerAgent(BaseAgent):
    """Deterministic Planner backend used for offline operation and fallback."""

    name = "trip_planner"

    def run(
        self,
        request: TripPlanRequest,
        attractions: List[Attraction],
        weather_info: List[WeatherInfo],
        hotels: List[Hotel],
    ) -> TripPlan:
        plan = self._generate_deterministic_plan(request, attractions, weather_info, hotels)
        return validate_planner_output(plan, request)

    async def execute(self, context: Dict[str, Any]) -> TripPlan:
        request = context["request"]
        attractions = context["attraction_search"]
        weather_info = context["weather_query"]
        hotels = context["hotel_recommendation"]
        return await asyncio.to_thread(
            self.run,
            request,
            attractions,
            weather_info,
            hotels,
        )

    def _generate_deterministic_plan(
        self,
        request: TripPlanRequest,
        attractions: List[Attraction],
        weather_info: List[WeatherInfo],
        hotels: List[Hotel],
    ) -> TripPlan:
        start = date.fromisoformat(request.start_date)
        days: List[DayPlan] = []
        per_day = 3 if len(attractions) >= request.days * 3 else 2

        for day_index in range(request.days):
            day_date = (start + timedelta(days=day_index)).isoformat()
            start_index = day_index * per_day
            day_attractions = [
                attractions[(start_index + offset) % len(attractions)] for offset in range(per_day)
            ]
            hotel = hotels[day_index % len(hotels)] if hotels else None
            days.append(
                DayPlan(
                    date=day_date,
                    day_index=day_index,
                    description=f"第{day_index + 1}天围绕{request.city}的{request.preferences}主题安排，兼顾交通顺路和游览节奏。",
                    transportation=request.transportation,
                    accommodation=request.accommodation,
                    hotel=hotel,
                    attractions=day_attractions,
                    meals=build_mock_meals(request.city, day_index, request.budget),
                )
            )

        budget = calculate_budget(days, request.transportation)
        return TripPlan(
            city=request.city,
            start_date=request.start_date,
            end_date=request.end_date,
            days=days,
            weather_info=weather_info[: request.days],
            overall_suggestions=self._build_overall_suggestions(request, weather_info),
            budget=budget,
        )

    @staticmethod
    def _build_overall_suggestions(request: TripPlanRequest, weather_info: List[WeatherInfo]) -> str:
        weather = weather_info[0] if weather_info else None
        day_temp = weather.day_temp if weather else None
        weather_name = weather.day_weather if weather else "当地天气"
        if day_temp is None:
            clothing = f"出发前确认{request.city}实时天气，按昼夜温差准备外套或雨具。"
        elif day_temp >= 30:
            clothing = f"{request.city}白天约{day_temp}℃且{weather_name}，穿透气上衣，午后户外段准备帽子和防晒。"
        elif day_temp <= 8:
            clothing = f"{request.city}白天约{day_temp}℃，长时间户外会偏冷，建议羽绒服或厚外套配围巾。"
        else:
            clothing = f"{request.city}白天约{day_temp}℃且{weather_name}，轻便外套足够，早晚移动时注意加一层。"

        food_tips = {
            "北京": "北京烤鸭、炸酱面、铜锅涮肉",
            "上海": "生煎、小笼包、本帮红烧肉",
            "杭州": "西湖醋鱼、龙井虾仁、片儿川",
            "成都": "担担面、钟水饺、麻婆豆腐",
            "广州": "虾饺、肠粉、烧鹅",
            "深圳": "光明乳鸽、潮汕牛肉火锅、客家酿豆腐",
            "西安": "肉夹馍、羊肉泡馍、凉皮",
        }
        hidden_tips = {
            "北京": "傍晚从景山西门转到地安门，再沿什刹海走到银锭桥，胡同灯光比正午更有层次。",
            "上海": "傍晚从武康路慢走到安福路，避开南京路高峰，也更容易找到安静咖啡馆。",
            "杭州": "清晨先到北山街或杨公堤，西湖边人少，水面和山影更适合拍照。",
            "成都": "下午把人民公园茶馆留作休息点，盖碗茶比赶景点更能感受本地节奏。",
            "广州": "早上去老城区茶楼吃早茶，错开 10 点后的排队高峰。",
            "深圳": "日落前到深圳湾公园海边步道，天气通透时能看到湾区天际线。",
            "西安": "傍晚从书院门走到城墙南门，灯亮后比白天更适合拍古城轮廓。",
        }
        foods = food_tips.get(request.city, f"{request.city}的老字号招牌菜、当季小吃和酒店附近排队稳定的本地餐馆")
        hidden = hidden_tips.get(request.city, f"挑一个傍晚时段去{request.city}老街区或河岸步道慢走，比正午赶景点更轻松。")

        return "\n".join(
            [
                f"1. 穿衣：{clothing}",
                f"2. 必吃：优先安排{foods}，紧凑日选景点附近翻台快的店，轻松日晚餐再留给特色餐厅。",
                f"3. 交通：{request.transportation}优先串联同一区域景点，上午先去预约或排队压力大的点，下午再转向开放街区。",
                f"4. 避坑：热门场馆、观景台和收费展览以官方预约渠道为准，现场低价讲解或临时加价套餐不要急着买。",
                f"5. 隐藏玩法：{hidden}",
            ]
        )
