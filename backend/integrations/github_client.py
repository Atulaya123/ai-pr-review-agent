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
    ReviewOutcome.ESCALATED: "COMMENT",  # escalated reviews shouldn't reach here; see hitl gate
}


class GitHubClient:
    """GitHub App auth: sign a short-lived JWT with the App's private key, trade
    it for an installation access token, then call the REST API as that installation.
    """

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    def _app_jwt(self) -> str:
        if not (self.settings.github_app_id and self.settings.github_private_key_path):
            raise RuntimeError("GITHUB_APP_ID / GITHUB_PRIVATE_KEY_PATH not configured")
        private_key = Path(self.settings.github_private_key_path).read_text()
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


def _summary_body(findings: list[Finding], outcome: ReviewOutcome) -> str:
    if not findings:
        return "🤖 Automated review: no issues found."
    counts: dict[str, int] = {}
    for f in findings:
        counts[f.severity.value] = counts.get(f.severity.value, 0) + 1
    breakdown = ", ".join(f"{n} {sev}" for sev, n in counts.items())
    return f"🤖 Automated review ({outcome.value}): {len(findings)} finding(s) — {breakdown}."
