from enum import StrEnum


class AgentType(StrEnum):
    SECURITY = "security"
    QUALITY = "quality"
    TESTS = "tests"
    DOCS = "docs"
    AGGREGATOR = "aggregator"


class Severity(StrEnum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INFO = "INFO"


class ReviewOutcome(StrEnum):
    APPROVED = "approved"
    REQUEST_CHANGES = "request_changes"
    CRITICAL_BLOCK = "critical_block"
    ESCALATED = "escalated"


class EventType(StrEnum):
    SPAN_START = "span.start"
    SPAN_END = "span.end"
    LLM_CALL = "llm.call"
    TOOL_CALL = "tool.call"
    DECISION = "decision"
    ESCALATION = "escalation"
