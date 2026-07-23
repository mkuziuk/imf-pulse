"""Safe, file-based ingestion for The Residual.

The package treats the sibling IMF repository as untrusted, read-only input.  It
copies explicitly allowlisted bytes into a private snapshot before parsing them
and never imports or executes source-repository code.
"""

from .errors import (
    ConfigurationError,
    ExtractionError,
    PipelineError,
    PublicationError,
    SnapshotError,
    ValidationError,
)

__all__ = [
    "ConfigurationError",
    "ExtractionError",
    "PipelineError",
    "PublicationError",
    "SnapshotError",
    "ValidationError",
]
