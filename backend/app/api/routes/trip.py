from fastapi import APIRouter, Depends, Request, status

from app.api.deps import get_state_service
from app.api.middlewares.rate_limit import limiter
from app.models.schemas import PlanVersionsResponse, TripPlanRequest, TripPlanResponse, TripPlanUpdateRequest
from app.services.state_service import StateService


router = APIRouter(prefix="/trip", tags=["trip"])


@router.post("/plan", response_model=TripPlanResponse, status_code=status.HTTP_201_CREATED)
@limiter.limit("5/minute")
@limiter.limit("50/hour")
async def create_trip_plan(
    request: Request,
    trip_request: TripPlanRequest,
    state: StateService = Depends(get_state_service),
) -> TripPlanResponse:
    return await state.create_trip_plan(trip_request)


@router.get("/plan/{plan_id}", response_model=TripPlanResponse)
@limiter.limit("60/minute")
async def get_trip_plan(
    request: Request,
    plan_id: str,
    state: StateService = Depends(get_state_service),
) -> TripPlanResponse:
    return await state.get_trip_plan(plan_id)


@router.put("/plan/{plan_id}", response_model=TripPlanResponse)
@limiter.limit("10/minute")
async def update_trip_plan(
    request: Request,
    plan_id: str,
    trip_update: TripPlanUpdateRequest,
    state: StateService = Depends(get_state_service),
) -> TripPlanResponse:
    return await state.update_trip_plan(plan_id, trip_update)


@router.get("/plan/{plan_id}/versions", response_model=PlanVersionsResponse)
@limiter.limit("60/minute")
async def get_plan_versions(
    request: Request,
    plan_id: str,
    state: StateService = Depends(get_state_service),
) -> PlanVersionsResponse:
    return await state.list_plan_versions(plan_id)


@router.post("/plan/{plan_id}/revert/{version}", response_model=TripPlanResponse)
@limiter.limit("10/minute")
async def revert_plan_version(
    request: Request,
    plan_id: str,
    version: int,
    state: StateService = Depends(get_state_service),
) -> TripPlanResponse:
    return await state.revert_plan(plan_id, version)


@router.delete("/plan/{plan_id}", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("10/minute")
async def archive_trip_plan(
    request: Request,
    plan_id: str,
    state: StateService = Depends(get_state_service),
) -> None:
    await state.archive_plan(plan_id)
