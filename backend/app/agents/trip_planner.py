from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from pydantic import ValidationError

from app.agents.prompts import PLANNER_AGENT_PROMPT, PLANNER_AGENT_PROMPT_LEGACY
from app.config import get_settings
from app.models.schemas import Attraction, DayPlan, Hotel, TripPlan, TripPlanRequest, WeatherInfo
from app.orchestration.base import AgentStatus, BaseAgent, ExecutionTrace
from app.orchestration.orchestrator import AgentOrchestrator
from app.orchestration.trace import ExecutionTracer
from app.services.amap_service import AmapService
from app.services.amap_mcp_service import get_amap_mcp_service
from app.services.budget import calculate_budget
from app.services.llm_service import LLMService
from app.services.mock_data import build_mock_attractions, build_mock_hotels, build_mock_meals, build_mock_weather
from app.services.plan_quality import PlanQualityError, validate_trip_plan_for_request
from app.services.unsplash_service import UnsplashService
from app.tools.bootstrap import bootstrap_tools
from app.tools.executor import ToolExecutor
from app.tools.registry import ToolRegistry


logger = logging.getLogger(__name__)


def build_planner_query(
    request: TripPlanRequest,
    attraction_response: str,
    weather_response: str,
    hotel_response: str,
    memory_context: Optional[Dict[str, Any]] = None,
    conversation_context: Optional[List[Dict[str, str]]] = None,
) -> str:
    memory_text = _format_memory_context(memory_context or {})
    conversation_text = _format_conversation_context(conversation_context or [])
    return f"""
请根据以下 baseline 数据生成{request.city}的{request.days}日旅行计划:

**用户需求:**
- 目的地: {request.city}
- 日期: {request.start_date} 至 {request.end_date}
- 天数: {request.days}天
- 偏好: {request.preferences}
- 预算: {request.budget}
- 交通方式: {request.transportation}
- 住宿类型: {request.accommodation}

**历史记忆:**
{memory_text}

**近期对话:**
{conversation_text}

**景点信息:**
{attraction_response}

**天气信息:**
{weather_response}

**酒店信息:**
{hotel_response}

**可用工具:**
- amap_poi_search: 用不同关键词补充或验证景点、餐厅、街区、商圈等 POI。
- baidu_poi_search: 搜索餐厅/美食，返回口味、服务、环境等多维度评分、人均消费和营业时间。适合需要精细筛选餐厅时使用。
- baidu_direction: 计算两个地点间的实际交通距离和耗时（公交/步行/驾车/骑行）。
- amap_weather: 验证目的地天气。
- hotel_search: 补充住宿候选。
- budget_calculator: 估算预算结构。
- unsplash_image: 为景点补充图片候选。

**去重与多样性要求:**
1. 上面的景点、天气和酒店是 baseline 数据，不是最终答案的全部来源。
2. 如果可以调用工具，请主动用 2-4 组不同关键词扩展候选，例如“博物馆”“艺术区”“胡同”“城市漫步”“亲子体验”等。
3. 为了增加多样性，建议替换 baseline 景点中的 40%-60%，但保留最符合用户偏好的高价值地点。
4. 对历史记忆、近期对话、同城收藏或用户明确说去过的地点做去重；确需保留时说明理由，并避免连续多天安排同质化景点。
5. 如果 baseline 与工具结果名称或地址重复，请合并为一个候选，不要在不同行程日重复安排。
6. 如果不确定餐厅质量，优先用 baidu_poi_search 查口味评分、人均消费和营业时间，再与 amap_poi_search 结果互补。

请生成详细的旅行计划,包括每天的景点安排、餐饮推荐、住宿信息和预算明细。

**餐饮 (Meal) 数据填充要求:**
- 使用 baidu_poi_search 获取的餐厅数据，请把 rating（综合评分，0-5）、price_per_person（人均消费，整数元）、shop_hours（营业时间字符串）、comment_num（评论数量）一并填入对应的 Meal 对象。
- estimated_cost 按 meal.type 估算：breakfast 约为人均的30%、lunch 约为人均的60%、dinner 约为人均的80%。
"""


