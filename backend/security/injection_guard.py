"""Sanitize attacker-controlled PR content before adding it to a model prompt."""

import re
from dataclasses import dataclass

from backend.models.review import DiffFile, ReviewRequest

_INJECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "instruction_override",
        re.compile(
            r"\bignore\s+(?:all\s+)?(?:previous|prior|above)\s+instructions?\b", re.IGNORECASE
        ),
    ),
    (
        "instruction_override",
        re.compile(
            r"\bdisregard\s+(?:the\s+)?(?:rules|guidelines|policy|instructions?)\b", re.IGNORECASE
        ),
    ),
    ("role_override", re.compile(r"\byou\s+are\s+now\b", re.IGNORECASE)),
    ("role_override", re.compile(r"\bsystem\s*:\s*", re.IGNORECASE)),
    ("role_override", re.compile(r"###\s*(?:instruction|system|prompt)\b", re.IGNORECASE)),
    (
        "secret_exfiltration",
        re.compile(
            r"\b(?:reveal|expose|print|output|send|exfiltrate)\b.{0,50}\b(?:secrets?|credentials?|api\s+keys?|system\s+prompt)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "review_suppression",
        re.compile(r"\b(?:approve|accept)\s+(?:this|the)\s+(?:pr|pull\s+request)\b", re.IGNORECASE),
    ),
    (
        "review_suppression",
        re.compile(
            r"\b(?:do\s+not|don't|never)\s+(?:report|flag|mention)\b.{0,50}\b(?:vulnerabilit|security|bug|issue)",
            re.IGNORECASE,
        ),
    ),
    (
        "delimiter_spoofing",
        re.compile(r"<<<\s*UNTRUSTED_[A-Z_]+_(?:START|END)\s*>>>", re.IGNORECASE),
    ),
)

_NEUTRALIZED_LINE = "[NEUTRALIZED: instruction-shaped untrusted content]"


@dataclass(frozen=True)
class InjectionDetection:
    source: str
    pattern: str
    line_number: int
    action: str


@dataclass(frozen=True)
class SanitizationReport:
    detections: tuple[InjectionDetection, ...]
    applicable: bool
    injection_attempted: bool
    text_reached_prompt: bool
    neutralized: bool
    injection_attempt_count: int
    prompt_leak_count: int
    neutralized_count: int


@dataclass(frozen=True)
class SanitizedText:
    content: str
    detections: tuple[InjectionDetection, ...]


@dataclass(frozen=True)
class SanitizedReview:
    request: ReviewRequest
    report: SanitizationReport
    prompt_paths: tuple[str, ...]


def sanitize_untrusted_text(content: str, *, source: str) -> SanitizedText:
    """Replace instruction-shaped lines while preserving unaffected diff data.

    A whole matched line is neutralized to avoid leaving trailing instruction
    text that could still influence a model after a phrase-only replacement.
    """
    safe_lines: list[str] = []
    detections: list[InjectionDetection] = []
    for line_number, line in enumerate(content.splitlines(), start=1):
        matched_patterns = [name for name, pattern in _INJECTION_PATTERNS if pattern.search(line)]
        if matched_patterns:
            safe_lines.append(_NEUTRALIZED_LINE)
            detections.extend(
                InjectionDetection(
                    source=source,
                    pattern=pattern_name,
                    line_number=line_number,
                    action="neutralized_line",
                )
                for pattern_name in matched_patterns
            )
        else:
            safe_lines.append(line)
    if content and not safe_lines:
        safe_lines.append("")
    return SanitizedText(content="\n".join(safe_lines), detections=tuple(detections))


def sanitize_review_request(request: ReviewRequest) -> SanitizedReview:
    detections: list[InjectionDetection] = []
    safe_files: list[DiffFile] = []
    prompt_paths: list[str] = []
    for file_index, file in enumerate(request.files):
        safe_path = sanitize_untrusted_text(file.path, source=f"filename:{file_index}")
        safe_patch = sanitize_untrusted_text(file.patch, source=f"diff:{file_index}")
        detections.extend(safe_path.detections)
        detections.extend(safe_patch.detections)
        safe_files.append(DiffFile(path=file.path, patch=safe_patch.content))
        prompt_paths.append(
            safe_path.content if not safe_path.detections else f"untrusted-filename-{file_index}"
        )

    safe_commit_messages: list[str] = []
    for message_index, message in enumerate(request.commit_messages):
        safe_message = sanitize_untrusted_text(message, source=f"commit_message:{message_index}")
        detections.extend(safe_message.detections)
        safe_commit_messages.append(safe_message.content)

    sanitized_request = request.model_copy(
        update={"files": safe_files, "commit_messages": safe_commit_messages}
    )
    attempted_lines = {(item.source, item.line_number) for item in detections}
    residual_lines = {
        (source, line_number)
        for source, text in [
            *((f"filename:{index}", path) for index, path in enumerate(prompt_paths)),
            *((f"diff:{index}", file.patch) for index, file in enumerate(sanitized_request.files)),
            *(
                (f"commit_message:{index}", message)
                for index, message in enumerate(sanitized_request.commit_messages)
            ),
        ]
        for line_number, line in enumerate(text.splitlines(), start=1)
        if any(pattern.search(line) for _, pattern in _INJECTION_PATTERNS)
    }
    applicable = any(file.path or file.patch for file in request.files) or any(
        request.commit_messages
    )
    injection_attempt_count = len(attempted_lines)
    prompt_leak_count = len(residual_lines)
    injection_attempted = injection_attempt_count > 0
    text_reached_prompt = prompt_leak_count > 0
    return SanitizedReview(
        request=sanitized_request,
        report=SanitizationReport(
            detections=tuple(detections),
            applicable=applicable,
            injection_attempted=injection_attempted,
            text_reached_prompt=text_reached_prompt,
            neutralized=injection_attempted and not text_reached_prompt,
            injection_attempt_count=injection_attempt_count,
            prompt_leak_count=prompt_leak_count,
            neutralized_count=max(0, injection_attempt_count - prompt_leak_count),
        ),
        prompt_paths=tuple(prompt_paths),
    )


def fence_untrusted_content(label: str, content: str) -> str:
    """Wrap untrusted content in explicit delimiters after neutralization."""
    return f"<<<UNTRUSTED_{label.upper()}_START>>>\n{content}\n<<<UNTRUSTED_{label.upper()}_END>>>"


def detect_injection_attempt(content: str) -> list[str]:
    """Return matched pattern names, not matched attacker-controlled text."""
    return [name for name, pattern in _INJECTION_PATTERNS if pattern.search(content)]
