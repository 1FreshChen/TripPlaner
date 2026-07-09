from __future__ import annotations

import json
import uuid
from datetime import date
from decimal import Decimal
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.trip_planner import TripPlannerAgent
from app.config import get_settings
from app.memory.long_term import LongTermMemory
from app.memory.recall import MemoryRecall
from app.memory.short_term import ShortTermMemory
from app.models.db_models import AuditEvent, ConversationMessage, TokenUsage as TokenUsageModel, TripPlan as TripPlanModel
from app.models.db_models import TripPlanVersion, User, UserPreference
from app.models.schemas import (
    ConversationListResponse,
    ConversationMessageResponse,
    ConversationRequest,
    ConversationResponse,
    PlanVersionsResponse,
    SessionCreateResponse,
    SessionDetailResponse,
    SessionTripPlanSummary,
    TripPlan,
    TripPlanRequest,
    TripPlanResponse,
    TripPlanUpdateRequest,
    UserPreferenceResponse,
    UserPreferenceUpdateRequest,
)
from app.services.content_filter import filter_llm_output
from app.services.llm_service import LLMService, TokenUsage as LLMTokenUsage, estimate_cost
from app.services.meal_enrichment import enrich_meals_with_baidu
from app.services.baidu_map_service import BaiduMapService
from app.services.plan_quality import PlanQualityError, validate_trip_plan_for_request
from app.tools.bootstrap import bootstrap_tools
from app.tools.executor import ToolExecutor


def _iso(value) -> str:
    return value.isoformat() if value else ""


def _parse_uuid(value: str, label: str = "id") -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=f"{label} 不存在") from exc


def _split_preferences(value: str) -> list[str]:
    return [item.strip() for item in value.replace("，", ",").split(",") if item.strip()]


