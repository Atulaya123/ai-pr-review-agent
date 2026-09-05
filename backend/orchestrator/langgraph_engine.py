from typing import Any

from backend.core.config import get_settings
from backend.core.workflow_engine import WorkflowEngine
from backend.orchestrator.graph import build_review_graph
from backend.orchestrator.redis_checkpointer import RedisCheckpointSaver

# M2: Redis-backed checkpointer (per pr-review-agent.html 3.2) so a worker
# crash mid-review resumes from the last completed node instead of
# restarting — REDIS_URL is the same Redis the ARQ queue already uses.
# Hand-rolled rather than the official langgraph-checkpoint-redis package:
# see redis_checkpointer.py's module docstring for why (its redisvl
# dependency needs redis>=6.3, arq needs redis<6 — genuinely incompatible in
# one virtualenv, not a version pin that could be fixed).
_checkpointer = RedisCheckpointSaver.from_url(get_settings().redis_url)
_compiled_graph = build_review_graph().compile(checkpointer=_checkpointer)


class LangGraphWorkflowEngine(WorkflowEngine):
    async def run(self, workflow_id: str, input: dict[str, Any]) -> dict[str, Any]:
        config = {"configurable": {"thread_id": workflow_id}}
        return await _compiled_graph.ainvoke(input, config=config)

    async def resume(self, workflow_id: str, state: dict[str, Any]) -> dict[str, Any]:
        config = {"configurable": {"thread_id": workflow_id}}
        return await _compiled_graph.ainvoke(state, config=config)

    async def get_state(self, workflow_id: str) -> dict[str, Any] | None:
        config = {"configurable": {"thread_id": workflow_id}}
        snapshot = await _compiled_graph.aget_state(config)
        return snapshot.values if snapshot else None