def _format_memory_context(memory_context: Dict[str, Any]) -> str:
    if not memory_context:
        return "(无)"

    lines: list[str] = []
    categories = memory_context.get("preferred_categories") or []
    if categories:
        lines.append(f"- 偏好类别: {', '.join(str(item) for item in categories)}")

    budget_profile = memory_context.get("budget_profile") or {}
    if budget_profile:
        profile_text = ", ".join(f"{key}: {value}" for key, value in budget_profile.items())
        lines.append(f"- 预算画像: {profile_text}")

    travel_style = memory_context.get("travel_style")
    if travel_style:
        lines.append(f"- 旅行风格: {travel_style}")

    favorite_cities = memory_context.get("favorite_cities") or []
    if favorite_cities:
        lines.append(f"- 常去城市: {', '.join(str(item) for item in favorite_cities)}")

    avg_trip_days = memory_context.get("avg_trip_days")
    if avg_trip_days is not None:
        lines.append(f"- 历史平均天数: {avg_trip_days}")

    saved_attractions = memory_context.get("saved_attractions_in_city") or []
    if saved_attractions:
        names = [
            str(item.get("name") or item.get("title") or item)
            for item in saved_attractions
        ]
        lines.append(f"- 同城收藏景点: {', '.join(names)}")

    return "\n".join(lines) if lines else "(无)"


def _format_conversation_context(conversation_context: List[Dict[str, str]]) -> str:
    if not conversation_context:
        return "(无)"
    return "\n".join(
        f"- {message.get('role', 'unknown')}: {message.get('content', '')}"
        for message in conversation_context[-10:]
        if message.get("content")
    ) or "(无)"


def summarize_attractions(attractions: List[Attraction]) -> str:
    return "\n".join(
        f"- {item.name} | {item.address} | 门票{item.ticket_price}元 | 建议{item.visit_duration}分钟"
        for item in attractions
    )


def summarize_weather(weather_info: List[WeatherInfo]) -> str:
    return "\n".join(
        f"- {item.date}: 白天{item.day_weather}{item.day_temp}℃, 夜间{item.night_weather}{item.night_temp}℃"
        for item in weather_info
    )


def summarize_hotels(hotels: List[Hotel]) -> str:
    return "\n".join(
        f"- {item.name} | {item.address} | {item.price_range} | 预估{item.estimated_cost}元/晚"
        for item in hotels
    )


class AttractionSearchAgent(BaseAgent):
    """景点搜索专家。"""

    name = "attraction_search"

    def __init__(self, amap_service: Optional[AmapService] = None, enable_external_services: Optional[bool] = None):
        settings = get_settings()
        self.amap_service = amap_service or get_amap_mcp_service()
        self.mcp_tool = getattr(self.amap_service, "mcp_tool", None)
        self.enable_external_services = (
            settings.enable_external_services if enable_external_services is None else enable_external_services
        )

    def run(self, request: TripPlanRequest) -> List[Attraction]:
        keyword = self._keyword_from_preferences(request.preferences)
        if self.enable_external_services:
            pois = self.amap_service.search_pois(keyword, request.city, offset=max(request.days * 3, 10))
            attractions = [
                attraction
                for attraction in (self.amap_service.poi_to_attraction(poi, request.preferences) for poi in pois)
                if attraction is not None
            ]
            if attractions:
                return attractions[: max(request.days * 3, 6)]
        return build_mock_attractions(request.city, request.preferences, request.days)

    async def execute(self, context: Dict[str, Any]) -> List[Attraction]:
        return await asyncio.to_thread(self.run, context["request"])

    @staticmethod
    def _keyword_from_preferences(preferences: str) -> str:
        if "历史" in preferences or "文化" in preferences:
            return "博物馆 历史 景点"
        if "自然" in preferences or "风光" in preferences:
            return "公园 风景区"
        if "美食" in preferences:
            return "美食街 景点"
        return "景点"


class WeatherQueryAgent(BaseAgent):
    """天气查询专家。"""

    name = "weather_query"

    def __init__(self, amap_service: Optional[AmapService] = None, enable_external_services: Optional[bool] = None):
        settings = get_settings()
        self.amap_service = amap_service or get_amap_mcp_service()
        self.mcp_tool = getattr(self.amap_service, "mcp_tool", None)
        self.enable_external_services = (
            settings.enable_external_services if enable_external_services is None else enable_external_services
        )

    def run(self, request: TripPlanRequest) -> List[WeatherInfo]:
        if self.enable_external_services:
            weather = self.amap_service.get_weather(request.city)
            if weather:
                matching_weather = self._select_weather_for_request(weather, request)
                if matching_weather:
                    return matching_weather
        return build_mock_weather(request.start_date, request.days)

    async def execute(self, context: Dict[str, Any]) -> List[WeatherInfo]:
        return await asyncio.to_thread(self.run, context["request"])

    @staticmethod
    def _select_weather_for_request(
        weather: List[WeatherInfo],
        request: TripPlanRequest,
    ) -> List[WeatherInfo]:
        start = date.fromisoformat(request.start_date)
        expected_dates = [
            (start + timedelta(days=offset)).isoformat()
            for offset in range(request.days)
        ]
        weather_by_date = {item.date: item for item in weather}
        if all(day in weather_by_date for day in expected_dates):
            return [weather_by_date[day] for day in expected_dates]
        return []


