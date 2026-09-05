import time
from pathlib import Path

import httpx
import jwt

from backend.core.config import Settings, get_settings
from backend.models.enums import ReviewOutcome
from backend.models.findings import Finding
from backend.reliability.retry import retry_with_backoff
from backend.reliability.timeout import with_timeout

GITHUB_API = "https://api.github.com"

_EVENT_BY_OUTCOME = {
    ReviewOutcome.APPROVED: "COMMENT",
    ReviewOutcome.REQUEST_CHANGES: "COMMENT",
    ReviewOutcome.CRITICAL_BLOCK: "REQUEST_CHANGES",
    ReviewOutcome.ESCALATED: "COMMENT",  # never actually used — ESCALATED goes through
    # request_human_review() below, not post_review(); kept for completeness only.
}


class GitHubClient:
    """GitHub App auth: sign a short-lived JWT with the App's private key, trade
    it for an installation access token, then call the REST API as that installation.
    """

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    def _app_jwt(self) -> str:
        if not self.settings.github_app_id:
            raise RuntimeError("GITHUB_APP_ID not configured")
        if self.settings.github_private_key:
            # env-var content — used on hosts with an ephemeral filesystem (e.g. Render),
            # where a file path written at build time wouldn't survive a redeploy.
            # Defensive normalization: dashboards commonly mangle multi-line paste —
            # stripping real newlines down to literal "\n", adding surrounding quotes,
            # or introducing \r\n. Undo all three rather than assume a clean paste.
            private_key = self.settings.github_private_key.strip()
            if len(private_key) >= 2 and private_key[0] == private_key[-1] and private_key[0] in "\"'":
                private_key = private_key[1:-1]
            private_key = private_key.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\r\n", "\n")
        elif self.settings.github_private_key_path:
            private_key = Path(self.settings.github_private_key_path).read_text()
        else:
            raise RuntimeError("neither GITHUB_PRIVATE_KEY nor GITHUB_PRIVATE_KEY_PATH is configured")
        now = int(time.time())
        payload = {"iat": now - 60, "exp": now + 9 * 60, "iss": self.settings.github_app_id}
        return jwt.encode(payload, private_key, algorithm="RS256")

    async def _installation_token(self, installation_id: int) -> str:
        async def _call():
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{GITHUB_API}/app/installations/{installation_id}/access_tokens",
                    headers={
                        "Authorization": f"Bearer {self._app_jwt()}",
                        "Accept": "application/vnd.github+json",
                    },
                )
                resp.raise_for_status()
                return resp.json()["token"]

        return await with_timeout(retry_with_backoff(_call))

    async def fetch_pr_diff(self, repo: str, pr_number: int, installation_id: int) -> str:
        token = await self._installation_token(installation_id)

        async def _call():
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}",
                    headers={"Authorization": f"token {token}", "Accept": "application/vnd.github.v3.diff"},
                )
                resp.raise_for_status()
                return resp.text

        return await with_timeout(retry_with_backoff(_call))

    async def post_review(
        self,
        repo: str,
        pr_number: int,
        installation_id: int,
        findings: list[Finding],
        outcome: ReviewOutcome,
    ) -> str:
        token = await self._installation_token(installation_id)
        event = _EVENT_BY_OUTCOME[outcome]

        comments = [
            {
                "path": f.file_path,
                "line": f.line_end,
                "body": f"**[{f.severity.value}] {f.category}** ({f.agent_type.value}, confidence {f.confidence:.2f})\n\n{f.summary}\n\n{f.rationale}"
                + (f"\n\n_Suggestion:_ {f.suggestion}" if f.suggestion else ""),
            }
            for f in findings
        ]
        body = _summary_body(findings, outcome)

        async def _call():
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}/reviews",
                    headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json"},
                    json={"body": body, "event": event, "comments": comments},
                )
                resp.raise_for_status()
                return str(resp.json()["id"])

        return await with_timeout(retry_with_backoff(_call))

    async def request_human_review(
        self,
        repo: str,
        pr_number: int,
        installation_id: int,
        *,
        overall_confidence: float,
        findings: list[Finding],
    ) -> str:
        """The HITL gate's ESCALATED path: below-threshold confidence means
        the specialists aren't sure enough for an autonomous REQUEST_CHANGES,
        but silently dropping the review was the actual gap (HITLReview
        existed in the schema, nothing ever consumed it — see
        docs/INTERVIEW_PREP.md). The PR itself is the queue: a plain issue
        comment (not a formal review — the findings aren't confident enough
        to stand as this bot's authoritative verdict) plus a label, so a human
        can find every review awaiting them with `is:pr label:needs-human-review`,
        no separate UI needed.
        """
        token = await self._installation_token(installation_id)
        body = _escalation_body(overall_confidence, findings)

        async def _post_comment():
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{GITHUB_API}/repos/{repo}/issues/{pr_number}/comments",
                    headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json"},
                    json={"body": body},
                )
                resp.raise_for_status()
                return str(resp.json()["id"])

        comment_id = await with_timeout(retry_with_backoff(_post_comment))

        async def _add_label():
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{GITHUB_API}/repos/{repo}/issues/{pr_number}/labels",
                    headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json"},
                    json={"labels": ["needs-human-review"]},
                )
                resp.raise_for_status()

        await with_timeout(retry_with_backoff(_add_label))
        return comment_id


def _escalation_body(overall_confidence: float, findings: list[Finding]) -> str:
    if not findings:
        return (
            "🤖 **Needs human review** — the automated reviewer's confidence "
            f"({overall_confidence:.2f}) fell below the threshold for an autonomous "
            "verdict, even with no findings to report. Nothing specific stood out, "
            "but the low confidence itself is the signal worth a second look."
        )
    lines = [
        f"🤖 **Needs human review** — overall confidence {overall_confidence:.2f} is below "
        "this reviewer's threshold for an autonomous verdict. The findings below are what "
        "it noticed, but treat them as leads for a human reviewer, not a final verdict:",
        "",
    ]
    for f in findings:
        lines.append(f"- **[{f.severity.value}] {f.category}** ({f.file_path}:{f.line_start}, confidence {f.confidence:.2f}): {f.summary}")
    return "\n".join(lines)


def _summary_body(findings: list[Finding], outcome: ReviewOutcome) -> str:
    if not findings:
        return "🤖 Automated review: no issues found."
    counts: dict[str, int] = {}
    for f in findings:
        counts[f.severity.value] = counts.get(f.severity.value, 0) + 1
    breakdown = ", ".join(f"{n} {sev}" for sev, n in counts.items())
    return f"🤖 Automated review ({outcome.value}): {len(findings)} finding(s) — {breakdown}."
