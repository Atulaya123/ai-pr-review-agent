from pydantic import BaseModel


class PullRequestPayload(BaseModel):
    action: str
    number: int
    repo_full_name: str
    installation_id: int | None
    head_sha: str

    @classmethod
    def from_github_event(cls, body: dict) -> "PullRequestPayload":
        return cls(
            action=body["action"],
            number=body["pull_request"]["number"],
            repo_full_name=body["repository"]["full_name"],
            installation_id=(body.get("installation") or {}).get("id"),
            head_sha=body["pull_request"]["head"]["sha"],
        )
