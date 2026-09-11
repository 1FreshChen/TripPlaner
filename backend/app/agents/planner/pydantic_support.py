from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Annotated, Any, Callable, Literal

from pydantic import Field
from pydantic_ai import RunContext, Tool
from pydantic_ai.models import Model
from pydantic_ai.tools import ToolDefinition

from app.models.schemas import TripPlanRequest
from app.services.llm_service import LLMService
from app.tools.executor import ToolExecutor


@dataclass

class PlannerToolState:
    request: TripPlanRequest
    baseline_attractions: int
    baseline_weather: int
    baseline_hotels: int
    max_tool_rounds: int = 3
    max_tool_calls: int = 6
    model_requests: int = 0
    tool_rounds: int = 0
    total_tool_calls: int = 0
    force_finalize: bool = False
    validation_failures: int = 0
    current_stage: str = "initializing"
    added_attractions: int = 0
    added_weather: int = 0
    added_hotels: int = 0
    keyword_searches: int = 0
    attempted_tools: set[str] = field(default_factory=set)
    successful_tools: set[str] = field(default_factory=set)
    seen_results: dict[str, dict[str, Any]] = field(default_factory=dict)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    amap_pois: list[dict[str, Any]] = field(default_factory=list)

    @property
    def required_attractions(self) -> int:
        return max(2, min(self.request.days * 2, 10))

    @property
    def baseline_is_sufficient(self) -> bool:
        return not self._missing_requirements()

    def _missing_requirements(self) -> set[str]:
        missing: set[str] = set()
        if self.baseline_attractions + self.added_attractions < self.required_attractions:
            missing.add("attractions")
        if self.baseline_weather + self.added_weather < self.request.days:
            missing.add("weather")
        if self.baseline_hotels + self.added_hotels < 1:
            missing.add("hotels")
        return missing

    def visible_tool_names(self) -> set[str]:
        if (
            self.force_finalize
            or self.tool_rounds >= self.max_tool_rounds
            or self.total_tool_calls >= self.max_tool_calls
        ):
            return set()

        visible: list[str] = []
        missing = self._missing_requirements()
        if "attractions" in missing and self.keyword_searches < 2:
            visible.append("amap_poi_search")
        if "weather" in missing and "amap_weather" not in self.attempted_tools:
            visible.append("amap_weather")
        if "hotels" in missing and "hotel_search" not in self.attempted_tools:
            visible.append("hotel_search")

        # Optional enrichment is opt-in and only considered after all required
        # baseline gaps are resolved. This prevents seven-tool fan-out.
        if not missing:
            preference = self.request.preferences.lower()
            optional: list[str] = []
            if any(word in preference for word in ("路线", "换乘", "自驾", "步行距离")):
                optional.append("baidu_direction")
            if any(word in preference for word in ("精确预算", "预算上限", "省钱")) or re.search(
                r"\d", self.request.budget
            ):
                optional.append("budget_calculator")
            if any(word in preference for word in ("摄影", "拍照", "图片", "出片")):
                optional.append("unsplash_image")
            visible.extend(name for name in optional if name not in self.attempted_tools)

        # Never expose more than the tools needed for the current stage.
        return set(visible[:3])

    def record_result(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        result: dict[str, Any],
        *,
        tool_call_id: str | None,
        duplicate: bool = False,
        skipped: bool = False,
    ) -> None:
        entry: dict[str, Any] = {
            "tool": tool_name,
            "arguments": arguments,
            "id": tool_call_id,
        }
        error = result.get("error")
        if duplicate:
            entry["duplicate"] = True
            entry["error"] = error or "duplicate tool call suppressed"
        elif skipped:
            entry["error"] = error or "tool call budget exhausted"
        elif error:
            entry["error"] = str(error)
        self.tool_calls.append(entry)

        if duplicate or skipped:
            if not self.visible_tool_names():
                self.force_finalize = True
            return
        self.attempted_tools.add(tool_name)
        success = result.get("success", "error" not in result) is not False
        if success:
            self.successful_tools.add(tool_name)
            count = max(int(result.get("count") or 0), 0)
            if tool_name == "amap_poi_search":
                self.added_attractions += count
                self.amap_pois.extend(
                    poi for poi in (result.get("pois") or []) if isinstance(poi, dict)
                )
            elif tool_name == "amap_weather":
                self.added_weather += count
            elif tool_name == "hotel_search":
                self.added_hotels += count

        if not self.visible_tool_names():
            self.force_finalize = True

    def status_summary(self) -> str:
        missing = sorted(self._missing_requirements())
        return json.dumps(
            {
                "baseline_sufficient": not missing,
                "missing": missing,
                "baseline_counts": {
                    "attractions": self.baseline_attractions,
                    "weather_days": self.baseline_weather,
                    "hotels": self.baseline_hotels,
                },
                "initial_visible_tools": sorted(self.visible_tool_names()),
                "stop_when": "missing 为空或工具预算到达上限时，立即输出 TripPlan JSON",
            },
            ensure_ascii=False,
        )

    def diagnostics(self) -> dict[str, Any]:
        return {
            "stage": self.current_stage,
            "model_requests": self.model_requests,
            "tool_rounds": self.tool_rounds,
            "tool_calls": self.total_tool_calls,
            "validation_failures": self.validation_failures,
            "forced_finalize": self.force_finalize,
            "verified_amap_pois": len(self.amap_pois),
        }


