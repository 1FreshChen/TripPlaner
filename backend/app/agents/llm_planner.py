from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from pydantic import ValidationError

from app.agents.critic import PlanCritic
from app.agents.prompts import PLANNER_AGENT_PROMPT
from app.agents.trip_planner import (
    build_planner_query,
    summarize_attractions,
    summarize_hotels,
    summarize_weather,
)
from app.models.schemas import Attraction, CritiqueResult, Hotel, TripPlan, TripPlanRequest, WeatherInfo
from app.orchestration.base import BaseAgent
from app.services.llm_service import LLMService, TokenUsage
from app.services.plan_quality import PlanQualityError, validate_trip_plan_for_request
from app.tools.executor import ToolExecutor
from app.tools.registry import ToolRegistry


class LLMPlannerAgent(BaseAgent):
    """使用 LLM function calling 和现有工具生成旅行计划。"""

    name = "trip_planner"

    def __init__(
        self,
        llm_service: LLMService,
        tool_registry: ToolRegistry,
        tool_executor: ToolExecutor,
        enable_critique: bool = True,
        max_refinement_rounds: int = 3,
        min_pass_score: float = 7.0,
    ):
        self.llm_service = llm_service
        self.tool_registry = tool_registry
        self.tool_executor = tool_executor
        self.enable_critique = enable_critique
        self.max_refinement_rounds = max_refinement_rounds
        self.min_pass_score = min_pass_score

    async def execute(self, context: Dict[str, Any]) -> TripPlan:
        if not self.llm_service.enabled:
            raise RuntimeError("LLM tool planning requires an enabled LLM service")

        request: TripPlanRequest = context["request"]
        attractions: List[Attraction] = context["attraction_search"]
        weather_info: List[WeatherInfo] = context["weather_query"]
        hotels: List[Hotel] = context["hotel_recommendation"]
        user_prompt = self._build_user_prompt(
            request=request,
            attractions=attractions,
            weather_info=weather_info,
            hotels=hotels,
            memory_context=context.get("memory_context"),
            conversation_context=context.get("conversation_context"),
        )
        tools = self.tool_registry.get_openai_functions()
        final_text, tool_calls, usage = await self.llm_service.chat_with_tools(
            system_prompt=PLANNER_AGENT_PROMPT,
            user_prompt=user_prompt,
            tools=tools,
            tool_executor=self.tool_executor,
            max_tool_rounds=5,
            response_format={"type": "json_object"},
        )
        try:
            trip_plan, correction_usage = await self._parse_and_validate(final_text, request)
        except (json.JSONDecodeError, ValidationError, TypeError, ValueError, PlanQualityError):
            context["trip_planner_quality_failed"] = True
            raise
        if correction_usage:
            usage = self._merge_usage(usage, correction_usage)
        all_tool_calls = list(tool_calls)
        if self._critique_enabled():
            trip_plan, usage = await self._refine_with_critique(
                plan=trip_plan,
                request=request,
                base_user_prompt=user_prompt,
                tools=tools,
                tool_calls=all_tool_calls,
                usage=usage,
                context=context,
            )
        context["trip_planner_tool_calls"] = all_tool_calls
        context["trip_planner_token_usage"] = usage
        return trip_plan

    def _build_user_prompt(
        self,
        request: TripPlanRequest,
        attractions: List[Attraction],
        weather_info: List[WeatherInfo],
        hotels: List[Hotel],
        memory_context: Optional[Dict[str, Any]] = None,
        conversation_context: Optional[List[Dict[str, str]]] = None,
    ) -> str:
        return build_planner_query(
            request=request,
            attraction_response=summarize_attractions(attractions),
            weather_response=summarize_weather(weather_info),
            hotel_response=summarize_hotels(hotels),
            memory_context=memory_context,
            conversation_context=conversation_context,
        )

    async def _parse_and_validate(
        self,
        text: str,
        request: TripPlanRequest,
    ) -> tuple[TripPlan, Optional[TokenUsage]]:
        try:
            trip_plan = self._load_trip_plan(text)
            validate_trip_plan_for_request(trip_plan, request)
            return trip_plan, None
        except (json.JSONDecodeError, ValidationError, TypeError, ValueError, PlanQualityError) as exc:
            issue_type = "质量校验失败" if isinstance(exc, PlanQualityError) else "格式错误"
            request_payload = json.dumps(request.model_dump(mode="json"), ensure_ascii=False, indent=2)
            correction_prompt = (
                f"你的输出有{issue_type}：{exc}。\n"
                f"原始用户请求如下，修正后的 TripPlan 必须逐项匹配：\n{request_payload}\n"
                "请修正后重新输出完整的 TripPlan JSON。只返回 JSON 对象，不要输出 Markdown 或解释。"
            )
            corrected_text, _, correction_usage = await self.llm_service.chat_with_tools(
                system_prompt=PLANNER_AGENT_PROMPT,
                user_prompt=correction_prompt,
                tools=[],
                tool_executor=self.tool_executor,
                max_tool_rounds=1,
            )
            trip_plan = self._load_trip_plan(corrected_text)
            validate_trip_plan_for_request(trip_plan, request)
            return trip_plan, correction_usage

    def _load_trip_plan(self, text: str) -> TripPlan:
        payload = json.loads(self._strip_json_fence(text))
        return TripPlan.model_validate(payload)

    async def _refine_with_critique(
        self,
        plan: TripPlan,
        request: TripPlanRequest,
        base_user_prompt: str,
        tools: List[Dict[str, Any]],
        tool_calls: List[Dict[str, Any]],
        usage: TokenUsage,
        context: Dict[str, Any],
    ) -> tuple[TripPlan, TokenUsage]:
        critic = PlanCritic(self.llm_service)
        current_plan = plan
        revisions_used = 0
        round_index = 0

        while True:
            critique = await critic.evaluate(current_plan, request)
            self._record_critique_event(context, round_index, critique)
            if not self._should_refine(critique, revisions_used):
                if self._critique_failed(critique):
                    context["trip_planner_quality_failed"] = True
                    raise PlanQualityError(
                        f"审查器仍要求修改或评分过低：needs_revision={critique.needs_revision}, "
                        f"average_score={critique.average_score}, summary={critique.revision_summary}"
                    )
                validate_trip_plan_for_request(
                    current_plan,
                    request,
                    critique_events=context.get("plan_critique_events", []),
                )
                return current_plan, usage

            revision_prompt = self._build_revision_prompt(base_user_prompt, current_plan, critique)
            revised_text, revision_tool_calls, revision_usage = await self.llm_service.chat_with_tools(
                system_prompt=PLANNER_AGENT_PROMPT,
                user_prompt=revision_prompt,
                tools=tools,
                tool_executor=self.tool_executor,
                max_tool_rounds=5,
            )
            tool_calls.extend(revision_tool_calls)
            usage = self._merge_usage(usage, revision_usage)
            try:
                current_plan, correction_usage = await self._parse_and_validate(revised_text, request)
            except (json.JSONDecodeError, ValidationError, TypeError, ValueError, PlanQualityError):
                context["trip_planner_quality_failed"] = True
                raise
            if correction_usage:
                usage = self._merge_usage(usage, correction_usage)
            revisions_used += 1
            round_index += 1

    def _critique_enabled(self) -> bool:
        return self.enable_critique and self.max_refinement_rounds >= 0 and hasattr(self.llm_service, "generate_json")

    def _should_refine(self, critique: CritiqueResult, revisions_used: int) -> bool:
        return (
            self._critique_failed(critique)
            and revisions_used < self.max_refinement_rounds
        )

    def _critique_failed(self, critique: CritiqueResult) -> bool:
        return critique.needs_revision or critique.average_score < self.min_pass_score

    def _build_revision_prompt(
        self,
        base_user_prompt: str,
        previous_plan: TripPlan,
        critique: CritiqueResult,
    ) -> str:
        critique_payload = critique.model_dump(mode="json")
        critique_payload["average_score"] = critique.average_score
        issues = "\n".join(
            f"{index + 1}. [{issue.severity}] Day {issue.day}: {issue.problem}；建议：{issue.suggestion}"
            for index, issue in enumerate(critique.issues)
        )
        return (
            "上一版行程存在以下问题，请修正后重新输出完整 TripPlan JSON。\n"
            "必须延续原始用户需求和 baseline 数据，只返回 JSON 对象，不要输出 Markdown 或解释。\n\n"
            f"原始规划上下文:\n{base_user_prompt}\n\n"
            f"上一版行程:\n{self._to_json(previous_plan)}\n\n"
            f"审视结果:\n{json.dumps(critique_payload, ensure_ascii=False, indent=2)}\n\n"
            f"问题清单:\n{issues or '无'}"
        )

    def _record_critique_event(self, context: Dict[str, Any], round_index: int, critique: CritiqueResult) -> None:
        event = {
            "event_type": "plan_critique",
            "severity": "warning" if critique.needs_revision else "info",
            "details": {
                "round": round_index,
                "scores": critique.scores.model_dump(mode="json"),
                "average_score": critique.average_score,
                "needs_revision": critique.needs_revision,
                "issues_count": len(critique.issues),
                "revision_summary": critique.revision_summary,
            },
        }
        context.setdefault("event_log", []).append(event)
        context.setdefault("plan_critique_events", []).append(event)

    @staticmethod
    def _to_json(value: Any) -> str:
        if hasattr(value, "model_dump"):
            value = value.model_dump(mode="json")
        return json.dumps(value, ensure_ascii=False, indent=2)

    @staticmethod
    def _merge_usage(primary: TokenUsage, extra: TokenUsage) -> TokenUsage:
        primary.prompt_tokens += extra.prompt_tokens
        primary.completion_tokens += extra.completion_tokens
        primary.total_tokens += extra.total_tokens
        return primary

    @staticmethod
    def _strip_json_fence(text: str) -> str:
        cleaned = (text or "").strip()
        if cleaned.startswith("```"):
            lines = cleaned.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            cleaned = "\n".join(lines).strip()
        return cleaned
