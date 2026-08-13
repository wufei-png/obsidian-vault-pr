"""Structured public errors shared by governance modules."""


class DomainError(RuntimeError):
    """Expected domain failure with a stable CLI protocol code."""

    default_code = "execution_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code or self.default_code


class IngestError(DomainError):
    """Raised when a source cannot be safely prepared."""

    default_code = "ingest_error"


class PlanError(DomainError):
    """Raised when a semantic change plan is invalid or unsafe."""

    default_code = "invalid_plan"