@dataclass
class PlannerDeps:
    request: TripPlanRequest
    tool_executor: ToolExecutor
    tool_state: PlannerToolState


def _canonical_value(key: str, value: Any) -> Any:
    if isinstance(value, str):
        normalized = " ".join(value.strip().lower().split())
        if key in {"keywords", "tag"}:
            tokens = [item.strip() for item in re.split(r"[|,，、]+", normalized) if item.strip()]
            return "|".join(sorted(set(tokens)))
        return normalized
    if isinstance(value, dict):
        return {item_key: _canonical_value(item_key, item) for item_key, item in sorted(value.items())}
    if isinstance(value, list):
        return [_canonical_value(key, item) for item in value]
    return value


def canonical_tool_signature(tool_name: str, arguments: dict[str, Any]) -> str:
    normalized = {key: _canonical_value(key, value) for key, value in sorted(arguments.items())}
    return f"{tool_name}:{json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(',', ':'))}"


def _result_summary(result: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in result.items()
        if key in {"success", "count", "city", "source", "error", "query"}
    }


async def _execute_tool(
    ctx: RunContext[PlannerDeps],
    tool_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    state = ctx.deps.tool_state
    if state.total_tool_calls >= state.max_tool_calls:
        state.force_finalize = True
        result = {
            "success": False,
            "error": "工具调用预算已耗尽",
            "next_action": "finalize",
        }
        state.record_result(
            tool_name,
            arguments,
            result,
            tool_call_id=ctx.tool_call_id,
            skipped=True,
        )
        return result

    state.total_tool_calls += 1
    if tool_name == "amap_poi_search":
        state.keyword_searches += 1

    signature = canonical_tool_signature(tool_name, arguments)
    previous = state.seen_results.get(signature)
    if previous is not None:
        result = {
            "success": False,
            "duplicate": True,
            "error": "相同工具和参数已经执行，已阻止重复外部调用",
            "previous_result_summary": _result_summary(previous),
            "next_action": "finalize_or_use_different_missing_dimension",
        }
        state.record_result(
            tool_name,
            arguments,
            result,
            tool_call_id=ctx.tool_call_id,
            duplicate=True,
        )
        return result

    raw_result = await ctx.deps.tool_executor.execute_by_name(tool_name, **arguments)
    result = dict(raw_result) if isinstance(raw_result, dict) else {"data": raw_result, "success": True}
    state.seen_results[signature] = result
    state.record_result(tool_name, arguments, result, tool_call_id=ctx.tool_call_id)
    result = dict(result)
    result["next_action"] = "finalize" if state.force_finalize else "fill_only_remaining_gap"
    return result


async def amap_poi_search(
    ctx: RunContext[PlannerDeps],
    keywords: Annotated[str, Field(description="景点或街区搜索关键词；最多使用两组互补关键词")],
    city: Annotated[str, Field(description="目的地城市")],
    offset: Annotated[int, Field(ge=1, le=25, description="返回数量上限")] = 10,
) -> dict[str, Any]:
    """仅在 baseline 景点数量不足时补充 POI；已有足够候选时不得调用。"""
    return await _execute_tool(ctx, "amap_poi_search", {"keywords": keywords, "city": city, "offset": offset})


async def amap_weather(
    ctx: RunContext[PlannerDeps],
    city: Annotated[str, Field(description="需要补齐天气的目的地城市")],
) -> dict[str, Any]:
    """仅在 baseline 天气未覆盖全部行程日期时查询天气。"""
    return await _execute_tool(ctx, "amap_weather", {"city": city})


async def hotel_search(
    ctx: RunContext[PlannerDeps],
    city: Annotated[str, Field(description="目的地城市")],
    hotel_type: Annotated[str, Field(description="住宿类型")] = "经济型酒店",
    limit: Annotated[int, Field(ge=1, le=10, description="酒店候选数量")] = 5,
) -> dict[str, Any]:
    """仅在 baseline 没有酒店候选时搜索住宿。"""
    return await _execute_tool(
        ctx,
        "hotel_search",
        {"city": city, "hotel_type": hotel_type, "limit": limit},
    )


async def baidu_direction(
    ctx: RunContext[PlannerDeps],
    origin: Annotated[str, Field(description="起点名称或地址")],
    destination: Annotated[str, Field(description="终点名称或地址")],
    city: Annotated[str, Field(description="目的地城市")],
    mode: Literal["driving", "walking", "transit", "riding"] = "transit",
) -> dict[str, Any]:
    """仅在用户明确要求路线、换乘或距离细节时查询一段关键路线。"""
    return await _execute_tool(
        ctx,
        "baidu_direction",
        {"origin": origin, "destination": destination, "city": city, "mode": mode},
    )


async def budget_calculator(
    ctx: RunContext[PlannerDeps],
    days: Annotated[int, Field(ge=1, description="旅行天数")],
    transportation: Literal["步行", "公共交通", "地铁", "打车", "自驾"],
    attraction_count: Annotated[int, Field(ge=0, description="景点总数")],
    hotel_price_per_night: Annotated[int, Field(ge=0, description="每晚住宿费用")],
    meal_level: Literal["经济", "中等", "舒适", "高"],
    avg_ticket_price: Annotated[int, Field(ge=0, description="平均门票价格")] = 0,
) -> dict[str, Any]:
    """仅在用户给出明确预算约束、需要精确核算时计算一次预算。"""
    return await _execute_tool(
        ctx,
        "budget_calculator",
        {
            "days": days,
            "transportation": transportation,
            "attraction_count": attraction_count,
            "hotel_price_per_night": hotel_price_per_night,
            "meal_level": meal_level,
            "avg_ticket_price": avg_ticket_price,
        },
    )


async def unsplash_image(
    ctx: RunContext[PlannerDeps],
    query: Annotated[str, Field(description="景点名称与城市")],
    count: Annotated[int, Field(ge=1, le=5, description="图片数量")] = 1,
) -> dict[str, Any]:
    """仅在用户明确要求摄影或图片体验时为一个重点景点补充图片。"""
    return await _execute_tool(ctx, "unsplash_image", {"query": query, "count": count})


def _prepare_tool(tool_name: str) -> Callable[[RunContext[PlannerDeps], ToolDefinition], ToolDefinition | None]:
    def prepare(ctx: RunContext[PlannerDeps], tool_def: ToolDefinition) -> ToolDefinition | None:
        return tool_def if tool_name in ctx.deps.tool_state.visible_tool_names() else None

    return prepare


PYDANTIC_PLANNER_TOOLS: list[Tool[PlannerDeps]] = [
    Tool(amap_poi_search, name="amap_poi_search", prepare=_prepare_tool("amap_poi_search"), sequential=True),
    Tool(amap_weather, name="amap_weather", prepare=_prepare_tool("amap_weather"), sequential=True),
    Tool(hotel_search, name="hotel_search", prepare=_prepare_tool("hotel_search"), sequential=True),
    Tool(baidu_direction, name="baidu_direction", prepare=_prepare_tool("baidu_direction"), sequential=True),
    Tool(
        budget_calculator,
        name="budget_calculator",
        prepare=_prepare_tool("budget_calculator"),
        sequential=True,
    ),
    Tool(unsplash_image, name="unsplash_image", prepare=_prepare_tool("unsplash_image"), sequential=True),
]


ModelFactory = Callable[[LLMService, PlannerToolState], Model]

__all__ = [
    "ModelFactory",
    "PYDANTIC_PLANNER_TOOLS",
    "PlannerDeps",
    "PlannerToolState",
    "canonical_tool_signature",
    "_execute_tool",
]
