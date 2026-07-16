"""Public error types shared by focused governance modules."""


class IngestError(RuntimeError):
    """Raised when a source cannot be safely prepared."""
