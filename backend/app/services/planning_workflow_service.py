from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.schemas import TripPlanRequest, TripPlanResponse
from app.services.state_service import StateService
from app.services.task_service import reconcile_task_progress, update_task_progress


class PlanningWorkflowService:
    """Coordinate short DB transactions around one durable LangGraph run."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        graph,
    ) -> None:
        self._session_factory = session_factory
        self._graph = graph

    async def run(
        self,
        *,
        task_id: str,
        lease_owner: str,
        request_payload: dict[str, Any],
        workflow_version: str,
        state_schema_version: int,
    ) -> TripPlanResponse:
        config = {
            "configurable": {
                "thread_id": task_id,
                "checkpoint_ns": workflow_version,
            }
        }
        snapshot = await self._graph.aget_state(config)
        has_snapshot = bool(snapshot.values)
        plan_id: str | None = None

        if has_snapshot:
            values = dict(snapshot.values)
            self._assert_compatible(values, workflow_version, state_schema_version)
            plan_id = values.get("plan_id")
            await reconcile_task_progress(
                self._session_factory,
                task_id,
                values,
                lease_owner=lease_owner,
            )
            if not snapshot.next:
                return await self._finish(task_id, lease_owner, plan_id, values)
            graph_input = None
            projected_values = values
            projected_values["agent_results"] = list(values.get("agent_results") or [])
        else:
            request = TripPlanRequest.model_validate(request_payload)
            async with self._session_factory() as db:
                prepared = await StateService(db).prepare_planning_context(request, task_id=task_id)
            plan_id = prepared["plan_id"]
            await update_task_progress(
                self._session_factory,
                task_id,
                "collecting_context",
                10,
                "正在加载偏好和会话上下文",
                lease_owner=lease_owner,
            )
            graph_input = prepared
            projected_values = dict(prepared)
            projected_values["agent_results"] = list(prepared.get("agent_results") or [])

        async for event in self._graph.astream(graph_input, config, stream_mode="updates"):
            for update in event.values():
                if not isinstance(update, dict):
                    continue
                agent_results = update.get("agent_results")
                projected_values.update(
                    {key: value for key, value in update.items() if key != "agent_results"}
                )
                if agent_results:
                    projected_values.setdefault("agent_results", []).extend(agent_results)
            await reconcile_task_progress(
                self._session_factory,
                task_id,
                projected_values,
                lease_owner=lease_owner,
            )

        final_snapshot = await self._graph.aget_state(config)
        values = dict(final_snapshot.values)
        plan_id = values.get("plan_id") or plan_id
        return await self._finish(task_id, lease_owner, plan_id, values)

    async def _finish(
        self,
        task_id: str,
        lease_owner: str,
        plan_id: str | None,
        values: dict[str, Any],
    ) -> TripPlanResponse:
        if not plan_id:
            raise RuntimeError("工作流没有稳定 plan_id")
        terminal_error = values.get("terminal_error")
        if terminal_error or not values.get("validation_passed"):
            error = terminal_error or {
                "code": "GRAPH_INCOMPLETE",
                "message": "工作流结束但未通过最终校验",
            }
            async with self._session_factory() as db:
                await StateService(db).fail_planning_run(
                    task_id,
                    plan_id,
                    error,
                    lease_owner=lease_owner,
                )
            raise RuntimeError(str(error.get("message") or error))

        await update_task_progress(
            self._session_factory,
            task_id,
            "saving",
            95,
            "正在保存行程和初始版本",
            lease_owner=lease_owner,
        )
        async with self._session_factory() as db:
            return await StateService(db).finalize_trip_plan(
                task_id,
                plan_id,
                values,
                lease_owner=lease_owner,
            )

    @staticmethod
    def _assert_compatible(
        values: dict[str, Any],
        workflow_version: str,
        state_schema_version: int,
    ) -> None:
        if values.get("workflow_version") != workflow_version:
            raise RuntimeError("checkpoint workflow version 与任务不兼容")
        if values.get("state_schema_version") != state_schema_version:
            raise RuntimeError("checkpoint state schema version 与任务不兼容")
