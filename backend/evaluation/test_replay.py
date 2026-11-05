import json
from pathlib import Path

import pytest

from backend.evaluation.replay import (
    ReviewReplayFixture,
    finding_snapshot,
    load_replay_fixture,
    replay_prompt_hash,
    run_replay_fixture,
)

REPLAY_FIXTURE_DIR = Path(__file__).parents[1] / "tests" / "fixtures" / "replays"


def _fixtures() -> list[Path]:
    return sorted(REPLAY_FIXTURE_DIR.glob("*.json"))


@pytest.mark.parametrize("fixture_path", _fixtures(), ids=lambda path: path.stem)
async def test_replay_matches_recorded_full_review_without_provider_calls(
    fixture_path: Path,
) -> None:
    fixture = load_replay_fixture(fixture_path)
    result = await run_replay_fixture(fixture)

    actual_findings = sorted(
        (finding_snapshot(finding) for finding in result.findings),
        key=lambda finding: json.dumps(finding, sort_keys=True),
    )
    expected_findings = sorted(
        (finding.model_dump(mode="json") for finding in fixture.expected_findings),
        key=lambda finding: json.dumps(finding, sort_keys=True),
    )

    assert result.model_calls == 4
    assert actual_findings == expected_findings
    assert result.overall_confidence == fixture.expected_overall_confidence
    assert result.outcome == fixture.expected_outcome


async def test_replay_rejects_prompt_drift() -> None:
    fixture = load_replay_fixture(REPLAY_FIXTURE_DIR / "known_good.json")
    changed_fixture = fixture.model_copy(update={"prompt_hash": "different-prompt"})

    with pytest.raises(ValueError, match="prompt hash mismatch"):
        await run_replay_fixture(changed_fixture)


def test_replay_fixture_prompt_hash_matches_current_prompts() -> None:
    for path in _fixtures():
        fixture: ReviewReplayFixture = load_replay_fixture(path)
        assert replay_prompt_hash(fixture) == fixture.prompt_hash
