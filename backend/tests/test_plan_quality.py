import copy

import pytest

from app.models.schemas import DayPlan, TripPlan, TripPlanRequest
from app.services.mock_data import build_mock_attractions, build_mock_hotels, build_mock_weather
from app.services.plan_quality import (
    PlanQualityError,
    collect_trip_plan_quality_issues,
    validate_trip_plan_for_request,
)


def _request() -> TripPlanRequest:
    return TripPlanRequest(
        city="台州",
        start_date="2026-07-17",
        end_date="2026-07-19",
        days=3,
        preferences="自然风光",
        budget="中等",
        transportation="公共交通",
        accommodation="经济型酒店",
    )


def _valid_plan() -> TripPlan:
    request = _request()
    attractions = build_mock_attractions(request.city, request.preferences, request.days)
    hotels = build_mock_hotels(request.city, request.accommodation, request.budget)
    weather = build_mock_weather(request.start_date, request.days)
    days = [
        DayPlan(
            date=item.date,
            day_index=index,
            description=f"第{index + 1}天安排室内外结合的台州自然风光行程。",
            transportation="公共交通：公交和地铁换乘",
            accommodation="经济型酒店",
            hotel=hotels[index % len(hotels)],
            attractions=[attractions[index], attractions[(index + 1) % len(attractions)]],
            meals=[],
        )
        for index, item in enumerate(weather)
    ]
    return TripPlan(
        city=request.city,
        start_date=request.start_date,
        end_date=request.end_date,
        days=days,
        weather_info=weather,
        overall_suggestions="按天气安排室内外节奏，优先公共交通。",
        budget=None,
    )


def _issues_for(plan: TripPlan, request: TripPlanRequest | None = None) -> list[str]:
    return collect_trip_plan_quality_issues(plan, request or _request())


def test_validate_trip_plan_accepts_complete_matching_plan():
    validate_trip_plan_for_request(_valid_plan(), _request())


def test_validate_trip_plan_rejects_city_mismatch():
    plan = _valid_plan().model_copy(deep=True)
    plan.city = "北京"

    issues = _issues_for(plan)

    assert any("城市" in issue for issue in issues)


def test_validate_trip_plan_rejects_wrong_day_count():
    plan = _valid_plan().model_copy(deep=True)
    plan.days = plan.days[:2]

    issues = _issues_for(plan)

    assert any("天数" in issue for issue in issues)


def test_validate_trip_plan_rejects_day_without_attractions():
    plan = _valid_plan().model_copy(deep=True)
    plan.days[1].attractions = []

    issues = _issues_for(plan)

    assert any("景点" in issue for issue in issues)


def test_validate_trip_plan_rejects_dates_that_do_not_cover_request_range():
    plan = _valid_plan().model_copy(deep=True)
    plan.days[2].date = "2026-07-21"

    issues = _issues_for(plan)

    assert any("日期" in issue for issue in issues)


def test_validate_trip_plan_rejects_latest_critique_that_still_needs_revision():
    critique_events = [
        {"details": {"needs_revision": True, "revision_summary": "第 2 天安排过紧"}},
    ]

    with pytest.raises(PlanQualityError) as exc:
        validate_trip_plan_for_request(_valid_plan(), _request(), critique_events=critique_events)

    assert "审查器" in str(exc.value)


def test_validate_trip_plan_uses_latest_critique_result_only():
    critique_events = [
        {"details": {"needs_revision": True, "revision_summary": "初版需要修改"}},
        {"details": {"needs_revision": False, "revision_summary": "修订后通过"}},
    ]

    validate_trip_plan_for_request(_valid_plan(), _request(), critique_events=critique_events)


def test_validate_trip_plan_rejects_weather_conflict_for_outdoor_day():
    plan = _valid_plan().model_copy(deep=True)
    plan.weather_info[0].day_weather = "暴雨"
    plan.days[0].description = "全天海滨公园徒步和山地步道户外游览"
    first_attraction = copy.deepcopy(plan.days[0].attractions[0])
    first_attraction.name = "台州海滨公园"
    first_attraction.category = "户外景区"
    first_attraction.description = "海边徒步和露天观景"
    plan.days[0].attractions = [first_attraction]

    issues = _issues_for(plan)

    assert any("天气" in issue for issue in issues)


def test_validate_trip_plan_rejects_accommodation_conflict():
    plan = _valid_plan().model_copy(deep=True)
    plan.days[0].accommodation = "五星级豪华度假酒店"

    issues = _issues_for(plan)

    assert any("住宿" in issue for issue in issues)


def test_validate_trip_plan_rejects_transportation_conflict():
    plan = _valid_plan().model_copy(deep=True)
    plan.days[0].transportation = "全程自驾"

    issues = _issues_for(plan)

    assert any("交通" in issue for issue in issues)
