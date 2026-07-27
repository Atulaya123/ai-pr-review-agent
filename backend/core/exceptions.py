class AIPRReviewError(Exception):
    """Base class for all application errors."""


class InvalidWebhookSignatureError(AIPRReviewError):
    """The GitHub webhook HMAC signature did not match."""


class DuplicateDeliveryError(AIPRReviewError):
    """This webhook delivery id was already processed (idempotency)."""


class BudgetExceededError(AIPRReviewError):
    """The daily LLM spend cap has been reached; refuse further calls."""


class CircuitOpenError(AIPRReviewError):
    """The circuit breaker for this dependency is open; fail fast."""


class WorkflowExecutionError(AIPRReviewError):
    """The orchestrator failed to complete a review workflow."""