class HotelAgent(BaseAgent):
    """酒店推荐专家。"""

    name = "hotel_recommendation"

    def __init__(self, amap_service: Optional[AmapService] = None, enable_external_services: Optional[bool] = None):
        settings = get_settings()
        self.amap_service = amap_service or get_amap_mcp_service()
        self.mcp_tool = getattr(self.amap_service, "mcp_tool", None)
        self.enable_external_services = (
            settings.enable_external_services if enable_external_services is None else enable_external_services
        )

    def run(self, request: TripPlanRequest) -> List[Hotel]:
        if self.enable_external_services:
            pois = self.amap_service.search_pois(f"{request.accommodation} 酒店", request.city, offset=5)
            hotels = [
                hotel
                for hotel in (self.amap_service.poi_to_hotel(poi, request.accommodation) for poi in pois)
                if hotel is not None
            ]
            if hotels:
                return hotels
        return build_mock_hotels(request.city, request.accommodation, request.budget)

    async def execute(self, context: Dict[str, Any]) -> List[Hotel]:
        return await asyncio.to_thread(self.run, context["request"])


class PlannerAgent(BaseAgent):
    """行程规划专家。"""

    name = "trip_planner"

    def __init__(
        self,
        llm_service: Optional[LLMService] = None,
        use_llm: Optional[bool] = None,
        use_enhanced_prompt: Optional[bool] = None,
    ):
        settings = get_settings()
        self.llm_service = llm_service
        if self.llm_service is None:
            self.llm_service = LLMService(settings.llm_api_key, settings.llm_base_url, settings.llm_model)
        self.use_llm = (
            settings.enable_external_services and self.llm_service.enabled if use_llm is None else use_llm
        )
        prompt_flag = settings.use_enhanced_prompt if use_enhanced_prompt is None else use_enhanced_prompt
        self.planner_prompt = PLANNER_AGENT_PROMPT if prompt_flag else PLANNER_AGENT_PROMPT_LEGACY

    def run(
        self,
        request: TripPlanRequest,
        attractions: List[Attraction],
        weather_info: List[WeatherInfo],
        hotels: List[Hotel],
        planner_query: str,
    ) -> TripPlan:
        if self.use_llm:
            payload = self.llm_service.generate_json(self.planner_prompt, planner_query)
            if payload:
                try:
                    return TripPlan.model_validate(payload)
                except ValidationError as exc:
                    logger.warning(
                        "Planner LLM response failed schema validation; using deterministic fallback (%d errors)",
                        exc.error_count(),
                    )
        return self._generate_deterministic_plan(request, attractions, weather_info, hotels)

    async def execute(self, context: Dict[str, Any]) -> TripPlan:
        request = context["request"]
        attractions = context["attraction_search"]
        weather_info = context["weather_query"]
        hotels = context["hotel_recommendation"]
        planner_query = context.get(
            "planner_query",
            build_planner_query(
                request=request,
                attraction_response=summarize_attractions(attractions),
                weather_response=summarize_weather(weather_info),
                hotel_response=summarize_hotels(hotels),
                memory_context=context.get("memory_context"),
                conversation_context=context.get("conversation_context"),
            ),
        )
        return await asyncio.to_thread(self.run, request, attractions, weather_info, hotels, planner_query)

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

