from functools import lru_cache
from pathlib import Path

_TEMPLATES_DIR = Path(__file__).parent / "templates"


@lru_cache
def get_system_prompt(agent_type: str) -> str:
    path = _TEMPLATES_DIR / f"{agent_type}.txt"
    return path.read_text()
