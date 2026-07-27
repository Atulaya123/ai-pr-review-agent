"""Defense against prompt injection via untrusted PR diff/code content.

The trust boundary (DONE.html section 1): diffs originate from arbitrary,
potentially adversarial contributors. Two independent layers:

1. Structural: diff content is always fenced as an explicitly-labeled untrusted
   data block, never concatenated into the instruction portion of a prompt.
2. Heuristic: a small set of known injection phrasings is flagged so the
   security specialist can raise a finding about the diff itself, rather than
   silently following any instruction it might contain.
"""

import re

_SUSPICIOUS_PATTERNS = [
    re.compile(r"ignore (all )?(previous|prior|above) instructions", re.IGNORECASE),
    re.compile(r"you are now", re.IGNORECASE),
    re.compile(r"system\s*:\s*", re.IGNORECASE),
    re.compile(r"###\s*(instruction|system|prompt)", re.IGNORECASE),
    re.compile(r"disregard (the )?(rules|guidelines|policy)", re.IGNORECASE),
]


def fence_untrusted_content(label: str, content: str) -> str:
    """Wrap diff/code content in unambiguous delimiters for a prompt.

    The surrounding prompt template must instruct the model that everything
    between these markers is DATA to review, never an instruction to obey.
    """
    return f"<<<UNTRUSTED_{label.upper()}_START>>>\n{content}\n<<<UNTRUSTED_{label.upper()}_END>>>"


def detect_injection_attempt(content: str) -> list[str]:
    """Return the list of matched suspicious phrases, empty if none found."""
    return [p.pattern for p in _SUSPICIOUS_PATTERNS if p.search(content)]
