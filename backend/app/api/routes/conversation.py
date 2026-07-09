from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query, Request

from app.api.deps import get_state_service
from app.api.middlewares.rate_limit import limiter
from app.models.schemas import ConversationListResponse, ConversationRequest, ConversationResponse
from app.services.state_service import StateService

router = APIRouter(prefix="/conversation", tags=["conversation"])


@router.post("/{session_id}", response_model=ConversationResponse)
@limiter.limit("10/minute")
async def send_message(
    request: Request,
    session_id: str,
    conversation_request: ConversationRequest,
    state: StateService = Depends(get_state_service),
) -> ConversationResponse:
    return await state.send_conversation_message(session_id, conversation_request)


@router.get("/{session_id}", response_model=ConversationListResponse)
@limiter.limit("30/minute")
async def get_conversation(
    request: Request,
    session_id: str,
    limit: int = Query(default=50, ge=1, le=100),
    before_id: Optional[str] = None,
    state: StateService = Depends(get_state_service),
) -> ConversationListResponse:
    return await state.list_conversation(session_id, limit=limit, before_id=before_id)
