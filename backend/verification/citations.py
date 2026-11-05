import re
from dataclasses import dataclass
from typing import Mapping

from backend.models.findings import Finding
from backend.models.review import DiffFile

_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,(?P<count>\d+))? @@")


@dataclass(frozen=True)
class CitationVerification:
    verified: bool
    failures: tuple[str, ...]


def verify_finding_citation(
    finding: Finding,
    diff_files: list[DiffFile],
    repository_files: Mapping[str, str] | None = None,
    repository_lookup_failures: Mapping[str, str] | None = None,
) -> CitationVerification:
    """Verify a finding's cited path, line range, and optional exact evidence."""
    failures: list[str] = []
    if not is_safe_repository_path(finding.file_path):
        return CitationVerification(False, ("citation path is not a repository-relative path",))

    diff_by_path = {file.path: file.patch for file in diff_files}
    if finding.file_path in diff_by_path:
        line_map = _diff_lines_by_new_line_number(diff_by_path[finding.file_path])
        if not line_map:
            failures.append("citation file has no verifiable new-file lines in the diff")
    elif repository_files is not None and finding.file_path in repository_files:
        line_map = {
            line_number: line
            for line_number, line in enumerate(
                repository_files[finding.file_path].splitlines(), start=1
            )
        }
    else:
        line_map = {}
        lookup_failure = (repository_lookup_failures or {}).get(finding.file_path)
        if lookup_failure:
            failures.append(f"repository lookup failed: {lookup_failure}")
        else:
            failures.append(
                "citation file could not be verified in the diff or repository snapshot"
            )

    if finding.line_start < 1 or finding.line_end < finding.line_start:
        failures.append("citation line range is invalid")
    else:
        cited_lines = range(finding.line_start, finding.line_end + 1)
        if any(line_number not in line_map for line_number in cited_lines):
            failures.append("citation line range is outside the verifiable file lines")
        elif finding.evidence:
            cited_text = "\n".join(
                line_map[line_number]
                for line_number in range(finding.line_start, finding.line_end + 1)
            )
            if finding.evidence not in cited_text:
                failures.append("quoted evidence does not appear verbatim in the cited lines")

    return CitationVerification(not failures, tuple(failures))


def _diff_lines_by_new_line_number(patch: str) -> dict[int, str]:
    lines_by_number: dict[int, str] = {}
    new_line_number: int | None = None
    for line in patch.splitlines():
        hunk = _HUNK_HEADER.match(line)
        if hunk:
            new_line_number = int(hunk.group("start"))
            continue
        if new_line_number is None or line.startswith("\\"):
            continue
        if line.startswith("+"):
            lines_by_number[new_line_number] = line[1:]
            new_line_number += 1
        elif line.startswith("-"):
            continue
        elif line.startswith(" "):
            lines_by_number[new_line_number] = line[1:]
            new_line_number += 1
    return lines_by_number


def is_safe_repository_path(path: str) -> bool:
    return (
        bool(path)
        and not path.startswith("/")
        and "\\" not in path
        and all(part not in ("", ".", "..") for part in path.split("/"))
    )
