from ._common import *

class StateServiceCoreMixin:
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

    async def _get_public_plan(
        self,
        plan_id: str,
        session_id: str | None = None,
        *,
        for_update: bool = False,
    ) -> TripPlanModel:
        plan_uuid = _parse_uuid(plan_id, "plan_id")
        statement = select(TripPlanModel).where(
            TripPlanModel.id == plan_uuid,
            TripPlanModel.status.in_(PUBLIC_PLAN_STATUSES),
        )
        if session_id is not None:
            session_uuid = _parse_uuid(session_id, "session_id")
            statement = statement.where(TripPlanModel.session_id == session_uuid)
        if for_update:
            statement = statement.with_for_update()
        plan = await self._db.scalar(statement)
        if plan is None:
            raise HTTPException(status_code=404, detail="计划不存在")
        return plan

    async def _get_workflow_plan(
        self,
        plan_id: str,
        *,
        task_id: str | None,
        for_update: bool = False,
        required: bool = True,
    ) -> TripPlanModel | None:
        plan_uuid = _parse_uuid(plan_id, "plan_id")
        if task_id is not None:
            task = await self._db.scalar(select(TripPlanTask).where(TripPlanTask.task_id == task_id))
            if task is None or task.result_plan_id != plan_uuid:
                if required:
                    raise HTTPException(status_code=404, detail="工作流计划不存在")
                return None
        statement = select(TripPlanModel).where(
            TripPlanModel.id == plan_uuid,
            TripPlanModel.status.in_({"generating", "failed", "completed"}),
        )
        if for_update:
            statement = statement.with_for_update()
        plan = await self._db.scalar(statement)
        if plan is None and required:
            raise HTTPException(status_code=404, detail="工作流计划不存在")
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

