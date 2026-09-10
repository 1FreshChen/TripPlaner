from ._common import *

class StateServicePlansMixin:
    async def get_trip_plan(self, plan_id: str, session_id: str | None = None) -> TripPlanResponse:
        plan = await self._get_public_plan(plan_id, session_id=session_id)
        return self._plan_response(plan)

    async def update_trip_plan(
        self,
        plan_id: str,
        update: TripPlanUpdateRequest,
        session_id: str | None = None,
    ) -> TripPlanResponse:
        plan = await self._get_public_plan(plan_id, session_id=session_id, for_update=True)
        if update.expected_version != plan.version:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"版本冲突：您的修改基于版本 {update.expected_version}，"
                    f"但当前已是版本 {plan.version}。请刷新后重新编辑。"
                ),
            )

        try:
            new_start_date = date.fromisoformat(update.plan_json.start_date)
            new_end_date = date.fromisoformat(update.plan_json.end_date)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=f"行程日期格式无效: {exc}") from exc

        plan.version += 1
        plan.status = "completed"
        plan.city = update.plan_json.city
        plan.start_date = new_start_date
        plan.end_date = new_end_date
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

    async def list_plan_versions(self, plan_id: str, session_id: str | None = None) -> PlanVersionsResponse:
        plan = await self._get_public_plan(plan_id, session_id=session_id)
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

    async def revert_plan(
        self,
        plan_id: str,
        version: int,
        session_id: str | None = None,
    ) -> TripPlanResponse:
        plan = await self._get_public_plan(plan_id, session_id=session_id, for_update=True)
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

    async def archive_plan(self, plan_id: str, session_id: str | None = None) -> None:
        plan = await self._get_public_plan(plan_id, session_id=session_id, for_update=True)
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

