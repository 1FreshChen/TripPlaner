from ._common import *

class StateServicePlanningMixin:
    async def create_trip_plan(
        self,
        request: TripPlanRequest,
        progress_callback: ProgressCallback | None = None,
    ) -> TripPlanResponse:
        """Run the synchronous compatibility path with short transaction boundaries."""
        await self._report_progress(progress_callback, "preparing", 5, "正在准备行程请求")
        prepared = await self.prepare_planning_context(request)
        plan_id = prepared["plan_id"]
        try:
            await self._report_progress(
                progress_callback,
                "collecting_context",
                10,
                "正在加载偏好和会话上下文",
            )
            tool_registry = _compat_symbol("bootstrap_tools", bootstrap_tools)()
            planner = _compat_symbol("TripPlannerAgent", TripPlannerAgent)(
                tool_registry=tool_registry,
                tool_executor=_compat_symbol("ToolExecutor", ToolExecutor)(registry=tool_registry),
            )
            await self._report_progress(progress_callback, "llm_planning", 35, "正在生成每日行程")
            trip_plan = await planner.aplan_trip(
                request,
                memory_context=prepared["memory_context"],
                conversation_context=prepared["conversation_context"],
            )
            critique_events = getattr(planner, "last_critique_events", [])
            await self._report_progress(progress_callback, "validating", 90, "正在校验行程质量")
            self._ensure_completed_plan_is_publishable(request, trip_plan, critique_events)
            trace = getattr(planner, "last_trace", None)
            graph_result = {
                **prepared,
                "trip_planner": trip_plan.model_dump(mode="json"),
                "trip_planner_token_usage": self._json_value(getattr(planner, "last_token_usage", None)),
                "trip_planner_tool_calls": getattr(planner, "last_tool_calls", []),
                "plan_critique_events": critique_events,
                "agent_results": [self._json_value(item) for item in (trace.agent_results if trace else [])],
                "validation_passed": True,
            }
            await self._report_progress(progress_callback, "saving", 95, "正在保存行程和初始版本")
            result = await self.finalize_trip_plan(None, plan_id, graph_result)
            await self._report_progress(progress_callback, "completed", 100, "行程规划已完成")
            return result
        except Exception as exc:
            await self._db.rollback()
            await self.fail_planning_run(None, plan_id, exc)
            if isinstance(exc, PlanQualityError):
                raise HTTPException(status_code=503, detail=_plan_quality_error_detail(exc)) from exc
            raise

    async def prepare_planning_context(
        self,
        request: TripPlanRequest,
        task_id: str | None = None,
    ) -> dict:
        """Persist or reuse the generating plan and return JSON-only graph input."""
        session_id = request.session_id or str(uuid.uuid4())
        session_uuid = _parse_uuid(session_id, "session_id")
        task: TripPlanTask | None = None
        if task_id is not None:
            task = await self._db.scalar(
                select(TripPlanTask).where(TripPlanTask.task_id == task_id).with_for_update()
            )
            if task is None:
                raise HTTPException(status_code=404, detail="任务不存在")

        user = await self._get_or_create_user_by_session(session_id)
        plan: TripPlanModel | None = None
        if task is not None and task.result_plan_id is not None:
            plan = await self._db.scalar(
                select(TripPlanModel).where(
                    TripPlanModel.id == task.result_plan_id,
                    TripPlanModel.status.in_({"generating", "completed"}),
                )
            )
            if plan is None:
                raise RuntimeError("任务关联的内部计划不存在")
        if plan is None:
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
                request_json=request.model_dump(mode="json"),
            )
            self._db.add(plan)
            await self._db.flush()
            if task is not None:
                task.result_plan_id = plan.id

        settings = get_settings()
        long_term_memory = _compat_symbol("LongTermMemory", LongTermMemory)()
        short_term = _compat_symbol("ShortTermMemory", ShortTermMemory)(session_uuid, max_messages=settings.max_conversation_messages)
        await short_term.restore_from_db(self._db)
        memory_context = await _compat_symbol("MemoryRecall", MemoryRecall)(long_term_memory).recall(
            user.id,
            self._db,
            request.city,
            request.preferences,
        )
        prepared = {
            "task_id": task_id,
            "plan_id": str(plan.id),
            "request": request.model_dump(mode="json"),
            "memory_context": self._json_value(memory_context),
            "conversation_context": self._json_value(short_term.get_context()),
            "workflow_version": task.workflow_version if task is not None else settings.langgraph_workflow_version,
            "state_schema_version": (
                task.state_schema_version if task is not None else settings.langgraph_state_schema_version
            ),
            "agent_results": [],
        }
        await self._db.commit()
        return prepared

    async def finalize_trip_plan(
        self,
        task_id: str | None,
        plan_id: str,
        graph_result: dict,
        *,
        lease_owner: str | None = None,
    ) -> TripPlanResponse:
        """Atomically publish a validated graph result and, when present, its task."""
        task: TripPlanTask | None = None
        if task_id is not None:
            task = await self._db.scalar(
                select(TripPlanTask).where(TripPlanTask.task_id == task_id).with_for_update()
            )
            if task is None:
                raise HTTPException(status_code=404, detail="任务不存在")
            if task.status == "succeeded" and task.result_payload:
                return TripPlanResponse.model_validate(task.result_payload)
            if task.status in {"failed", "cancelled", "expired"}:
                raise RuntimeError(f"终态任务不能再次完成: {task.status}")
            if lease_owner is not None and task.lease_owner != lease_owner:
                raise RuntimeError("任务租约已失效")

        plan = await self._get_workflow_plan(plan_id, task_id=task_id, for_update=True)
        if plan.status == "completed" and plan.plan_json:
            response = self._plan_response(plan)
            if task is not None and task.status != "succeeded":
                self._complete_task(task, response)
                await self._db.commit()
            return response
        if not graph_result.get("validation_passed") or graph_result.get("terminal_error"):
            raise RuntimeError("未经成功校验的图结果不能落库")

        request = TripPlanRequest.model_validate(graph_result.get("request") or plan.request_json)
        trip_plan = TripPlan.model_validate(graph_result["trip_planner"])
        plan.plan_json = trip_plan.model_dump(mode="json")
        plan.status = "completed"
        plan.version = 1
        plan.overall_suggestions = trip_plan.overall_suggestions
        plan.budget_summary = trip_plan.budget.model_dump(mode="json") if trip_plan.budget else None

        existing_version = await self._db.scalar(
            select(TripPlanVersion.id).where(
                TripPlanVersion.trip_plan_id == plan.id,
                TripPlanVersion.version == 1,
            )
        )
        if existing_version is None:
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

        usage_payload = graph_result.get("trip_planner_token_usage")
        if usage_payload:
            usage = LLMTokenUsage(**usage_payload)
            self._record_token_usage(usage, conversation_id=None, trip_plan_id=plan.id)

        tool_calls = graph_result.get("trip_planner_tool_calls") or []
        if tool_calls:
            self._add_audit_event(
                event_type="trip_planner_tool_calls",
                action="llm_tool_planning",
                session_id=plan.session_id,
                resource_type="trip_plan",
                resource_id=plan.id,
                details_json={"count": len(tool_calls), "tool_calls": tool_calls},
            )

        fallback_results = [
            {
                "agent": result.get("agent_name"),
                "fallback": result.get("fallback_used"),
                "error": result.get("error_message"),
            }
            for result in graph_result.get("agent_results", [])
            if result.get("fallback_used")
        ]
        if fallback_results:
            self._add_audit_event(
                event_type="trip_planner_fallback_used",
                action="external_service_fallback",
                severity="warning",
                session_id=plan.session_id,
                resource_type="trip_plan",
                resource_id=plan.id,
                details_json={"fallbacks": fallback_results},
            )
        for event in graph_result.get("plan_critique_events") or []:
            self._add_audit_event(
                event_type=event.get("event_type", "plan_critique"),
                action="plan_critique",
                severity=event.get("severity", "info"),
                session_id=plan.session_id,
                resource_type="trip_plan",
                resource_id=plan.id,
                details_json=event.get("details", {}),
            )

        await _compat_symbol("LongTermMemory", LongTermMemory)().update_from_trip(
            user_id=plan.user_id,
            db=self._db,
            city=request.city,
            preferences=_split_preferences(request.preferences),
            budget_level=request.budget,
            days=request.days,
            source_id=plan.id,
        )
        self._add_audit_event(
            event_type="trip_plan_created",
            action="create_trip_plan",
            session_id=plan.session_id,
            resource_type="trip_plan",
            resource_id=plan.id,
            details_json={"city": request.city, "days": request.days},
        )
        response = self._plan_response(plan, trip_plan)
        if task is not None:
            self._complete_task(task, response)
        await self._db.commit()
        return response

    async def fail_planning_run(
        self,
        task_id: str | None,
        plan_id: str | None,
        error: Exception | dict | str,
        *,
        lease_owner: str | None = None,
    ) -> None:
        """Atomically terminate the task and its internal draft when planning fails."""
        now = datetime.now(timezone.utc)
        task: TripPlanTask | None = None
        if task_id is not None:
            task = await self._db.scalar(
                select(TripPlanTask).where(TripPlanTask.task_id == task_id).with_for_update()
            )
            if task is not None and task.status == "succeeded":
                return
            if lease_owner is not None and task is not None and task.lease_owner != lease_owner:
                return
            if plan_id is None and task is not None and task.result_plan_id is not None:
                plan_id = str(task.result_plan_id)

        plan: TripPlanModel | None = None
        if plan_id is not None:
            plan = await self._get_workflow_plan(plan_id, task_id=task_id, for_update=True, required=False)
            if plan is not None and plan.status != "completed":
                plan.status = "failed"

        error_code, error_message = self._planning_error(error)
        if task is not None and task.status not in {"succeeded", "cancelled", "expired"}:
            task.status = "failed"
            task.phase = "failed"
            task.message = "行程生成失败"
            task.error_code = error_code[:64]
            task.error_message = error_message[:2000]
            task.finished_at = now
            task.updated_at = now
            task.lease_owner = None
            task.recovery_state = "none"
        await self._db.commit()

    @staticmethod
    def _complete_task(task: TripPlanTask, response: TripPlanResponse) -> None:
        now = datetime.now(timezone.utc)
        task.status = "succeeded"
        task.phase = "completed"
        task.progress = 100
        task.message = "行程规划已完成"
        task.result_plan_id = uuid.UUID(response.plan_id)
        task.result_payload = response.model_dump(mode="json")
        task.finished_at = now
        task.updated_at = now
        task.error_code = None
        task.error_message = None
        task.lease_owner = None
        task.recovery_state = "none"

    @staticmethod
    def _planning_error(error: Exception | dict | str) -> tuple[str, str]:
        detail = getattr(error, "detail", None) if isinstance(error, Exception) else error
        error_code = error.__class__.__name__.upper() if isinstance(error, Exception) else "PLANNING_FAILED"
        error_message = str(error)
        if isinstance(detail, dict):
            error_code = str(detail.get("code") or error_code)
            issues = detail.get("issues")
            error_message = str(
                detail.get("message") or ("；".join(issues) if isinstance(issues, list) else detail)
            )
        elif detail:
            error_message = str(detail)
        return error_code, error_message

    @classmethod
    def _json_value(cls, value):
        if value is None:
            return None
        if is_dataclass(value):
            value = asdict(value)
        if hasattr(value, "model_dump"):
            value = value.model_dump(mode="json")
        if isinstance(value, dict):
            return {str(key): cls._json_value(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [cls._json_value(item) for item in value]
        if hasattr(value, "value"):
            return cls._json_value(value.value)
        if isinstance(value, (str, int, float, bool)):
            return value
        return str(value)

    @staticmethod
    async def _report_progress(
        callback: ProgressCallback | None,
        phase: str,
        progress: int,
        message: str,
    ) -> None:
        if callback is not None:
            await callback(phase, progress, message)

    def _ensure_completed_plan_is_publishable(
        self,
        request: TripPlanRequest,
        trip_plan: TripPlan,
        critique_events: list[dict] | None = None,
    ) -> None:
        try:
            validate_planner_output(trip_plan, request, critique_events=critique_events)
        except PlanQualityError as exc:
            raise HTTPException(
                status_code=422,
                detail=_plan_quality_error_detail(exc, retryable=False),
            ) from exc
