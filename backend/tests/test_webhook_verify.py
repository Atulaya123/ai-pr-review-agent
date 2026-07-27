import hashlib
import hmac

import pytest

from backend.core.exceptions import InvalidWebhookSignatureError
from backend.security.webhook_verify import verify_github_signature


def _sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), msg=body, digestmod=hashlib.sha256).hexdigest()


def test_valid_signature_passes():
    body = b'{"action": "opened"}'
    secret = "test-secret"
    verify_github_signature(body, _sign(body, secret), secret)  # should not raise


def test_tampered_body_rejected():
    body = b'{"action": "opened"}'
    secret = "test-secret"
    signature = _sign(body, secret)
    with pytest.raises(InvalidWebhookSignatureError):
        verify_github_signature(b'{"action": "malicious"}', signature, secret)


def test_missing_header_rejected():
    with pytest.raises(InvalidWebhookSignatureError):
        verify_github_signature(b"{}", None, "test-secret")


def test_wrong_secret_rejected():
    body = b'{"action": "opened"}'
    signature = _sign(body, "right-secret")
    with pytest.raises(InvalidWebhookSignatureError):
        verify_github_signature(body, signature, "wrong-secret")
