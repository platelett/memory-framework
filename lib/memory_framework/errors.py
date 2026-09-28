class MemoryFrameworkError(RuntimeError):
    """Base error for expected framework failures."""


class ValidationError(MemoryFrameworkError):
    """Raised when authoritative inputs are malformed or incomplete."""


class PublicationError(MemoryFrameworkError):
    """Raised when a relabel proposal cannot be published safely."""
