from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any, Iterable

from app.models.schemas import DayPlan, TripPlan, TripPlanRequest, WeatherInfo


class PlanQualityError(ValueError):
    """Raised when a generated trip plan is structurally valid but not publishable."""


BAD_WEATHER_KEYWORDS = ("暴雨", "大雨", "中雨", "雷阵雨", "雷雨", "台风", "暴雪", "大雪", "冰雹", "沙尘", "大风")
OUTDOOR_KEYWORDS = (
    "户外",
    "海滨",
    "海滩",
    "沙滩",
    "登山",
    "山地",
    "公园",
    "徒步",
    "露营",
    "漂流",
    "湿地",
    "森林",
    "湖",
    "江",
    "河",
    "岛",
    "步道",
    "景区",
    "风景区",
)
INDOOR_OR_BACKUP_KEYWORDS = ("室内", "博物馆", "美术馆", "展览", "剧院", "商场", "科技馆", "文化馆", "纪念馆", "备选", "雨天", "如遇")

TRANSPORTATION_GROUPS = {
    "public": ("公共交通", "地铁", "公交", "巴士", "轻轨"),
    "self_drive": ("自驾", "驾车", "开车", "租车"),
    "taxi": ("打车", "出租车", "网约车"),
    "walking": ("步行", "徒步"),
}

ACCOMMODATION_GROUPS = {
    "economy": ("经济", "快捷", "青旅", "青年旅舍"),
    "boutique": ("精品",),
    "luxury": ("豪华", "五星", "高端", "度假"),
}


def validate_trip_plan_for_request(
    plan: TripPlan,
    request: TripPlanRequest,
    critique_events: Iterable[dict[str, Any]] | None = None,
) -> None:
    issues = collect_trip_plan_quality_issues(plan, request, critique_events=critique_events)
    if issues:
        raise PlanQualityError("；".join(issues))


def collect_trip_plan_quality_issues(
    plan: TripPlan,
    request: TripPlanRequest,
    critique_events: Iterable[dict[str, Any]] | None = None,
) -> list[str]:
    issues: list[str] = []
    issues.extend(_request_identity_issues(plan, request))
    issues.extend(_date_and_day_issues(plan, request))
    issues.extend(_daily_content_issues(plan))
    issues.extend(_weather_issues(plan, request))
    issues.extend(_transportation_issues(plan, request))
    issues.extend(_accommodation_issues(plan, request))
    issues.extend(_critique_issues(critique_events or []))
    return issues


def _request_identity_issues(plan: TripPlan, request: TripPlanRequest) -> list[str]:
    issues = []
    if _normalize_city(plan.city) != _normalize_city(request.city):
        issues.append(f"城市不一致：用户请求 {request.city}，计划返回 {plan.city}")
    if plan.start_date != request.start_date:
        issues.append(f"开始日期不一致：用户请求 {request.start_date}，计划返回 {plan.start_date}")
    if plan.end_date != request.end_date:
        issues.append(f"结束日期不一致：用户请求 {request.end_date}，计划返回 {plan.end_date}")
    return issues


def _date_and_day_issues(plan: TripPlan, request: TripPlanRequest) -> list[str]:
    issues = []
    expected_dates = _expected_dates(request)
    actual_dates = [day.date for day in plan.days]
    if len(plan.days) != request.days:
        issues.append(f"天数不一致：用户请求 {request.days} 天，计划返回 {len(plan.days)} 天")
    if actual_dates != expected_dates:
        issues.append(f"每日日期未覆盖请求范围：期望 {expected_dates}，实际 {actual_dates}")
    return issues


def _daily_content_issues(plan: TripPlan) -> list[str]:
    issues = []
    for day in plan.days:
        if not day.attractions:
            issues.append(f"第 {day.day_index + 1} 天没有安排景点")
    return issues


def _weather_issues(plan: TripPlan, request: TripPlanRequest) -> list[str]:
    issues = []
    expected_dates = _expected_dates(request)
    weather_dates = [item.date for item in plan.weather_info]
    if weather_dates[: len(expected_dates)] != expected_dates:
        issues.append(f"天气日期未覆盖请求范围：期望 {expected_dates}，实际 {weather_dates}")

    weather_by_date = {item.date: item for item in plan.weather_info}
    for day in plan.days:
        weather = weather_by_date.get(day.date)
        if weather and _is_bad_weather(weather) and _is_exposed_outdoor_day(day):
            issues.append(f"第 {day.day_index + 1} 天天气为 {weather.day_weather}，但安排了明显户外行程")
    return issues


