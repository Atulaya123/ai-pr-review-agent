# Red-team attack corpus

The versioned, checked-in corpus is `backend/tests/fixtures/redteam_attacks.v1.jsonl`. It
contains 60 realistic PR-shaped artefacts; each record includes an expected outcome and a
machine-checkable success condition evaluated by `backend.redteam.corpus`.

## Attack families

- **DIRECT_OVERRIDE:** Instructions in changed code ask the reviewer to ignore its rules or hide a finding. This applies because PR diffs are attacker-controlled input to the review agents.
- **METADATA:** Instructions appear in PR titles, descriptions, commit messages, branch names, or filenames rather than added lines. These fields accompany a review request and can reach the same model context as the diff.
- **TOOL_RESULT:** Instructions are returned by MCP retrieval or findings tools. Tool results are a separate untrusted surface and can carry poisoned repository context into a review.
- **CORPUS_POISON:** A PR adds instructions to architecture documentation that the knowledge-base refresh may ingest. This is especially important here because repository-derived retrieval can make poisoned content persist into future reviews.
- **OBFUSCATED:** Instructions use encoding, homoglyphs, invisible characters, bidirectional controls, or non-English text. These forms can evade superficial inspection while still being understandable to a model.
- **DELIMITER_ESCAPE:** Content imitates or closes markers used to distinguish untrusted data from trusted instructions. This applies because the review prompt uses explicit delimiters around untrusted content.
- **ROLE_CONFUSION:** Content imitates system, assistant, user, or tool messages. These transcript-like strings are relevant because the reviewer consumes multiple context sources and tool responses.
- **STAGED:** Setup in one hunk establishes a false premise and a later hunk contains the vulnerability. This targets reviews that inspect changes in fragments rather than considering the whole PR.
- **GOAL_HIJACK:** Persuasive rationale argues a real issue is harmless or exempt without directly ordering the model to ignore instructions. This applies because reviewers assess findings with contextual reasoning and may be swayed by claims about generated or internal-only code.
- **EXFILTRATION:** Changed code or retrieved context asks the reviewer to disclose prompt, invariant, or configuration text in a public comment. The system can produce posted review comments, so internal context disclosure is an observable risk.
- **CONFIDENCE_INFLATION:** Content attempts to alter finding confidence or suppress findings below the human-review gate. This applies because confidence scoring and the gate determine which findings are surfaced.

The corpus is test data, not evidence that any attack has been run against the review service. Later measurements must distinguish input blocking, isolation, model decisions, output blocking, and successful attacker outcomes.
