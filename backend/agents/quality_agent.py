from backend.agents.base_agent import SpecialistAgent
from backend.models.enums import AgentType
from backend.tools.llm_client import LLMClient


def build_quality_agent(llm_client: LLMClient, model: str | None = None) -> SpecialistAgent:
    return SpecialistAgent(AgentType.QUALITY, llm_client, model)