class TripPlannerAgent:
    """多 Agent 协作入口。"""

    def __init__(
        self,
        enable_external_services: Optional[bool] = None,
        amap_service: Optional[AmapService] = None,
        llm_service: Optional[LLMService] = None,
        unsplash_service: Optional[UnsplashService] = None,
        tool_registry: Optional[ToolRegistry] = None,
        tool_executor: Optional[ToolExecutor] = None,
        enable_llm_tool_planning: Optional[bool] = None,
    ):
        settings = get_settings()
        use_external = settings.enable_external_services if enable_external_services is None else enable_external_services
        self.amap_service = amap_service or get_amap_mcp_service()
        self.mcp_tool = getattr(self.amap_service, "mcp_tool", None)
        self.llm_service = llm_service or LLMService(settings.llm_api_key, settings.llm_base_url, settings.llm_model)
        self.unsplash_service = unsplash_service or UnsplashService(settings.unsplash_access_key)
        self.tool_registry = tool_registry or bootstrap_tools(self.amap_service, self.unsplash_service)
        self.tool_executor = tool_executor or ToolExecutor(registry=self.tool_registry)
        self.attraction_agent = AttractionSearchAgent(self.amap_service, use_external)
        self.weather_agent = WeatherQueryAgent(self.amap_service, use_external)
        self.hotel_agent = HotelAgent(self.amap_service, use_external)
        self.planner_agent = PlannerAgent(
            self.llm_service,
            use_llm=use_external and self.llm_service.enabled,
            use_enhanced_prompt=settings.use_enhanced_prompt,
        )
        self.tracer = ExecutionTracer()
        self.last_trace: Optional[ExecutionTrace] = None
        self.last_tool_calls: List[Dict[str, Any]] = []
        self.last_token_usage: Any = None
        self.last_critique_events: List[Dict[str, Any]] = []

        from app.orchestration.bootstrap import bootstrap_orchestration, build_default_fallback_chain

        self.registry = bootstrap_orchestration(
            amap_service=self.amap_service,
            llm_service=self.llm_service,
            enable_external_services=use_external,
            tool_registry=self.tool_registry,
            tool_executor=self.tool_executor,
            enable_llm_tool_planning=enable_llm_tool_planning,
        )
        self.fallback_chain = build_default_fallback_chain()

    async def aplan_trip(
        self,
        request: TripPlanRequest,
        memory_context: Optional[Dict[str, Any]] = None,
        conversation_context: Optional[List[Dict[str, str]]] = None,
    ) -> TripPlan:
        context: Dict[str, Any] = {
            "request": request,
            "user_preferences": memory_context or request.preferences,
            "memory_context": memory_context or {},
            "conversation_context": conversation_context or [],
            "tool_registry": self.tool_registry,
            "tool_executor": self.tool_executor,
        }
        orchestrator = AgentOrchestrator(self.registry, self.fallback_chain, self.tracer)
        trace = await orchestrator.run("trip-planner", context)
        self.last_trace = trace
        self.last_tool_calls = context.get("trip_planner_tool_calls", [])
        self.last_token_usage = context.get("trip_planner_token_usage")
        self.last_critique_events = context.get("plan_critique_events", [])

        if context.get("trip_planner_quality_failed"):
            raise PlanQualityError("generated plan did not pass quality review")
        if trace.overall_status != AgentStatus.COMPLETED or "trip_planner" not in context:
            failure = next(
                (result for result in trace.agent_results if result.status == AgentStatus.FAILED),
                None,
            )
            detail = failure.error_message if failure else "trip_planner did not produce a result"
            raise RuntimeError(f"Trip planning orchestration failed: {detail}")

        trip_plan = context["trip_planner"]
        try:
            validate_trip_plan_for_request(
                trip_plan,
                request,
                critique_events=self.last_critique_events,
            )
        except PlanQualityError as exc:
            raise PlanQualityError(f"Trip planning orchestration failed quality validation: {exc}") from exc
        return trip_plan

    def plan_trip(self, request: TripPlanRequest) -> TripPlan:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.aplan_trip(request))
        raise RuntimeError("Use aplan_trip() when calling TripPlannerAgent from async code")

    def _build_planner_query(
        self,
        request: TripPlanRequest,
        attraction_response: str,
        weather_response: str,
        hotel_response: str,
    ) -> str:
        """构建规划Agent的查询"""
        return build_planner_query(request, attraction_response, weather_response, hotel_response)

    @staticmethod
    def _summarize_attractions(attractions: List[Attraction]) -> str:
        return summarize_attractions(attractions)

    @staticmethod
    def _summarize_weather(weather_info: List[WeatherInfo]) -> str:
        return summarize_weather(weather_info)

    @staticmethod
    def _summarize_hotels(hotels: List[Hotel]) -> str:
        return summarize_hotels(hotels)
