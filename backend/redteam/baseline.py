"""All-or-nothing execution for local red-team baseline runs."""

import asyncio
import hashlib
import json
import os
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias

JsonRecord: TypeAlias = dict[str, object]
CaseExecutor: TypeAlias = Callable[[str, JsonRecord], Awaitable[JsonRecord]]


@dataclass(frozen=True)
class CorpusCounts:
    attempted: int
    completed: int
    skipped: int


class BaselineRunAborted(RuntimeError):
    def __init__(
        self,
        *,
        corpus_counts: Mapping[str, CorpusCounts],
        case_id: str,
        cause: BaseException,
    ) -> None:
        counts = ", ".join(
            f"{name}: attempted={item.attempted}, completed={item.completed}, "
            f"skipped={item.skipped}"
            for name, item in corpus_counts.items()
        )
        super().__init__(
            f"Baseline aborted at {case_id}; no result file written. "
            f"Cause: {type(cause).__name__}: {cause}. {counts}"
        )
        self.corpus_counts = dict(corpus_counts)
        self.case_id = case_id
        self.__cause__ = cause


async def run_complete_baseline(
    corpora: Mapping[str, Sequence[JsonRecord]],
    execute_case: CaseExecutor,
    output_path: Path,
    *,
    provider: str,
    model: str,
    execution_config: Mapping[str, object],
) -> dict[str, object]:
    """Run every corpus case and atomically write only a complete result set."""
    counts = {name: CorpusCounts(0, 0, len(records)) for name, records in corpora.items()}
    results: list[JsonRecord] = []

    for corpus_name, records in corpora.items():
        attempted = 0
        completed = 0
        for record in records:
            case_id = str(record.get("id", "<missing-id>"))
            attempted += 1
            counts[corpus_name] = CorpusCounts(attempted, completed, len(records) - attempted)
            try:
                result = await execute_case(corpus_name, record)
            except Exception as exc:
                raise BaselineRunAborted(
                    corpus_counts=counts,
                    case_id=case_id,
                    cause=exc,
                ) from exc
            if result.get("id") != case_id:
                raise BaselineRunAborted(
                    corpus_counts=counts,
                    case_id=case_id,
                    cause=ValueError("executor result id does not match the input record"),
                )
            results.append(result)
            completed += 1
            counts[corpus_name] = CorpusCounts(attempted, completed, len(records) - attempted)

    if len(results) != sum(len(records) for records in corpora.values()):
        raise RuntimeError("baseline completion invariant failed")

    document: dict[str, object] = {
        "schema_version": 1,
        "provider": provider,
        "model": model,
        "execution_config": dict(execution_config),
        "execution_config_fingerprint": hashlib.sha256(
            json.dumps(execution_config, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "complete": True,
        "corpora": {
            name: {
                "expected": len(corpora[name]),
                "attempted": counts[name].attempted,
                "completed": counts[name].completed,
                "skipped": counts[name].skipped,
                "review_failed": sum(
                    result.get("corpus") == name and result.get("outcome") == "REVIEW_FAILED"
                    for result in results
                ),
            }
            for name in corpora
        },
        "results": results,
    }
    _atomic_write_json(output_path, document)
    return document


def assert_comparable_baselines(
    baseline: Mapping[str, object], candidate: Mapping[str, object]
) -> None:
    """Reject before/after comparisons whose model execution settings differ."""
    required = ("provider", "model", "execution_config_fingerprint")
    for label, document in (("baseline", baseline), ("candidate", candidate)):
        if any(key not in document for key in required):
            raise ValueError(f"{label} is missing provider/model/execution_config metadata")
    baseline_config = {key: baseline[key] for key in required}
    candidate_config = {key: candidate[key] for key in required}
    if baseline_config != candidate_config:
        raise ValueError(
            "Baseline comparison refused: provider, model, or execution configuration differs."
        )


def merge_baseline_records(path: Path, updates: Sequence[JsonRecord]) -> dict[str, object]:
    """Atomically update an exact subset of records in a complete baseline artifact."""
    document = json.loads(path.read_text())
    if not isinstance(document, dict) or document.get("complete") is not True:
        raise ValueError("Cannot update a missing or incomplete baseline artifact")
    results = document.get("results")
    if not isinstance(results, list) or not all(isinstance(item, dict) for item in results):
        raise ValueError("Baseline artifact has invalid results")

    update_ids = [str(item.get("id", "")) for item in updates]
    if not update_ids or len(update_ids) != len(set(update_ids)):
        raise ValueError("Baseline updates must contain unique record IDs")
    existing = {str(item.get("id", "")): item for item in results}
    if any(record_id not in existing for record_id in update_ids):
        raise ValueError("Baseline update contains an unknown record ID")
    for update in updates:
        record_id = str(update.get("id", ""))
        if update.get("corpus") != existing[record_id].get("corpus"):
            raise ValueError(f"Corpus changed for baseline record {record_id}")
        existing[record_id].update(update)

    corpus_counts = document.get("corpora")
    if not isinstance(corpus_counts, dict):
        raise ValueError("Baseline artifact has invalid corpus counts")
    for corpus_name, counts in corpus_counts.items():
        if not isinstance(counts, dict):
            raise ValueError(f"Baseline artifact has invalid counts for {corpus_name}")
        counts["review_failed"] = sum(
            item.get("corpus") == corpus_name and item.get("outcome") == "REVIEW_FAILED"
            for item in results
        )

    poison_records = [
        item
        for item in results
        if item.get("family") == "CORPUS_POISON"
        and isinstance(item.get("evidence"), dict)
        and isinstance(item["evidence"].get("corpus_poison_recheck"), dict)
    ]
    if poison_records:
        completed = sum(
            item["evidence"]["corpus_poison_recheck"]["post_ingestion_review"].get(
                "retrieved_context_contains_poison"
            )
            is True
            for item in poison_records
        )
        checkable = sum(
            item["evidence"]["success_condition"].get("checkable") is True
            for item in poison_records
        )
        document["rechecks"] = {
            "corpus_poison": {
                "expected": 7,
                "attempted": len(poison_records),
                "completed": completed,
                "skipped": 7 - len(poison_records),
                "checkable": checkable,
            }
        }

    _atomic_write_json(path, document)
    return document


def _atomic_write_json(path: Path, document: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary_path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n")
        temporary_path.replace(path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
