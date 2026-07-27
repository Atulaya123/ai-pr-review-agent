import re

from backend.models.review import DiffFile

_FILE_HEADER = re.compile(r"^diff --git a/(?P<a>.+?) b/(?P<b>.+)$")
_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,(?P<count>\d+))? @@")


def get_valid_line_range(patch: str) -> tuple[int, int] | None:
    """The (min, max) line numbers in the new-file version that a hunk header
    says are actually part of this diff. GitHub's review-comment API rejects
    a `line` outside this range with a 422 — and small/local LLMs occasionally
    hallucinate a line just past the end of a short file, so callers should
    clamp findings into this range before posting rather than trust the model."""
    lo: int | None = None
    hi: int | None = None
    for line in patch.splitlines():
        match = _HUNK_HEADER.match(line)
        if not match:
            continue
        start = int(match.group("start"))
        count = int(match.group("count")) if match.group("count") else 1
        end = start + max(count - 1, 0)
        lo = start if lo is None else min(lo, start)
        hi = end if hi is None else max(hi, end)
    if lo is None or hi is None:
        return None
    return lo, hi


def parse_unified_diff(diff_text: str) -> list[DiffFile]:
    """Split a full unified diff (as returned by the GitHub .diff media type)
    into one DiffFile per changed file."""
    files: list[DiffFile] = []
    current_path: str | None = None
    current_lines: list[str] = []

    for line in diff_text.splitlines():
        match = _FILE_HEADER.match(line)
        if match:
            if current_path is not None:
                files.append(DiffFile(path=current_path, patch="\n".join(current_lines)))
            current_path = match.group("b")
            current_lines = [line]
        elif current_path is not None:
            current_lines.append(line)

    if current_path is not None:
        files.append(DiffFile(path=current_path, patch="\n".join(current_lines)))

    return files
