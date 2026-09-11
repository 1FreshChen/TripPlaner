from __future__ import annotations

import asyncio
import inspect
import json
import logging
from typing import Any

from pydantic import ValidationError

from app.agents.prompts import CRITIC_AGENT_PROMPT
from app.models.schemas import CritiqueResult, CritiqueScores, TripPlan, TripPlanRequest
from app.services.llm_service import LLMService

logger = logging.getLogger(__name__)


class PlanCritic:
    """行程质量审视 Agent。"""

    def __init__(self, llm_service: LLMService):
        self.llm_service = llm_service

    async def evaluate(
        self,
        plan: TripPlan,
        request: TripPlanRequest,
        *,
        strict: bool = False,
        timeout_seconds: float | None = 60.0,
    ) -> CritiqueResult:
        """对行程进行多维度评估。"""
        prompt = self._build_critique_prompt(plan, request)
        generate_json_async = getattr(self.llm_service, "generate_json_async", None)
        if callable(generate_json_async):
            result = await generate_json_async(
                system_prompt=CRITIC_AGENT_PROMPT,
                user_prompt=prompt,
                timeout_seconds=timeout_seconds,
            )
        else:
            result = None
        generate_json = self.llm_service.generate_json
        if generate_json_async is None and inspect.iscoroutinefunction(generate_json):
            result = await generate_json(system_prompt=CRITIC_AGENT_PROMPT, user_prompt=prompt)
        elif generate_json_async is None:
            result = await asyncio.to_thread(
                generate_json,
                system_prompt=CRITIC_AGENT_PROMPT,
                user_prompt=prompt,
            )
        if inspect.isawaitable(result):
            result = await result
        if result is None:
            if strict:
                raise RuntimeError("Plan critic did not return a result")
            logger.warning("Plan critic returned no result; keeping current plan")
            return self._pass_result("审视服务未返回结果，保留当前行程")
        try:
            return CritiqueResult.model_validate(result)
        except ValidationError as exc:
            if strict:
                raise RuntimeError("Plan critic returned an invalid result") from exc
            logger.warning("Plan critic returned invalid result; keeping current plan: %s", exc)
            return self._pass_result("审视结果格式无效，保留当前行程")

    def _build_critique_prompt(self, plan: TripPlan, request: TripPlanRequest) -> str:
        request_json = self._to_json(request)
        plan_json = self._to_json(plan)
        return (
            "请审视下面的旅行计划是否符合用户请求，并按系统提示返回 CritiqueResult JSON。\n\n"
            f"用户请求:\n{request_json}\n\n"
            f"待审视行程:\n{plan_json}"
        )

    @staticmethod
    def _to_json(value: Any) -> str:
        if hasattr(value, "model_dump"):
            value = value.model_dump(mode="json")
        return json.dumps(value, ensure_ascii=False, indent=2)

    @staticmethod
    def _pass_result(summary: str) -> CritiqueResult:
        return CritiqueResult(
            scores=CritiqueScores(
                attraction_diversity=7,
                description_quality=7,
                weather_compatibility=7,
                schedule_feasibility=7,
                budget_realism=7,
            ),
            issues=[],
            suggestions=[],
            needs_revision=False,
            revision_summary=summary,
        )
