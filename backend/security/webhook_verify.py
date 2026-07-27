import hashlib
import hmac

from backend.core.exceptions import InvalidWebhookSignatureError


def verify_github_signature(payload_body: bytes, signature_header: str | None, secret: str) -> None:
    """Verify the X-Hub-Signature-256 header GitHub sends on every webhook delivery.

    Raises InvalidWebhookSignatureError rather than returning bool so callers can't
    accidentally ignore a False return value.
    """
    if not signature_header or not signature_header.startswith("sha256="):
        raise InvalidWebhookSignatureError("missing or malformed X-Hub-Signature-256 header")

    expected = hmac.new(secret.encode(), msg=payload_body, digestmod=hashlib.sha256).hexdigest()
    provided = signature_header.removeprefix("sha256=")

    if not hmac.compare_digest(expected, provided):
        raise InvalidWebhookSignatureError("signature mismatch")
