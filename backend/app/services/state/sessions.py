from ._common import *

class StateServiceSessionsMixin:
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
                .where(
                    TripPlanModel.session_id == session_uuid,
                    TripPlanModel.status.in_(PUBLIC_PLAN_STATUSES),
                )
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