class StateService:
    """PostgreSQL-backed state manager for sessions, plans, versions, and conversations."""

    def __init__(self, db: AsyncSession):
        self._db = db

    async def create_session(self) -> SessionCreateResponse:
        user = User(id=uuid.uuid4(), session_token=str(uuid.uuid4()))
        self._db.add(user)
        await self._db.flush()
        return SessionCreateResponse(
            session_id=user.session_token,
            user_id=str(user.id),
            created_at=_iso(user.created_at),
            is_new=True,
        )

    async def get_session(self, session_id: str) -> SessionDetailResponse:
        user = await self._get_user_by_session(session_id)
        session_uuid = _parse_uuid(session_id, "session_id")
        plans = (
            await self._db.execute(
                select(TripPlanModel)
                .where(TripPlanModel.session_id == session_uuid, TripPlanModel.status != "archived")
                .order_by(desc(TripPlanModel.created_at))
            )
        ).scalars().all()
        conversation_count = await self._db.scalar(
            select(func.count()).select_from(ConversationMessage).where(ConversationMessage.session_id == session_uuid)
        )
        return SessionDetailResponse(
            session_id=session_id,
            user_id=str(user.id),
            trip_plans=[
                SessionTripPlanSummary(
                    id=str(plan.id),
                    city=plan.city,
                    start_date=plan.start_date.isoformat(),
                    end_date=plan.end_date.isoformat(),
                    status=plan.status,
                    version=plan.version,
                    created_at=_iso(plan.created_at),
                    updated_at=_iso(plan.updated_at),
                )
                for plan in plans
            ],
            conversation_count=int(conversation_count or 0),
            created_at=_iso(user.created_at),
            updated_at=_iso(user.updated_at),
        )

    async def create_trip_plan(self, request: TripPlanRequest) -> TripPlanResponse:
        session_id = request.session_id or str(uuid.uuid4())
        user = await self._get_or_create_user_by_session(session_id)
        session_uuid = _parse_uuid(session_id, "session_id")
        plan = TripPlanModel(
            id=uuid.uuid4(),
            user_id=user.id,
            session_id=session_uuid,
            status="generating",
            city=request.city,
            start_date=date.fromisoformat(request.start_date),
            end_date=date.fromisoformat(request.end_date),
            days_count=request.days,
            preferences=request.preferences,
            budget_level=request.budget,
            transportation=request.transportation,
            accommodation=request.accommodation,
            request_json=request.model_dump(),
        )
        self._db.add(plan)
        await self._db.flush()

        settings = get_settings()
        long_term_memory = LongTermMemory()
        memory_recall = MemoryRecall(long_term_memory)
        short_term = ShortTermMemory(session_uuid, max_messages=settings.max_conversation_messages)
        await short_term.restore_from_db(self._db)
        memory_context = await memory_recall.recall(user.id, self._db, request.city, request.preferences)
        conversation_context = short_term.get_context()

        tool_registry = bootstrap_tools()
        planner = TripPlannerAgent(
            tool_registry=tool_registry,
            tool_executor=ToolExecutor(registry=tool_registry),
        )
        trip_plan = await planner.aplan_trip(
            request,
            memory_context=memory_context,
            conversation_context=conversation_context,
        )

        # Enrich meals with real Baidu Maps restaurant data (rating, price, hours)
        baidu_service = BaiduMapService(settings.baidu_map_api_key)
        trip_plan = await enrich_meals_with_baidu(trip_plan, baidu_service)

        planner_token_usage = getattr(planner, "last_token_usage", None)
        planner_tool_calls = getattr(planner, "last_tool_calls", [])
        planner_critique_events = getattr(planner, "last_critique_events", [])
        self._ensure_completed_plan_is_publishable(request, trip_plan, planner_critique_events)

        plan.plan_json = trip_plan.model_dump()
        plan.status = "completed"
        plan.version = 1
        plan.overall_suggestions = trip_plan.overall_suggestions
        plan.budget_summary = trip_plan.budget.model_dump() if trip_plan.budget else None
        if planner_token_usage:
            self._record_token_usage(planner_token_usage, conversation_id=None, trip_plan_id=plan.id)
        if planner_tool_calls:
            self._add_audit_event(
                event_type="trip_planner_tool_calls",
                action="llm_tool_planning",
                session_id=session_uuid,
                resource_type="trip_plan",
                resource_id=plan.id,
                details_json={
                    "count": len(planner_tool_calls),
                    "tool_calls": planner_tool_calls,
                },
            )
        for event in planner_critique_events:
            self._add_audit_event(
                event_type=event.get("event_type", "plan_critique"),
                action="plan_critique",
                severity=event.get("severity", "info"),
                session_id=session_uuid,
                resource_type="trip_plan",
                resource_id=plan.id,
                details_json=event.get("details", {}),
            )
        self._db.add(
            TripPlanVersion(
                id=uuid.uuid4(),
                trip_plan_id=plan.id,
                version=1,
                plan_json=plan.plan_json,
                change_summary="初始创建",
                change_type="initial_create",
            )
        )
        await long_term_memory.update_from_trip(
            user_id=user.id,
            db=self._db,
            city=request.city,
            preferences=_split_preferences(request.preferences),
            budget_level=request.budget,
            days=request.days,
        )
        self._add_audit_event(
            event_type="trip_plan_created",
            action="create_trip_plan",
            session_id=session_uuid,
            resource_type="trip_plan",
            resource_id=plan.id,
            details_json={"city": request.city, "days": request.days},
        )
        await self._db.flush()
        return self._plan_response(plan, trip_plan)

    async def get_trip_plan(self, plan_id: str) -> TripPlanResponse:
        plan = await self._get_plan(plan_id)
        return self._plan_response(plan)

    async def update_trip_plan(self, plan_id: str, update: TripPlanUpdateRequest) -> TripPlanResponse:
        plan = await self._get_plan(plan_id)
        plan.version += 1
        plan.status = "editing"
        plan.city = update.plan_json.city
        plan.start_date = date.fromisoformat(update.plan_json.start_date)
        plan.end_date = date.fromisoformat(update.plan_json.end_date)
        plan.days_count = len(update.plan_json.days)
        plan.plan_json = update.plan_json.model_dump()
        plan.overall_suggestions = update.plan_json.overall_suggestions
        plan.budget_summary = update.plan_json.budget.model_dump() if update.plan_json.budget else None
        self._db.add(
            TripPlanVersion(
                id=uuid.uuid4(),
                trip_plan_id=plan.id,
                version=plan.version,
                plan_json=plan.plan_json,
                change_summary=update.change_summary,
                change_type="manual_edit",
            )
        )
        self._add_audit_event(
            event_type="trip_plan_updated",
            action="update_trip_plan",
            session_id=plan.session_id,
            resource_type="trip_plan",
            resource_id=plan.id,
            details_json={"version": plan.version, "change_summary": update.change_summary},
        )
        await self._db.flush()
        return self._plan_response(plan, update.plan_json)

    async def list_plan_versions(self, plan_id: str) -> PlanVersionsResponse:
        plan = await self._get_plan(plan_id)
        versions = (
            await self._db.execute(
                select(TripPlanVersion)
                .where(TripPlanVersion.trip_plan_id == plan.id)
                .order_by(desc(TripPlanVersion.version))
            )
        ).scalars().all()
        return PlanVersionsResponse(
            versions=[
                {"version": version.version, "change_summary": version.change_summary, "created_at": _iso(version.created_at)}
                for version in versions
            ]
        )

    async def revert_plan(self, plan_id: str, version: int) -> TripPlanResponse:
        plan = await self._get_plan(plan_id)
        target = await self._db.scalar(
            select(TripPlanVersion).where(
                TripPlanVersion.trip_plan_id == plan.id,
                TripPlanVersion.version == version,
            )
        )
        if target is None:
            raise HTTPException(status_code=404, detail="版本不存在")
        restored = TripPlan.model_validate(target.plan_json)
        plan.version += 1
        plan.status = "completed"
        plan.plan_json = target.plan_json
        plan.city = restored.city
        plan.start_date = date.fromisoformat(restored.start_date)
        plan.end_date = date.fromisoformat(restored.end_date)
        plan.days_count = len(restored.days)
        plan.overall_suggestions = restored.overall_suggestions
        plan.budget_summary = restored.budget.model_dump() if restored.budget else None
        self._db.add(
            TripPlanVersion(
                id=uuid.uuid4(),
                trip_plan_id=plan.id,
                version=plan.version,
                plan_json=plan.plan_json,
                change_summary=f"恢复到版本 {version}",
                change_type="manual_edit",
            )
        )
        await self._db.flush()
        return self._plan_response(plan, restored)

    async def archive_plan(self, plan_id: str) -> None:
        plan = await self._get_plan(plan_id)
        plan.status = "archived"
        self._add_audit_event(
            event_type="trip_plan_archived",
            action="archive_trip_plan",
            session_id=plan.session_id,
            resource_type="trip_plan",
            resource_id=plan.id,
            details_json={"plan_id": str(plan.id)},
        )
        await self._db.flush()

    async def send_conversation_message(self, session_id: str, request: ConversationRequest) -> ConversationResponse:
        session_uuid = _parse_uuid(session_id, "session_id")
        referenced_plan_uuid: uuid.UUID | None = None
        referenced_plan_context = ""

        if request.referenced_plan_id:
            referenced_plan_uuid = _parse_uuid(request.referenced_plan_id, "referenced_plan_id")
            referenced_plan = await self._db.scalar(
                select(TripPlanModel).where(
                    TripPlanModel.id == referenced_plan_uuid,
                    TripPlanModel.session_id == session_uuid,
                    TripPlanModel.status != "archived",
                )
            )
            if referenced_plan and referenced_plan.plan_json:
                referenced_plan_context = json.dumps(referenced_plan.plan_json, ensure_ascii=False)

        user_message = ConversationMessage(
            id=uuid.uuid4(),
            session_id=session_uuid,
            role="user",
            content=request.message,
            metadata_json={"referenced_plan_id": request.referenced_plan_id} if request.referenced_plan_id else {},
        )
        self._db.add(user_message)
        await self._db.flush()

        settings = get_settings()
        history = await self._load_recent_messages(session_uuid, limit=settings.max_conversation_messages)
        history_text = "\n".join(f"{message.role}: {message.content}" for message in history if message.content)
        prompt_parts = [
            "Recent conversation:",
            history_text or "(none)",
            "Current user message:",
            request.message,
        ]
        if referenced_plan_context:
            prompt_parts.extend(["Referenced trip plan JSON:", referenced_plan_context])

        llm = LLMService(settings.llm_api_key, settings.llm_base_url, settings.llm_model)
        tool_registry = bootstrap_tools()
        tools = tool_registry.get_openai_functions()
        system_prompt = (
            "You are a travel planning assistant. Answer in the user's language. "
            "Use available tools when fresh local data is useful. "
            "When an itinerary is referenced, provide concrete adjustment suggestions."
        )
        assistant_id = uuid.uuid4()
        executor = ToolExecutor(registry=tool_registry)
        content, tool_calls, usage = await llm.chat_with_tools(
            system_prompt,
            "\n\n".join(prompt_parts),
            tools,
            executor,
        )
        content = self._filter_llm_response(content, assistant_id, referenced_plan_uuid, session_uuid)
        self._record_token_usage(usage, assistant_id, referenced_plan_uuid)

        if not content:
            content = "I recorded your request, but no LLM response is available because the LLM is not configured."

        assistant = ConversationMessage(
            id=assistant_id,
            session_id=session_uuid,
            role="assistant",
            content=content,
            tool_calls_json=tool_calls,
        )
        self._db.add(assistant)
        await self._db.flush()
        return ConversationResponse(
            message_id=str(assistant.id),
            role="assistant",
            content=content,
            tool_calls=tool_calls,
            updated_plan=None,
        )

    async def list_conversation(
        self,
        session_id: str,
        limit: int = 50,
        before_id: Optional[str] = None,
    ) -> ConversationListResponse:
        session_uuid = _parse_uuid(session_id, "session_id")
        filters = [ConversationMessage.session_id == session_uuid]
        if before_id:
            before_uuid = _parse_uuid(before_id, "before_id")
            cursor = await self._db.scalar(
                select(ConversationMessage).where(
                    ConversationMessage.id == before_uuid,
                    ConversationMessage.session_id == session_uuid,
                )
            )
            if cursor is None:
                raise HTTPException(status_code=404, detail="conversation cursor not found")
            filters.append(ConversationMessage.created_at < cursor.created_at)

        stmt = (
            select(ConversationMessage)
            .where(*filters)
            .order_by(desc(ConversationMessage.created_at))
            .limit(limit + 1)
        )
        messages = (await self._db.execute(stmt)).scalars().all()
        visible = list(reversed(messages[:limit]))
        return ConversationListResponse(
            messages=[
                ConversationMessageResponse(
                    id=str(message.id),
                    role=message.role,
                    content=message.content,
                    tool_calls=message.tool_calls_json,
                    created_at=_iso(message.created_at),
                )
                for message in visible
            ],
            has_more=len(messages) > limit,
        )

    async def get_preferences(self, user_id: str) -> UserPreferenceResponse:
        user_uuid = _parse_uuid(user_id, "user_id")
        pref = await self._db.scalar(select(UserPreference).where(UserPreference.user_id == user_uuid))
        if pref is None:
            return UserPreferenceResponse()
        return UserPreferenceResponse(
            preferred_categories=pref.preferred_categories or [],
            budget_profile=pref.budget_profile or {},
            travel_style=pref.travel_style or "",
            favorite_cities=pref.favorite_cities or [],
        )

    async def update_preferences(self, user_id: str, request: UserPreferenceUpdateRequest) -> UserPreferenceResponse:
        user_uuid = _parse_uuid(user_id, "user_id")
        pref = await self._db.scalar(select(UserPreference).where(UserPreference.user_id == user_uuid))
        if pref is None:
            pref = UserPreference(id=uuid.uuid4(), user_id=user_uuid)
            self._db.add(pref)
        pref.preferred_categories = request.preferred_categories
        pref.budget_profile = request.budget_profile
        pref.travel_style = request.travel_style
        pref.favorite_cities = request.favorite_cities
        await self._db.flush()
        return UserPreferenceResponse(**request.model_dump())

    async def _load_recent_messages(self, session_uuid: uuid.UUID, limit: int = 20) -> list[ConversationMessage]:
        messages = (
            await self._db.execute(
                select(ConversationMessage)
                .where(ConversationMessage.session_id == session_uuid)
                .order_by(desc(ConversationMessage.created_at))
                .limit(limit)
            )
        ).scalars().all()
        return list(reversed(messages))

    def _record_token_usage(
        self,
        usage: LLMTokenUsage,
        conversation_id: uuid.UUID | None,
        trip_plan_id: uuid.UUID | None = None,
    ) -> None:
        if usage.total_tokens <= 0:
            return
        estimated_cost = Decimal(str(estimate_cost(usage.model, usage.prompt_tokens, usage.completion_tokens)))
        self._db.add(
            TokenUsageModel(
                trip_plan_id=trip_plan_id,
                conversation_id=conversation_id,
                model=usage.model,
                provider=usage.provider,
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                total_tokens=usage.total_tokens,
                estimated_cost_usd=estimated_cost,
            )
        )

    def _filter_llm_response(
        self,
        content: str,
        conversation_id: uuid.UUID,
        trip_plan_id: uuid.UUID | None,
        session_id: uuid.UUID,
    ) -> str:
        filtered, issues = filter_llm_output(content)
        if issues:
            self._add_audit_event(
                event_type="content_filter_triggered",
                action="filter_llm_output",
                severity="warning",
                session_id=session_id,
                resource_type="conversation_message",
                resource_id=conversation_id,
                details_json={"issues": issues, "trip_plan_id": str(trip_plan_id) if trip_plan_id else None},
            )
        return filtered

    def _add_audit_event(
        self,
        *,
        event_type: str,
        action: str,
        severity: str = "info",
        session_id: uuid.UUID | None = None,
        resource_type: str | None = None,
        resource_id: uuid.UUID | None = None,
        details_json: dict | None = None,
    ) -> None:
        self._db.add(
            AuditEvent(
                event_type=event_type,
                action=action,
                severity=severity,
                session_id=session_id,
                resource_type=resource_type,
                resource_id=resource_id,
                details_json=details_json or {},
            )
        )

    def _ensure_completed_plan_is_publishable(
        self,
        request: TripPlanRequest,
        trip_plan: TripPlan,
        critique_events: list[dict] | None = None,
    ) -> None:
        try:
            validate_trip_plan_for_request(trip_plan, request, critique_events=critique_events)
        except PlanQualityError as exc:
            raise HTTPException(status_code=422, detail=f"计划质量校验失败，不能保存 completed：{exc}") from exc

    async def _get_user_by_session(self, session_id: str) -> User:
        user = await self._db.scalar(select(User).where(User.session_token == session_id))
        if user is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        return user

    async def _get_or_create_user_by_session(self, session_id: str) -> User:
        user = await self._db.scalar(select(User).where(User.session_token == session_id))
        if user is not None:
            return user
        user = User(id=uuid.uuid4(), session_token=session_id)
        self._db.add(user)
        await self._db.flush()
        return user

    async def _get_plan(self, plan_id: str) -> TripPlanModel:
        plan_uuid = _parse_uuid(plan_id, "plan_id")
        plan = await self._db.scalar(select(TripPlanModel).where(TripPlanModel.id == plan_uuid))
        if plan is None or plan.status == "archived":
            raise HTTPException(status_code=404, detail="计划不存在")
        return plan

    def _plan_response(self, plan: TripPlanModel, trip_plan: Optional[TripPlan] = None) -> TripPlanResponse:
        if trip_plan is None and plan.plan_json is None:
            raise HTTPException(status_code=500, detail=f"Plan {plan.id} is missing plan_json data")
        payload = trip_plan or TripPlan.model_validate(plan.plan_json)
        return TripPlanResponse(
            **payload.model_dump(),
            plan_id=str(plan.id),
            status=plan.status,
            version=plan.version,
        )
