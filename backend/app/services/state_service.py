from __future__ import annotations

from app.services.state._common import *
from app.services.state.conversation import StateServiceConversationMixin
from app.services.state.core import StateServiceCoreMixin
from app.services.state.planning import StateServicePlanningMixin
from app.services.state.plans import StateServicePlansMixin
from app.services.state.preferences import StateServicePreferencesMixin
from app.services.state.sessions import StateServiceSessionsMixin


class StateService(
    StateServiceSessionsMixin,
    StateServicePlanningMixin,
    StateServicePlansMixin,
    StateServiceConversationMixin,
    StateServicePreferencesMixin,
    StateServiceCoreMixin,
):
    """Facade preserving the original StateService API while keeping domain code modular."""

    def __init__(self, db: AsyncSession):
        self._db = db
