import re
from collections import Counter

TERM_LIMIT = 24
_WORDS = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,63}")
_STOP_WORDS = {
    "the",
    "and",
    "for",
    "with",
    "from",
    "this",
    "that",
    "not",
    "are",
    "was",
    "return",
    "import",
    "self",
    "none",
    "true",
    "false",
    "def",
    "async",
    "await",
}


def build_sparse_query(document: str) -> str:
    """Bound lexical input and OR terms; never interpret source text as search syntax."""
    words = [word.lower() for word in _WORDS.findall(document)]
    frequencies = Counter(word for word in words if word not in _STOP_WORDS)
    priority = {
        word.lower()
        for line in document.splitlines()
        if line.startswith(("+++", "---", "+", "-"))
        for word in _WORDS.findall(line)
        if "_" in word or "/" in line or re.search(r"\b(?:def|class)\s+" + re.escape(word), line)
    }
    ranked = sorted(frequencies, key=lambda word: (word not in priority, -frequencies[word], word))
    return " OR ".join(f'"{word}"' for word in ranked[:TERM_LIMIT])
