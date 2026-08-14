from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Annotated, Any, Callable, Dict, List, Literal, Optional

from pydantic import Field
from pydantic_ai import Agent, ModelRetry, PromptedOutput, RunContext, Tool, UsageLimits
from pydantic_ai.models import Model
from pydantic_ai.tools import ToolDefinition

from app.agents.critic import PlanCritic
from app.agents.llm_planner import LLMPlannerAgent
from app.agents.prompts import PLANNER_AGENT_PROMPT
from app.agents.trip_planner import (
    build_planner_query,
    summarize_attractions,
    summarize_hotels,
    summarize_weather,
)
from app.models.schemas import Attraction, CritiqueResult, Hotel, TripPlan, TripPlanRequest, WeatherInfo
from app.services.llm_service import LLMService, TokenUsage
from app.services.plan_quality import PlanQualityError, validate_trip_plan_for_request
from app.services.pydantic_ai_model import OpenAICompatiblePydanticModel
from app.tools.executor import ToolExecutor
from app.tools.registry import ToolRegistry


CONVERGENCE_INSTRUCTIONS = PLANNER_AGENT_PROMPT + """

**工具停止规则（优先级高于“可用工具”说明）:**
- baseline 已覆盖每日至少 2 个景点候选、全部日期天气且至少 1 家酒店时，信息已经足够，禁止调用工具，直接输出 TripPlan。
- 只调用本轮实际可见且能补齐明确缺口的工具；工具未出现表示当前不需要或工具阶段已经结束。
- 景点关键词扩展最多 2 组。一次成功查询已经补齐候选后必须停止，不得通过近义词、调整顺序或重复失败参数继续搜索。
- 工具返回中的 next_action 为强制执行的收敛指令。看到“finalize”后必须立刻输出最终 JSON。
- 不允许为了“也许更好”继续查天气、餐厅、路线、酒店、预算或图片；不确定的非关键细节使用谨慎措辞。
"""


TOOL_NAMES = {
    "amap_poi_search",
    "amap_weather",
    "hotel_search",
    "baidu_poi_search",
    "baidu_direction",
    "budget_calculator",
    "unsplash_image",
}


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
    added_attractions: int = 0
    added_weather: int = 0
    added_hotels: int = 0
    keyword_searches: int = 0
    attempted_tools: set[str] = field(default_factory=set)
    successful_tools: set[str] = field(default_factory=set)
    seen_results: dict[str, dict[str, Any]] = field(default_factory=dict)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)

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
            if any(word in preference for word in ("美食", "餐厅", "小吃", "咖啡")):
                optional.append("baidu_poi_search")
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
    if tool_name in {"amap_poi_search", "baidu_poi_search"}:
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


