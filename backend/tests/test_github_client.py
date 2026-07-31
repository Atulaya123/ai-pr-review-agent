import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from backend.core.config import Settings
from backend.integrations.github_client import GitHubClient


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
    settings = Settings(github_app_id="12345", github_private_key=None, github_private_key_path=str(key_path))
    token = GitHubClient(settings)._app_jwt()
    decoded = jwt.decode(token, options={"verify_signature": False})
    assert decoded["iss"] == "12345"


def test_app_jwt_raises_without_any_key_configured():
    settings = Settings(github_app_id="12345", github_private_key=None, github_private_key_path=None)
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
