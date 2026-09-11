from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic_ai import Agent, ModelRetry, PromptedOutput, RunContext, UsageLimits

from app.agents.critic import PlanCritic
from app.agents.llm_planner import LLMPlannerAgent
from app.agents.planner.pydantic_support import (
    ModelFactory,
    PYDANTIC_PLANNER_TOOLS,
    PlannerDeps,
    PlannerToolState,
)
from app.agents.planner.prompting import (
    build_planner_query,
    summarize_attractions,
    summarize_hotels,
    summarize_weather,
)
from app.agents.planner.validation import validate_planner_output
from app.agents.prompts import PLANNER_AGENT_PROMPT
from app.models.schemas import Attraction, CritiqueResult, Hotel, TripPlan, TripPlanRequest, WeatherInfo
from app.services.llm_service import LLMService, TokenUsage
from app.services.plan_quality import PlanQualityError
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
        model_request_timeout_seconds: float = 90.0,
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
        self.model_request_timeout_seconds = model_request_timeout_seconds
        self.model_factory = model_factory or (
            lambda service, state: OpenAICompatiblePydanticModel(
                service,
                state,
                timeout_seconds=self.model_request_timeout_seconds,
            )
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
            state.current_stage = "initial_generation"
            plan, run_usage = await self._run_agent(
                base_prompt,
                deps,
                request_limit=context.get("planner_request_limit"),
            )
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
            state.current_stage = "final_validation"
            validate_planner_output(
                plan,
                request,
                critique_events=context.get("plan_critique_events", []),
            )
            state.current_stage = "completed"
            return plan
        except Exception:
            if state.validation_failures:
                context["trip_planner_quality_failed"] = True
            raise
        finally:
            context["trip_planner_tool_calls"] = list(state.tool_calls)
            context["trip_planner_token_usage"] = total_usage
            context["trip_planner_diagnostics"] = state.diagnostics()
            context["planner_attraction_pois"] = [
                *(context.get("planner_attraction_pois") or []),
                *state.amap_pois,
            ]
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

    async def _run_agent(
        self,
        prompt: str,
        deps: PlannerDeps,
        *,
        request_limit: int | None = None,
    ) -> tuple[TripPlan, Any]:
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
                validate_planner_output(output, ctx.deps.request)
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
                request_limit=max(1, request_limit or self.request_limit),
                tool_calls_limit=self.tool_call_limit,
            ),
        )
        return result.output, result.usage

    async def refine_once(
        self,
        context: Dict[str, Any],
        previous_plan: TripPlan,
        critique: CritiqueResult,
    ) -> TripPlan:
        """Run one tool-free typed revision and expose its own diagnostics."""
        request: TripPlanRequest = context["request"]
        attractions: List[Attraction] = context["attraction_search"]
        weather_info: List[WeatherInfo] = context["weather_query"]
        hotels: List[Hotel] = context["hotel_recommendation"]
        state = PlannerToolState(
            request=request,
            baseline_attractions=len(attractions),
            baseline_weather=len({item.date for item in weather_info}),
            baseline_hotels=len(hotels),
            max_tool_rounds=0,
            max_tool_calls=self.tool_call_limit,
            force_finalize=True,
            current_stage="refinement_generation",
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
        revision_prompt = self._build_revision_prompt(base_prompt, previous_plan, critique)
        total_usage = TokenUsage(
            model=self.llm_service.model,
            provider=self.llm_service._infer_provider(),
        )
        try:
            plan, run_usage = await self._run_agent(
                revision_prompt,
                deps,
                request_limit=context.get("planner_request_limit"),
            )
            total_usage = self._add_run_usage(total_usage, run_usage)
            state.current_stage = "refinement_completed"
            return plan
        finally:
            context["trip_planner_tool_calls"] = list(state.tool_calls)
            context["trip_planner_token_usage"] = total_usage
            context["trip_planner_diagnostics"] = state.diagnostics()
            context["planner_attraction_pois"] = [
                *(context.get("planner_attraction_pois") or []),
                *state.amap_pois,
            ]

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
            deps.tool_state.current_stage = f"critique_round_{round_index + 1}"
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
            deps.tool_state.current_stage = f"refinement_round_{round_index + 1}"
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
