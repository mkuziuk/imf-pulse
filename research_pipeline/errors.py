"""Typed errors used at pipeline trust boundaries."""


class PipelineError(RuntimeError):
    """Base class for an expected pipeline failure."""


class ConfigurationError(PipelineError):
    """Configuration is absent, malformed, or unsafe."""


class SnapshotError(PipelineError):
    """A source cannot be copied or a snapshot cannot be verified."""


class ExtractionError(PipelineError):
    """Static extraction failed."""


class ValidationError(PipelineError):
    """Generated data failed schema or cross-reference validation."""


class PublicationError(PipelineError):
    """A release gate or atomic publication operation failed."""