async def baidu_poi_search(
    ctx: RunContext[PlannerDeps],
    keywords: Annotated[str, Field(description="餐厅或美食关键词")],
    city: Annotated[str, Field(description="目的地城市")],
    tag: Annotated[str, Field(description="可选餐饮分类")] = "",
    sort_by: Literal["overall_rating", "taste_rating", "service_rating", "price"] = "overall_rating",
    limit: Annotated[int, Field(ge=1, le=20, description="餐厅候选数量")] = 10,
) -> dict[str, Any]:
    """仅在用户明确强调美食且餐厅细节不足时补充餐厅数据。"""
    return await _execute_tool(
        ctx,
        "baidu_poi_search",
        {"keywords": keywords, "city": city, "tag": tag, "sort_by": sort_by, "limit": limit},
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
    Tool(baidu_poi_search, name="baidu_poi_search", prepare=_prepare_tool("baidu_poi_search"), sequential=True),
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


class PydanticAIPlannerAgent(LLMPlannerAgent):
    """Typed Pydantic AI planner with bounded, staged tool discovery."""

    def __init__(
        self,
        llm_service: LLMService,
        tool_registry: ToolRegistry,
        tool_executor: ToolExecutor,
        enable_critique: bool = True,
        max_refinement_rounds: int = 3,
        min_pass_score: float = 7.0,
        request_limit: int = 12,
        tool_call_limit: int = 8,
        tool_round_limit: int = 3,
        model_factory: ModelFactory | None = None,
    ) -> None:
        super().__init__(
            llm_service,
            tool_registry,
            tool_executor,
            enable_critique=enable_critique,
            max_refinement_rounds=max_refinement_rounds,
            min_pass_score=min_pass_score,
        )
        self.request_limit = request_limit
        self.tool_call_limit = tool_call_limit
        self.tool_round_limit = tool_round_limit
        self.model_factory = model_factory or (
            lambda service, state: OpenAICompatiblePydanticModel(service, state)
        )

    async def execute(self, context: Dict[str, Any]) -> TripPlan:
        if not self.llm_service.enabled:
            raise RuntimeError("Pydantic AI planning requires an enabled LLM service")

        request: TripPlanRequest = context["request"]
        attractions: List[Attraction] = context["attraction_search"]
        weather_info: List[WeatherInfo] = context["weather_query"]
        hotels: List[Hotel] = context["hotel_recommendation"]
        state = PlannerToolState(
            request=request,
            baseline_attractions=len(attractions),
            baseline_weather=len({item.date for item in weather_info}),
            baseline_hotels=len(hotels),
            max_tool_rounds=self.tool_round_limit,
            max_tool_calls=self.tool_call_limit,
        )
        deps = PlannerDeps(request=request, tool_executor=self.tool_executor, tool_state=state)
        base_prompt = self._build_pydantic_user_prompt(
            request,
            attractions,
            weather_info,
            hotels,
            state,
            context.get("memory_context"),
            context.get("conversation_context"),
        )
        total_usage = TokenUsage(
            model=self.llm_service.model,
            provider=self.llm_service._infer_provider(),
        )

        try:
            plan, run_usage = await self._run_agent(base_prompt, deps)
            total_usage = self._add_run_usage(total_usage, run_usage)
            if self._critique_enabled():
                plan, total_usage = await self._refine_with_pydantic_critique(
                    plan,
                    request,
                    base_prompt,
                    deps,
                    total_usage,
                    context,
                )
            validate_trip_plan_for_request(
                plan,
                request,
                critique_events=context.get("plan_critique_events", []),
            )
            return plan
        except Exception:
            if state.validation_failures:
                context["trip_planner_quality_failed"] = True
            raise
        finally:
            context["trip_planner_tool_calls"] = list(state.tool_calls)
            context["trip_planner_token_usage"] = total_usage
            context.setdefault("event_log", []).append(
                {
                    "event_type": "planner_convergence",
                    "severity": "info",
                    "details": {
                        "model_requests": state.model_requests,
                        "tool_rounds": state.tool_rounds,
                        "tool_calls": state.total_tool_calls,
                        "unique_external_calls": len(state.seen_results),
                        "duplicate_calls_suppressed": sum(
                            1 for call in state.tool_calls if call.get("duplicate")
                        ),
                        "forced_finalize": state.force_finalize,
                    },
                }
            )

    async def _run_agent(self, prompt: str, deps: PlannerDeps) -> tuple[TripPlan, Any]:
        model = self.model_factory(self.llm_service, deps.tool_state)
        agent: Agent[PlannerDeps, TripPlan] = Agent(
            model,
            deps_type=PlannerDeps,
            output_type=PromptedOutput(TripPlan),
            instructions=CONVERGENCE_INSTRUCTIONS,
            tools=PYDANTIC_PLANNER_TOOLS,
            retries=2,
            name="typed_trip_planner",
        )

        @agent.output_validator
        async def validate_output(ctx: RunContext[PlannerDeps], output: TripPlan) -> TripPlan:
            try:
                validate_trip_plan_for_request(output, ctx.deps.request)
            except PlanQualityError as exc:
                ctx.deps.tool_state.validation_failures += 1
                ctx.deps.tool_state.force_finalize = True
                raise ModelRetry(
                    f"TripPlan 质量校验失败：{exc}。不得再调用工具；请仅修正完整 JSON。"
                ) from exc
            return output

        result = await agent.run(
            prompt,
            deps=deps,
            usage_limits=UsageLimits(
                request_limit=self.request_limit,
                tool_calls_limit=self.tool_call_limit,
            ),
        )
        return result.output, result.usage

    def _build_pydantic_user_prompt(
        self,
        request: TripPlanRequest,
        attractions: List[Attraction],
        weather_info: List[WeatherInfo],
        hotels: List[Hotel],
        state: PlannerToolState,
        memory_context: Optional[Dict[str, Any]],
        conversation_context: Optional[List[Dict[str, str]]],
    ) -> str:
        base = build_planner_query(
            request=request,
            attraction_response=summarize_attractions(attractions),
            weather_response=summarize_weather(weather_info),
            hotel_response=summarize_hotels(hotels),
            memory_context=memory_context,
            conversation_context=conversation_context,
            convergent_tools=True,
        )
        return f"{base}\n\n**机器判定的数据充分性:**\n{state.status_summary()}"

    async def _refine_with_pydantic_critique(
        self,
        plan: TripPlan,
        request: TripPlanRequest,
        base_user_prompt: str,
        deps: PlannerDeps,
        usage: TokenUsage,
        context: Dict[str, Any],
    ) -> tuple[TripPlan, TokenUsage]:
        critic = PlanCritic(self.llm_service)
        current_plan = plan
        revisions_used = 0
        round_index = 0
        while True:
            critique: CritiqueResult = await critic.evaluate(current_plan, request)
            self._record_critique_event(context, round_index, critique)
            if not self._should_refine(critique, revisions_used):
                if self._critique_failed(critique):
                    context["trip_planner_quality_failed"] = True
                    raise PlanQualityError(
                        f"审查器仍要求修改或评分过低：needs_revision={critique.needs_revision}, "
                        f"average_score={critique.average_score}, summary={critique.revision_summary}"
                    )
                return current_plan, usage

            deps.tool_state.force_finalize = True
            revision_prompt = self._build_revision_prompt(base_user_prompt, current_plan, critique)
            current_plan, run_usage = await self._run_agent(revision_prompt, deps)
            usage = self._add_run_usage(usage, run_usage)
            revisions_used += 1
            round_index += 1

    @staticmethod
    def _add_run_usage(usage: TokenUsage, run_usage: Any) -> TokenUsage:
        usage.prompt_tokens += int(getattr(run_usage, "input_tokens", 0) or 0)
        usage.completion_tokens += int(getattr(run_usage, "output_tokens", 0) or 0)
        usage.total_tokens = usage.prompt_tokens + usage.completion_tokens
        return usage
