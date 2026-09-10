from ._common import *

class StateServiceConversationMixin:
    async def send_conversation_message(self, session_id: str, request: ConversationRequest) -> ConversationResponse:
        session_uuid = _parse_uuid(session_id, "session_id")
        referenced_plan_uuid: uuid.UUID | None = None
        referenced_plan: TripPlanModel | None = None
        referenced_plan_context = ""

        if request.referenced_plan_id:
            referenced_plan_uuid = _parse_uuid(request.referenced_plan_id, "referenced_plan_id")
            try:
                referenced_plan = await self._get_public_plan(
                    str(referenced_plan_uuid),
                    session_id=session_id,
                )
            except HTTPException as exc:
                raise HTTPException(
                    status_code=404,
                    detail="关联的行程计划不存在或不属于当前会话",
                ) from exc
            if referenced_plan.plan_json:
                referenced_plan_context = json.dumps(referenced_plan.plan_json, ensure_ascii=False)

        if request.apply_to_plan and referenced_plan is None:
            raise HTTPException(status_code=422, detail="对话调整必须关联一个可用的行程计划")

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

        llm = _compat_symbol("LLMService", LLMService)(settings.llm_api_key, settings.llm_base_url, settings.llm_model)
        tool_registry = _compat_symbol("bootstrap_tools", bootstrap_tools)()
        tools = tool_registry.get_openai_functions()
        assistant_id = uuid.uuid4()
        executor = _compat_symbol("ToolExecutor", ToolExecutor)(registry=tool_registry)
        intent_decision = ModificationIntentDecision(
            should_modify=False,
            instruction=request.message,
            source="no_plan",
            reason="当前对话未关联行程",
        )
        if referenced_plan is not None:
            previous_messages = [message for message in history if message.id != user_message.id]
            intent_decision = await self._resolve_modification_intent(
                llm=llm,
                tool_executor=executor,
                user_message=request.message,
                recent_messages=previous_messages,
                allow_llm_fallback=request.apply_to_plan,
                conversation_id=assistant_id,
                trip_plan_id=referenced_plan.id,
            )
            logger.info(
                "Conversation plan intent classified: plan_id=%s decision=%s source=%s reason=%s",
                referenced_plan.id,
                intent_decision.should_modify,
                intent_decision.source,
                intent_decision.reason,
            )
            self._add_audit_event(
                event_type="trip_plan_intent_classified",
                action="classify_conversation_intent",
                session_id=session_uuid,
                resource_type="trip_plan",
                resource_id=referenced_plan.id,
                details_json={
                    "decision": intent_decision.should_modify,
                    "source": intent_decision.source,
                    "reason": intent_decision.reason[:200],
                    "message": request.message[:200],
                },
            )

        system_prompt = (
            "You are a travel planning assistant. Answer in the user's language. "
            "Use available tools when fresh local data is useful. "
            "When an itinerary is referenced, provide concrete adjustment suggestions. "
            "Do not claim that an itinerary was saved, updated, or finalized; the application "
            "will add a confirmation only after the updated plan is persisted successfully."
        )
        if intent_decision.should_modify:
            system_prompt += (
                " The requested change is being applied now, so do not ask the user to confirm it again."
            )
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

        updated_plan = None
        plan_update_failed = False
        if referenced_plan is not None and intent_decision.should_modify:
            try:
                begin_nested = getattr(self._db, "begin_nested", None)
                if begin_nested is None:
                    updated_plan = await self._modify_plan_via_conversation(
                        llm=llm,
                        tool_executor=executor,
                        referenced_plan=referenced_plan,
                        user_message=request.message,
                        modification_instruction=intent_decision.instruction,
                        session_uuid=session_uuid,
                        conversation_id=assistant_id,
                    )
                else:
                    async with begin_nested():
                        updated_plan = await self._modify_plan_via_conversation(
                            llm=llm,
                            tool_executor=executor,
                            referenced_plan=referenced_plan,
                            user_message=request.message,
                            modification_instruction=intent_decision.instruction,
                            session_uuid=session_uuid,
                            conversation_id=assistant_id,
                        )
                if updated_plan:
                    content = f"行程已更新（版本 {updated_plan.version}）\n\n{content}"
            except Exception as exc:
                plan_update_failed = True
                logger.warning("Conversation plan modification failed: %s", exc, exc_info=True)
                self._add_audit_event(
                    event_type="trip_plan_conversation_update_failed",
                    action="conversation_modify_plan",
                    severity="warning",
                    session_id=session_uuid,
                    resource_type="trip_plan",
                    resource_id=referenced_plan.id,
                    details_json={"error": str(exc), "message": request.message[:200]},
                )
                content = (
                    f"{content}\n\n"
                    "⚠️ 我理解你想调整行程，但这次没有成功保存修改，当前行程未变化。"
                    "请换一种说法再试一次，或到行程页手动编辑。"
                )

        assistant_metadata = {}
        if request.referenced_plan_id:
            assistant_metadata["referenced_plan_id"] = request.referenced_plan_id
            assistant_metadata["plan_intent_decision"] = intent_decision.should_modify
            assistant_metadata["plan_intent_source"] = intent_decision.source
        if updated_plan:
            assistant_metadata["plan_updated"] = True
            assistant_metadata["updated_plan_id"] = updated_plan.plan_id
            assistant_metadata["updated_plan_version"] = updated_plan.version
        if plan_update_failed:
            assistant_metadata["plan_update_failed"] = True

        assistant = ConversationMessage(
            id=assistant_id,
            session_id=session_uuid,
            role="assistant",
            content=content,
            tool_calls_json=tool_calls,
            metadata_json=assistant_metadata,
        )
        self._db.add(assistant)
        await self._db.flush()
        return ConversationResponse(
            message_id=str(assistant.id),
            role="assistant",
            content=content,
            tool_calls=tool_calls,
            updated_plan=updated_plan,
            plan_update_failed=plan_update_failed,
        )

    @staticmethod
    def _classify_modification_intent_by_rules(message: str) -> bool | None:
        normalized = (message or "").strip().lower()
        if not normalized:
            return False

        negative_patterns = [
            r"(不需要|不用|无需|不必|不要|别|先别|暂时不).{0,12}(修改|调整|更改|改|优化|更新).{0,12}(行程|计划)?",
            r"(行程|计划).{0,8}(不需要|不用|无需|不必|不要|别|先别|暂时不).{0,8}(修改|调整|更改|改|优化|更新)",
            r"\b(do not|don't|dont|no need to|not trying to)\b.{0,20}\b(change|modify|update|adjust|edit)\b",
        ]
        if any(re.search(pattern, normalized, flags=re.IGNORECASE) for pattern in negative_patterns):
            return False

        day_pattern = r"(第[一二三四五六七八九十0-9]+天|day\s*\d+)"
        itinerary_pattern = r"(行程|计划|景点|餐厅|饭店|酒店|住宿|交通|路线)"
        action_pattern = r"(替换|更换|换掉|换成|改掉|改成|修改|调整|优化|重排|删除|删掉|去掉|移除|取消|增加|新增|添加|加入|安排|提前|延后|挪到|轻松|宽松|少一点|少些|别太赶|不要太赶)"
        explicit_modification_patterns = [
            r"(把|将|请把|帮我把).+(换成|替换成|改成|调整为|变成)",
            rf"{action_pattern}.*({itinerary_pattern}|{day_pattern})",
            rf"({itinerary_pattern}|{day_pattern}).*{action_pattern}",
            rf"{day_pattern}.+(太赶|太满|太紧|太累|累|赶|轻松点|宽松点|少一点|less busy|more relaxed)",
            r"\b(replace|change|modify|update|remove|delete|add|swap|reschedule|adjust|optimize|make)\b.+\b(itinerary|plan|day|attraction|hotel|restaurant|meal|route)\b",
            r"\bday\s*\d+\b.+\b(less busy|more relaxed|lighter|easier|not so packed)\b",
        ]
        if any(re.search(pattern, normalized, flags=re.IGNORECASE) for pattern in explicit_modification_patterns):
            return True

        inquiry_patterns = [
            r"(介绍|查一下|查询|告诉我|想知道|了解一下)",
            r"(怎么样|如何|好玩吗|值得吗|推荐吗|可以吗|合适吗|多少钱|几点开放|开放时间|天气如何|有多远)[？?]?$",
            r"\b(what|when|where|why|how|is it|are there|tell me about)\b",
        ]
        if any(re.search(pattern, normalized, flags=re.IGNORECASE) for pattern in inquiry_patterns):
            return False

        preference_modification_patterns = [
            rf"{day_pattern}.+(想|希望|更想|比较想|不想|不太想|不愿意).{{0,20}}(去|住|吃|安排|体验)",
            r"(我|我们)?(更|比较|还)?(想|希望).{0,12}(去|住|吃|安排|体验)",
            r"(不想|不太想|不愿意|不考虑|不打算).{0,12}(去|住|吃|安排|体验)",
            r"(别|不要).{0,12}(去|住|吃|安排)",
        ]
        if any(re.search(pattern, normalized, flags=re.IGNORECASE) for pattern in preference_modification_patterns):
            return True
        return None

    @staticmethod
    def _detect_modification_intent(message: str) -> bool:
        """Backward-compatible fast-path detector for explicit modification requests."""

        return StateServiceConversationMixin._classify_modification_intent_by_rules(message) is True

    async def _resolve_modification_intent(
        self,
        *,
        llm: _compat_symbol("LLMService", LLMService),
        tool_executor: _compat_symbol("ToolExecutor", ToolExecutor),
        user_message: str,
        recent_messages: list[ConversationMessage],
        allow_llm_fallback: bool,
        conversation_id: uuid.UUID,
        trip_plan_id: uuid.UUID,
    ) -> ModificationIntentDecision:
        rule_decision = self._classify_modification_intent_by_rules(user_message)
        if rule_decision is not None:
            return ModificationIntentDecision(
                should_modify=rule_decision,
                instruction=user_message,
                source="rule",
                reason="命中明确修改规则" if rule_decision else "命中明确咨询或不修改规则",
            )

        if not allow_llm_fallback:
            return ModificationIntentDecision(
                should_modify=False,
                instruction=user_message,
                source="rule_uncertain",
                reason="规则无法确定，且请求未启用智能修改判断",
            )
        if not getattr(llm, "enabled", True):
            return ModificationIntentDecision(
                should_modify=False,
                instruction=user_message,
                source="llm_disabled",
                reason="规则无法确定，且 LLM 未配置",
            )

        system_prompt = (
            "你是旅行行程修改意图分类器。判断用户当前消息是否要求改变已有关联行程。"
            "增删替换景点、酒店、餐饮、交通，调整日期顺序或节奏，以及用自然语言表达新的偏好，"
            "都属于修改。结合最近对话识别‘可以’‘没问题’‘就按你说的办’等对上一轮修改方案的确认。"
            "如果上一轮已经明确显示‘行程已更新’或‘已保存’，单纯确认不应再次修改；"
            "只有上一轮仍是建议、候选方案或等待用户确认时，确认语才属于修改。"
            "纯咨询、信息查询、比较方案但未要求采用，以及明确拒绝修改，都不属于修改。"
            "只输出 JSON：{\"should_modify\": true或false, \"instruction\": \"自包含的修改指令\", "
            "\"reason\": \"简短原因\"}。should_modify 为 true 时 instruction 必须脱离上下文也能执行；"
            "为 false 时 instruction 可为空字符串。"
        )
        context_lines = [
            f"{message.role}: {(message.content or '')[:1000]}"
            for message in recent_messages[-6:]
            if message.content
        ]
        conversation_context = "\n".join(context_lines) or "(无)"
        user_prompt = (
            "最近对话：\n"
            f"{conversation_context}\n\n"
            "当前用户消息：\n"
            f"{user_message}"
        )
        try:
            text, _, usage = await llm.chat_with_tools(
                system_prompt,
                user_prompt,
                [],
                tool_executor,
                max_tool_rounds=1,
            )
            self._record_token_usage(usage, conversation_id, trip_plan_id)
            return self._load_modification_intent_decision(text)
        except Exception as exc:
            logger.warning("Conversation plan intent classification failed: %s", exc, exc_info=True)
            return ModificationIntentDecision(
                should_modify=False,
                instruction=user_message,
                source="llm_error",
                reason=f"智能意图识别失败：{str(exc)[:120]}",
            )

    @staticmethod
    def _load_modification_intent_decision(text: str) -> ModificationIntentDecision:
        payload = json.loads(strip_json_fence(text))
        if not isinstance(payload, dict) or not isinstance(payload.get("should_modify"), bool):
            raise ValueError("Intent classifier must return a boolean should_modify field")

        should_modify = payload["should_modify"]
        instruction = str(payload.get("instruction") or "").strip()
        reason = str(payload.get("reason") or "").strip()
        if should_modify and not instruction:
            raise ValueError("Intent classifier must return an executable instruction for modification requests")
        return ModificationIntentDecision(
            should_modify=should_modify,
            instruction=instruction,
            source="llm",
            reason=reason,
        )

    async def _modify_plan_via_conversation(
        self,
        *,
        llm: _compat_symbol("LLMService", LLMService),
        tool_executor: _compat_symbol("ToolExecutor", ToolExecutor),
        referenced_plan: TripPlanModel,
        user_message: str,
        modification_instruction: str | None = None,
        session_uuid: uuid.UUID,
        conversation_id: uuid.UUID,
    ) -> TripPlanResponse | None:
        if not referenced_plan.plan_json:
            raise PlanModificationError("Referenced trip plan has no editable plan data")
        if not getattr(llm, "enabled", True):
            raise PlanModificationError("LLM is not configured; conversation plan update cannot run")

        current_plan = TripPlan.model_validate(referenced_plan.plan_json)
        system_prompt = (
            "你是一个旅行行程 JSON 编辑器。你只根据用户本轮要求修改给定 TripPlan，"
            "未被要求修改的城市、日期、天气、酒店、餐饮、预算和景点信息必须保持不变。"
            "必须只输出一个合法 JSON 对象，不要输出 Markdown、解释或代码块。"
            "输出必须匹配 TripPlan schema，且不要包含 plan_id、status、version。"
            "本次调用已经确认用户要求修改行程，必须把修改落实到 TripPlan 对应字段。"
            "完成后自检用户要求是否体现在 days、景点、酒店、餐饮、交通或其他相关字段中。"
            "不得只修改说明文字，也不得输出 {\"no_change\": true}。"
        )
        effective_instruction = (modification_instruction or user_message).strip()
        base_prompt = self._build_plan_modification_prompt(current_plan, user_message, effective_instruction)
        last_error = ""

        for attempt in range(2):
            prompt = base_prompt
            if last_error:
                prompt = (
                    f"{base_prompt}\n\n上一次输出无法解析或校验失败：{last_error}\n"
                    "请重新输出完整、合法的 TripPlan JSON。"
                )
            text, _, usage = await llm.chat_with_tools(
                system_prompt,
                prompt,
                [],
                tool_executor,
                max_tool_rounds=1,
            )
            self._record_token_usage(usage, conversation_id, referenced_plan.id)
            try:
                modified_plan = self._load_conversation_modified_plan(text)
            except Exception as exc:
                last_error = str(exc)
                continue

            if modified_plan is None:
                last_error = "修改意图已确认，不能返回 no_change"
                continue
            if modified_plan.model_dump(mode="json") == current_plan.model_dump(mode="json"):
                last_error = "修改后的 TripPlan 与原计划完全相同，用户要求尚未落实"
                continue

            try:
                new_start_date = date.fromisoformat(modified_plan.start_date)
                new_end_date = date.fromisoformat(modified_plan.end_date)
            except (TypeError, ValueError) as exc:
                last_error = f"日期解析失败: {exc}"
                continue

            referenced_plan.version += 1
            referenced_plan.status = "completed"
            referenced_plan.city = modified_plan.city
            referenced_plan.start_date = new_start_date
            referenced_plan.end_date = new_end_date
            referenced_plan.days_count = len(modified_plan.days)
            referenced_plan.plan_json = modified_plan.model_dump()
            referenced_plan.overall_suggestions = modified_plan.overall_suggestions
            referenced_plan.budget_summary = modified_plan.budget.model_dump() if modified_plan.budget else None
            self._db.add(
                TripPlanVersion(
                    id=uuid.uuid4(),
                    trip_plan_id=referenced_plan.id,
                    version=referenced_plan.version,
                    plan_json=referenced_plan.plan_json,
                    change_summary=effective_instruction[:200],
                    change_type="agent_regenerate",
                )
            )
            self._add_audit_event(
                event_type="trip_plan_conversation_updated",
                action="conversation_modify_plan",
                session_id=session_uuid,
                resource_type="trip_plan",
                resource_id=referenced_plan.id,
                details_json={
                    "version": referenced_plan.version,
                    "change_summary": effective_instruction[:200],
                    "original_message": user_message[:200],
                },
            )
            await self._db.flush()
            return self._plan_response(referenced_plan, modified_plan)

        raise PlanModificationError(last_error or "LLM did not return a valid TripPlan JSON")

    @staticmethod
    def _build_plan_modification_prompt(
        current_plan: TripPlan,
        user_message: str,
        modification_instruction: str | None = None,
    ) -> str:
        plan_json = json.dumps(current_plan.model_dump(mode="json"), ensure_ascii=False, indent=2)
        effective_instruction = (modification_instruction or user_message).strip()
        return (
            "当前 TripPlan JSON：\n"
            f"{plan_json}\n\n"
            "用户原始消息：\n"
            f"{user_message}\n\n"
            "已确认、可直接执行的修改指令：\n"
            f"{effective_instruction}\n\n"
            "请输出修改后的完整 TripPlan JSON。"
        )

    @staticmethod
    def _load_conversation_modified_plan(text: str) -> TripPlan | None:
        payload = json.loads(strip_json_fence(text))
        if isinstance(payload, dict) and payload.get("no_change") is True:
            return None
        return TripPlan.model_validate(payload)

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
                    plan_updated=bool((message.metadata_json or {}).get("plan_updated")),
                    plan_update_failed=bool((message.metadata_json or {}).get("plan_update_failed")),
                )
                for message in visible
            ],
            has_more=len(messages) > limit,
        )

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
