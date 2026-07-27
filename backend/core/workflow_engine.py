"""Abstract orchestration interface (ADR-001).

All orchestrator code must depend on this interface, never on LangGraph directly.
If scale later demands Temporal, a TemporalWorkflowEngine implementing this same
interface can be swapped in via get_workflow_engine() without touching any other
module — that is the entire point of the seam.
"""

from abc import ABC, abstractmethod
from typing import Any


class WorkflowEngine(ABC):
    @abstractmethod
    async def run(self, workflow_id: str, input: dict[str, Any]) -> dict[str, Any]:
        """Start (or continue) a workflow run and return its final state."""

    @abstractmethod
    async def resume(self, workflow_id: str, state: dict[str, Any]) -> dict[str, Any]:
        """Resume a workflow from a previously checkpointed state."""

    @abstractmethod
    async def get_state(self, workflow_id: str) -> dict[str, Any] | None:
        """Return the last checkpointed state for a workflow, if any."""


def get_workflow_engine() -> WorkflowEngine:
    from backend.orchestrator.langgraph_engine import LangGraphWorkflowEngine

    return LangGraphWorkflowEngine()
