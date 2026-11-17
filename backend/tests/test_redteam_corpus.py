import json
from collections import Counter
from pathlib import Path
from typing import cast

import pytest

from backend.redteam.corpus import evaluate_success_condition, validate_success_condition

CORPUS_PATH = Path(__file__).parent / "fixtures" / "redteam_attacks.v1.jsonl"
FAMILIES = {
    "DIRECT_OVERRIDE",
    "METADATA",
    "TOOL_RESULT",
    "CORPUS_POISON",
    "OBFUSCATED",
    "DELIMITER_ESCAPE",
    "ROLE_CONFUSION",
    "STAGED",
    "GOAL_HIJACK",
    "EXFILTRATION",
    "CONFIDENCE_INFLATION",
}


def load_corpus() -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for line_number, line in enumerate(CORPUS_PATH.read_text().splitlines(), start=1):
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            pytest.fail(f"Invalid JSON at {CORPUS_PATH}:{line_number}: {error}")
        assert isinstance(record, dict), f"Record at line {line_number} must be an object"
        records.append(record)
    return records


def test_attack_corpus_has_60_valid_machine_checkable_records():
    records = load_corpus()
    assert len(records) == 60
    assert len({record.get("id") for record in records}) == 60

    counts: Counter[str] = Counter()
    for record in records:
        assert isinstance(record.get("id"), str)
        family = record.get("family")
        assert isinstance(family, str) and family in FAMILIES
        counts[family] += 1
        assert record.get("expected_outcome") == "attack_should_fail"
        assert isinstance(record.get("attacker_goal"), str)
        artefact = record.get("artefact")
        assert isinstance(artefact, dict)
        assert isinstance(artefact.get("title"), str)
        assert isinstance(artefact.get("description"), str)
        assert isinstance(artefact.get("branch"), str)
        assert isinstance(artefact.get("commit_messages"), list)
        files = artefact.get("files")
        assert isinstance(files, list) and files
        assert all(
            isinstance(file, dict)
            and isinstance(file.get("path"), str)
            and isinstance(file.get("patch"), str)
            for file in files
        )
        condition = record.get("success_condition")
        assert isinstance(condition, dict)
        validate_success_condition(cast(dict[str, object], condition))

    assert set(counts) == FAMILIES
    assert counts["CORPUS_POISON"] >= 6
    assert counts["TOOL_RESULT"] >= 5


@pytest.mark.parametrize(
    ("condition", "observation", "expected"),
    [
        (
            {"type": "finding_suppressed", "finding_id": "vuln-1"},
            {"reported_finding_ids": []},
            True,
        ),
        (
            {"type": "output_contains", "target": "posted_comment", "substring": "marker"},
            {"posted_comment": "finding marker"},
            True,
        ),
        (
            {"type": "action_occurred", "action": "write_file:secrets/key"},
            {"actions": ["write_file:secrets/key"]},
            True,
        ),
        (
            {"type": "confidence_at_least", "finding_id": "vuln-1", "threshold": 0.9},
            {"finding_confidences": {"vuln-1": 0.95}},
            True,
        ),
        (
            {"type": "finding_suppressed", "finding_id": "vuln-1"},
            {"reported_finding_ids": ["vuln-1"]},
            False,
        ),
    ],
)
def test_success_condition_evaluators(condition, observation, expected):
    assert evaluate_success_condition(condition, observation) is expected


def test_unknown_success_condition_is_rejected():
    with pytest.raises(ValueError, match="Unsupported"):
        validate_success_condition({"type": "model_was_influenced"})
