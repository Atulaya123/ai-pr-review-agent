from typing import Any

from langgraph.checkpoint.memory import MemorySaver

from backend.core.workflow_engine import WorkflowEngine
from backend.orchestrator.graph import build_review_graph

# M1 uses an in-process MemorySaver so the demo has zero extra infra dependencies.
# M2 upgrades this to a Redis-backed checkpointer (per pr-review-agent.html 3.2)
# so a worker crash mid-review resumes from the last completed node instead of
# restarting — swap the checkpointer here, nothing else in the codebase changes.
_checkpointer = MemorySaver()
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
