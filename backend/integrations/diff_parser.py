import re

from backend.models.review import DiffFile

_FILE_HEADER = re.compile(r"^diff --git a/(?P<a>.+?) b/(?P<b>.+)$")


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
