import asyncio
from datetime import date, timedelta

from app.agents.prompts import PLANNER_AGENT_PROMPT_LEGACY
from app.agents.trip_planner import PlannerAgent, TripPlannerAgent, WeatherQueryAgent
from app.models.schemas import TripPlanRequest, WeatherInfo
from app.services.mock_data import build_mock_attractions, build_mock_hotels, build_mock_weather


class RecordingLLMService:
    def __init__(self):
        self.system_prompt = None
        self.user_prompt = None

    def generate_json(self, system_prompt, user_prompt):
        self.system_prompt = system_prompt
        self.user_prompt = user_prompt
        return None


class FakeWeatherService:
    def __init__(self, weather):
        self.weather = weather

    def get_weather(self, city):
        return self.weather


def _ascii_request(start_date="2026-07-23", days=5):
    start = date.fromisoformat(start_date)
    return TripPlanRequest(
        city="TestCity",
        start_date=start_date,
        end_date=(start + timedelta(days=days - 1)).isoformat(),
        days=days,
        preferences="museums",
        budget="medium",
        transportation="public transit",
        accommodation="economy hotel",
    )


def _weather_from(start_date, days):
    start = date.fromisoformat(start_date)
    return [
        WeatherInfo(
            date=(start + timedelta(days=index)).isoformat(),
            day_weather="ProviderSunny",
            night_weather="ProviderCloudy",
            day_temp=25 + index,
            night_temp=18 + index,
            wind_direction="east",
            wind_power="3",
        )
        for index in range(days)
    ]


def test_trip_planner_generates_mock_plan_without_api_keys():
    request = TripPlanRequest(
        city="北京",
        start_date="2026-06-01",
        end_date="2026-06-03",
        days=3,
        preferences="历史文化",
        budget="中等",
        transportation="公共交通",
        accommodation="经济型酒店",
    )

    planner = TripPlannerAgent(enable_external_services=False)
    plan = planner.plan_trip(request)

    assert plan.city == "北京"
    assert len(plan.days) == 3
    assert len(plan.weather_info) == 3
    assert all(2 <= len(day.attractions) <= 3 for day in plan.days)
    assert plan.budget is not None
    assert plan.budget.total == (
        plan.budget.total_attractions
        + plan.budget.total_hotels
        + plan.budget.total_meals
        + plan.budget.total_transportation
    )


def test_weather_query_ignores_external_forecast_that_does_not_cover_request_dates():
    request = _ascii_request()
    provider_weather = _weather_from("2026-07-12", 4)
    agent = WeatherQueryAgent(FakeWeatherService(provider_weather), enable_external_services=True)

    weather = agent.run(request)

    assert [item.date for item in weather] == [
        "2026-07-23",
        "2026-07-24",
        "2026-07-25",
        "2026-07-26",
        "2026-07-27",
    ]


def test_weather_query_falls_back_when_external_forecast_is_missing():
    request = _ascii_request()
    agent = WeatherQueryAgent(FakeWeatherService(None), enable_external_services=True)

    weather = agent.run(request)

    assert [item.date for item in weather] == [
        "2026-07-23",
        "2026-07-24",
        "2026-07-25",
        "2026-07-26",
        "2026-07-27",
    ]


def test_weather_query_uses_external_forecast_when_it_covers_request_dates():
    request = _ascii_request()
    provider_weather = _weather_from("2026-07-23", 5)
    agent = WeatherQueryAgent(FakeWeatherService(provider_weather), enable_external_services=True)

    weather = agent.run(request)

    assert weather == provider_weather


def test_trip_planner_async_entry_uses_orchestration_without_api_keys():
    request = TripPlanRequest(
        city="北京",
        start_date="2026-06-01",
        end_date="2026-06-03",
        days=3,
        preferences="历史文化",
        budget="中等",
        transportation="公共交通",
        accommodation="经济型酒店",
    )

    planner = TripPlannerAgent(enable_external_services=False)
    plan = asyncio.run(planner.aplan_trip(request))

    assert plan.city == "北京"
    assert len(plan.days) == 3
    assert len(plan.weather_info) == 3


def test_build_planner_query_contains_agent_outputs():
    request = TripPlanRequest(
        city="上海",
        start_date="2026-07-01",
        end_date="2026-07-02",
        days=2,
        preferences="美食,城市漫步",
        budget="舒适",
        transportation="地铁",
        accommodation="精品酒店",
    )
    planner = TripPlannerAgent(enable_external_services=False)

    query = planner._build_planner_query(
        request=request,
        attraction_response="外滩; 豫园",
        weather_response="晴 28度",
        hotel_response="人民广场附近精品酒店",
    )

    assert "上海" in query
    assert "外滩; 豫园" in query
    assert "晴 28度" in query
    assert "人民广场附近精品酒店" in query


def test_mock_plan_overall_suggestions_avoid_forbidden_templates():
    request = TripPlanRequest(
        city="北京",
        start_date="2026-06-01",
        end_date="2026-06-03",
        days=3,
        preferences="历史文化",
        budget="中等",
        transportation="公共交通",
        accommodation="经济型酒店",
    )

    planner = TripPlannerAgent(enable_external_services=False)
    plan = planner.plan_trip(request)

    forbidden_phrases = [
        "建议每天预留30-60分钟机动时间",
        "根据XX偏好搜索得到的目的地",
        "适合XX类型游客的景点",
        "祝您旅途愉快，玩得开心",
        "请注意安全，保管好随身物品",
    ]
    suggestion_lines = [line for line in plan.overall_suggestions.splitlines() if line.strip()]

    assert len(suggestion_lines) >= 5
    assert all(phrase not in plan.overall_suggestions for phrase in forbidden_phrases)
    assert "北京烤鸭" in plan.overall_suggestions


def test_planner_agent_uses_legacy_prompt_when_enhanced_prompt_disabled():
    request = TripPlanRequest(
        city="北京",
        start_date="2026-06-01",
        end_date="2026-06-02",
        days=2,
        preferences="历史文化",
        budget="中等",
        transportation="公共交通",
        accommodation="经济型酒店",
    )
    llm_service = RecordingLLMService()
    planner = PlannerAgent(llm_service, use_llm=True, use_enhanced_prompt=False)

    planner.run(
        request=request,
        attractions=build_mock_attractions(request.city, request.preferences, request.days),
        weather_info=build_mock_weather(request.start_date, request.days),
        hotels=build_mock_hotels(request.city, request.accommodation, request.budget),
        planner_query="query",
    )

    assert llm_service.system_prompt == PLANNER_AGENT_PROMPT_LEGACY
    assert llm_service.user_prompt == "query"
