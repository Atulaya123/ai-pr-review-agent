class AIPRReviewError(Exception):
    """Base class for all application errors."""


class InvalidWebhookSignatureError(AIPRReviewError):
    """The GitHub webhook HMAC signature did not match."""


class DuplicateDeliveryError(AIPRReviewError):
    """This webhook delivery id was already processed (idempotency)."""


class BudgetExceededError(AIPRReviewError):
    """The daily LLM spend cap has been reached; refuse further calls."""


class PricingUnavailableError(AIPRReviewError):
    """No effective provider/model price is available to reserve a call."""


class TokenBudgetExceededError(AIPRReviewError):
    """A whole file plus prompt overhead cannot fit the provider token limit."""


class CircuitOpenError(AIPRReviewError):
    """The circuit breaker for this dependency is open; fail fast."""


class WorkflowExecutionError(AIPRReviewError):
    """The orchestrator failed to complete a review workflow."""


class StageExecutionError(WorkflowExecutionError):
    def __init__(self, stage: str, cause: Exception) -> None:
        self.stage = stage
        self.cause_type = type(cause).__name__
        super().__init__(f"Review stage {stage} failed ({self.cause_type})")