def _transportation_issues(plan: TripPlan, request: TripPlanRequest) -> list[str]:
    request_group = _matched_group(request.transportation, TRANSPORTATION_GROUPS)
    issues = []
    for day in plan.days:
        actual_group = _matched_group(day.transportation, TRANSPORTATION_GROUPS)
        if request_group and actual_group and request_group != actual_group:
            issues.append(f"第 {day.day_index + 1} 天交通方式与用户偏好冲突：用户请求 {request.transportation}，计划为 {day.transportation}")
        if not day.transportation.strip():
            issues.append(f"第 {day.day_index + 1} 天缺少交通方式")
    return issues


def _accommodation_issues(plan: TripPlan, request: TripPlanRequest) -> list[str]:
    request_group = _matched_group(request.accommodation, ACCOMMODATION_GROUPS)
    issues = []
    for day in plan.days:
        day_accommodation_group = _matched_group(day.accommodation, ACCOMMODATION_GROUPS)
        accommodation_text = " ".join(
            filter(
                None,
                [
                    day.accommodation,
                    day.hotel.name if day.hotel else "",
                    day.hotel.type if day.hotel else "",
                    day.hotel.price_range if day.hotel else "",
                ],
            )
        )
        actual_group = _matched_group(accommodation_text, ACCOMMODATION_GROUPS)
        if request_group and day_accommodation_group and request_group != day_accommodation_group:
            issues.append(f"第 {day.day_index + 1} 天住宿与用户偏好冲突：用户请求 {request.accommodation}，计划为 {day.accommodation}")
            continue
        if request_group and actual_group and request_group != actual_group:
            issues.append(f"第 {day.day_index + 1} 天住宿与用户偏好冲突：用户请求 {request.accommodation}，计划为 {day.accommodation}")
        if not day.accommodation.strip():
            issues.append(f"第 {day.day_index + 1} 天缺少住宿安排")
    return issues


def _critique_issues(events: Iterable[dict[str, Any]]) -> list[str]:
    event_list = list(events)
    if not event_list:
        return []
    details = _event_details(event_list[-1])
    if details.get("needs_revision") is True:
        summary = details.get("revision_summary") or "审查器要求继续修改"
        return [f"审查器仍要求修改，不能保存 completed：{summary}"]
    return []


def _event_details(event: dict[str, Any]) -> dict[str, Any]:
    details = event.get("details")
    if details is None:
        details = event.get("details_json")
    if isinstance(details, str):
        try:
            parsed = json.loads(details)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return details if isinstance(details, dict) else {}


def _expected_dates(request: TripPlanRequest) -> list[str]:
    start = date.fromisoformat(request.start_date)
    return [(start + timedelta(days=offset)).isoformat() for offset in range(request.days)]


def _normalize_city(value: str) -> str:
    return "".join(str(value or "").split()).removesuffix("市")


def _is_bad_weather(weather: WeatherInfo) -> bool:
    text = f"{weather.day_weather} {weather.night_weather}"
    return any(keyword in text for keyword in BAD_WEATHER_KEYWORDS)


def _is_exposed_outdoor_day(day: DayPlan) -> bool:
    text = _day_text(day)
    has_outdoor = any(keyword in text for keyword in OUTDOOR_KEYWORDS)
    has_indoor_or_backup = any(keyword in text for keyword in INDOOR_OR_BACKUP_KEYWORDS)
    return has_outdoor and not has_indoor_or_backup


def _day_text(day: DayPlan) -> str:
    parts = [day.description, day.transportation, day.accommodation]
    for attraction in day.attractions:
        parts.extend([attraction.name, attraction.address, attraction.description, attraction.category or ""])
    return " ".join(part for part in parts if part)


def _matched_group(text: str, groups: dict[str, tuple[str, ...]]) -> str | None:
    normalized = str(text or "")
    for group, keywords in groups.items():
        if any(keyword in normalized for keyword in keywords):
            return group
    return None
