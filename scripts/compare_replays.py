import argparse
import asyncio
import json
from pathlib import Path

from backend.core.config import get_settings
from backend.evaluation.experiments import run_configuration_comparison
from backend.evaluation.replay import load_replay_fixture
from backend.models.enums import AgentType
from backend.tools.llm_client import get_llm_client
from backend.evaluation.ragas_metrics import budgeted_evaluation_embed

_FIXTURE_DIR = Path(__file__).parents[1] / "backend" / "tests" / "fixtures" / "replays"


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare current prompts with one candidate prompt using MLflow."
    )
    parser.add_argument(
        "--agent",
        choices=[agent.value for agent in AgentType if agent != AgentType.AGGREGATOR],
        required=True,
    )
    parser.add_argument("--candidate-prompt", type=Path, required=True)
    parser.add_argument("--tracking-uri", default="sqlite:///mlflow.db")
    parser.add_argument("--experiment-name", default="ai-pr-review-config-comparison")
    parser.add_argument("--fixture", action="append", type=Path)
    parser.add_argument(
        "--judge-metrics",
        action="store_true",
        help="Run budgeted judges and RAGAS; incurs separate judge spend",
    )
    return parser.parse_args()


async def _run() -> None:
    args = _arguments()
    fixture_paths = args.fixture or sorted(_FIXTURE_DIR.glob("*.json"))
    fixtures = [load_replay_fixture(path) for path in fixture_paths]
    settings = get_settings()
    comparison = await run_configuration_comparison(
        fixtures,
        candidate_agent=AgentType(args.agent),
        candidate_prompt=args.candidate_prompt.read_text(),
        settings=settings,
        judge_factory=get_llm_client if args.judge_metrics else None,
        evaluation_embed=budgeted_evaluation_embed(settings) if args.judge_metrics else None,
        tracking_uri=args.tracking_uri,
        experiment_name=args.experiment_name,
    )
    print(json.dumps(comparison.model_dump(mode="json"), indent=2))


if __name__ == "__main__":
    asyncio.run(_run())
