from unittest.mock import AsyncMock

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from backend.core.config import Settings
from backend.integrations.github_client import GitHubClient, _escalation_body
from backend.models.enums import AgentType, Severity
from backend.models.findings import Finding


def _generate_test_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return pem.decode()


def test_app_jwt_prefers_env_content_over_file_path(tmp_path):
    pem = _generate_test_pem()
    unused_path = tmp_path / "should-not-be-read.pem"
    settings = Settings(
        github_app_id="12345",
        github_private_key=pem,
        github_private_key_path=str(unused_path),
    )
    token = GitHubClient(settings)._app_jwt()
    decoded = jwt.decode(token, options={"verify_signature": False})
    assert decoded["iss"] == "12345"


def test_app_jwt_falls_back_to_file_path(tmp_path):
    pem = _generate_test_pem()
    key_path = tmp_path / "key.pem"
    key_path.write_text(pem)
    settings = Settings(
        github_app_id="12345", github_private_key=None, github_private_key_path=str(key_path)
    )
    token = GitHubClient(settings)._app_jwt()
    decoded = jwt.decode(token, options={"verify_signature": False})
    assert decoded["iss"] == "12345"


def test_app_jwt_raises_without_any_key_configured():
    settings = Settings(
        github_app_id="12345", github_private_key=None, github_private_key_path=None
    )
    with pytest.raises(RuntimeError):
        GitHubClient(settings)._app_jwt()


def test_app_jwt_raises_without_app_id():
    settings = Settings(github_app_id=None, github_private_key=_generate_test_pem())
    with pytest.raises(RuntimeError):
        GitHubClient(settings)._app_jwt()


def test_app_jwt_handles_literal_backslash_n(tmp_path):
    pem = _generate_test_pem()
    mangled = pem.replace("\n", "\\n")  # what a dashboard often stores from a single-line paste
    settings = Settings(github_app_id="12345", github_private_key=mangled)
    token = GitHubClient(settings)._app_jwt()
    decoded = jwt.decode(token, options={"verify_signature": False})
    assert decoded["iss"] == "12345"


def test_app_jwt_strips_surrounding_quotes(tmp_path):
    pem = _generate_test_pem()
    mangled = f'"{pem}"'
    settings = Settings(github_app_id="12345", github_private_key=mangled)
    token = GitHubClient(settings)._app_jwt()
    decoded = jwt.decode(token, options={"verify_signature": False})
    assert decoded["iss"] == "12345"


def test_app_jwt_handles_crlf(tmp_path):
    pem = _generate_test_pem()
    mangled = pem.replace("\n", "\r\n")
    settings = Settings(github_app_id="12345", github_private_key=mangled)
    token = GitHubClient(settings)._app_jwt()
    decoded = jwt.decode(token, options={"verify_signature": False})
    assert decoded["iss"] == "12345"


def test_escalation_body_with_no_findings_still_flags_low_confidence():
    body = _escalation_body(0.4, [])
    assert "Needs human review" in body
    assert "0.40" in body


def test_escalation_body_lists_findings_as_leads_not_a_verdict():
    finding = Finding(
        agent_type=AgentType.SECURITY,
        severity=Severity.LOW,
        category="style",
        summary="looks off",
        file_path="app.py",
        line_start=3,
        line_end=3,
        confidence=0.4,
        rationale="r",
    )
    body = _escalation_body(0.4, [finding])
    assert "app.py:3" in body
    assert "looks off" in body
    assert "not a final verdict" in body


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("existing_run", "expected_method", "expected_id", "conclusion", "expected_title"),
    [
        (None, "post", "987", "failure", "Review failed to produce a verdict"),
        (
            {"id": 654, "external_id": "aipr-review:42:deadbeef"},
            "patch",
            "654",
            "success",
            "Review completed",
        ),
    ],
)
async def test_review_check_is_created_or_updated_for_the_same_head_sha(
    monkeypatch, existing_run, expected_method, expected_id, conclusion, expected_title
):
    client = GitHubClient(Settings())
    monkeypatch.setattr(client, "_installation_token", AsyncMock(return_value="installation-token"))
    requests: list[tuple[str, str, dict[str, object] | None]] = []

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self):
            return self.payload

    class HttpClient:
        async def __aenter__(self) -> "HttpClient":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def get(self, url, *, params, headers):
            requests.append(("get", url, None))
            return Response({"check_runs": [existing_run] if existing_run else []})

        async def post(self, url, *, headers, json):
            requests.append(("post", url, json))
            return Response({"id": 987})

        async def patch(self, url, *, headers, json):
            requests.append(("patch", url, json))
            return Response({"id": 654})

    monkeypatch.setattr("backend.integrations.github_client.httpx.AsyncClient", HttpClient)

    check_id = await client.set_review_check(
        "acme/demo",
        42,
        "deadbeef",
        123,
        conclusion=conclusion,
        summary="The review could not produce a valid verdict.",
    )

    assert check_id == expected_id
    assert requests[0][0] == "get"
    method, url, body = requests[1]
    assert method == expected_method
    expected_endpoint = "check-runs" if expected_method == "post" else "check-runs/654"
    assert url == f"https://api.github.com/repos/acme/demo/{expected_endpoint}"
    assert body is not None
    assert body["name"] == "AIPR Review Verdict"
    assert body["head_sha"] == "deadbeef"
    assert body["external_id"] == "aipr-review:42:deadbeef"
    assert body["status"] == "completed"
    assert body["conclusion"] == conclusion
    assert body["output"]["title"] == expected_title
