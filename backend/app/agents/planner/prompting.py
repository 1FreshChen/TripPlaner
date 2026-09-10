from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.models.schemas import Attraction, Hotel, TripPlanRequest, WeatherInfo

def build_planner_query(
    request: TripPlanRequest,
    attraction_response: str,
    weather_response: str,
    hotel_response: str,
    memory_context: Optional[Dict[str, Any]] = None,
    conversation_context: Optional[List[Dict[str, str]]] = None,
    convergent_tools: bool = False,
) -> str:
    memory_text = _format_memory_context(memory_context or {})
    conversation_text = _format_conversation_context(conversation_context or [])
    if convergent_tools:
        tool_policy = """
**工具收敛要求:**
1. 上面的景点、天气和酒店是可信 baseline；只在明确缺少完成 TripPlan 所需的数据时调用当前可见工具。
2. 景点候选确实不足时才扩展关键词，最多使用 2 组互补关键词；一次查询已得到足够候选后必须停止，不要仅替换近义词重复搜索。
3. 不要因为工具可用就查询天气、餐厅、路线、酒店、预算或图片；这些工具只用于补齐对应的真实缺口。
4. 工具成功返回且足以覆盖缺口后，下一步必须提交最终 TripPlan JSON。工具失败时不要用相同参数重试。
5. baseline 与工具结果名称或地址重复时合并，不要在不同行程日重复安排。
"""
    else:
        tool_policy = """
**去重与多样性要求:**
1. 上面的景点、天气和酒店是 baseline 数据，不是最终答案的全部来源。
2. 如果可以调用工具，请主动用 2-4 组不同关键词扩展候选，例如“博物馆”“艺术区”“胡同”“城市漫步”“亲子体验”等。
3. 为了增加多样性，建议替换 baseline 景点中的 40%-60%，但保留最符合用户偏好的高价值地点。
4. 对历史记忆、近期对话、同城收藏或用户明确说去过的地点做去重；确需保留时说明理由，并避免连续多天安排同质化景点。
5. 如果 baseline 与工具结果名称或地址重复，请合并为一个候选，不要在不同行程日重复安排。
6. 不要生成或查询餐厅；最终餐饮由 Planner 完成后的百度 HTTP POI 阶段统一生成。
"""
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
- amap_poi_search: 用不同关键词补充或验证景点、街区、商圈等 POI。
- baidu_direction: 计算两个地点间的实际交通距离和耗时（公交/步行/驾车/骑行）。
- amap_weather: 验证目的地天气。
- hotel_search: 补充住宿候选。
- budget_calculator: 估算预算结构。
- unsplash_image: 为景点补充图片候选。

{tool_policy}

请生成详细的旅行计划，包括每天的景点安排、住宿信息和预算明细；days[].meals 必须为 []。

**餐饮要求:**
- 不得生成餐厅名称、地址、评分、人均、营业时间或评论数。
- days[].meals 必须返回空数组；后处理会直接使用百度地图 HTTP API 生成最终三餐。

**景点真实性要求:**
- 只能选择上方 baseline 或 amap_poi_search 实际返回的高德 POI，禁止自行补写景点。
- 必须原样携带所选 POI 的 poi_id、name、address、location、rating、data_source；不要猜测或改写事实字段。
- 最终定稿会再次用高德 POI ID/宽松名称匹配核验，无法核验的景点将拒绝发布。
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
        (
            f"- poi_id={item.poi_id or 'missing'} | name={item.name} | address={item.address} | "
            f"location={item.location.longitude},{item.location.latitude} | "
            f"category={item.category or ''} | rating={item.rating if item.rating is not None else 'unknown'} | "
            f"source={item.data_source or 'unknown'} | 建议{item.visit_duration}分钟"
        )
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
