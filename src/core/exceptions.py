"""Custom exception hierarchy for BI TestPilot AI.

A single base exception (:class:`BITestPilotError`) lets the UI layer catch and
present all domain/service failures uniformly, while specific subclasses allow
targeted handling where it matters (e.g. distinguishing a datasource
connection failure from a parsing failure).
"""

from __future__ import annotations


class BITestPilotError(Exception):
    """Base class for all application-specific errors."""


# --- Storage / project layer ----------------------------------------------
class StorageError(BITestPilotError):
    """Raised for filesystem / persistence failures."""


class ProjectNotFoundError(StorageError):
    """Raised when a requested project does not exist on disk."""


class ProjectAlreadyExistsError(StorageError):
    """Raised when creating a project whose name/id already exists."""


# --- Ingestion / parsing ---------------------------------------------------
class UploadError(BITestPilotError):
    """Raised when an uploaded asset is invalid or cannot be stored."""


class MetadataExtractionError(BITestPilotError):
    """Raised when a dashboard file cannot be parsed into metadata."""


class UnsupportedPlatformError(MetadataExtractionError):
    """Raised when no extractor is registered for a BI platform/file type."""


# --- Datasource ------------------------------------------------------------
class DatasourceError(BITestPilotError):
    """Base for datasource configuration/connection failures."""


class DatasourceConnectionError(DatasourceError):
    """Raised when a datasource cannot be reached or authenticated."""


class DatasourceConfigError(DatasourceError):
    """Raised when a datasource configuration is invalid/incomplete."""


# --- Analysis pipeline -----------------------------------------------------
class ValidationError(BITestPilotError):
    """Raised for failures inside the deterministic validation engine."""


class ComparisonError(BITestPilotError):
    """Raised for failures inside the deterministic comparison engine."""


# --- LLM layer -------------------------------------------------------------
class LLMError(BITestPilotError):
    """Base for LLM abstraction-layer failures."""


class LLMConfigError(LLMError):
    """Raised when an LLM provider is misconfigured (e.g. missing API key)."""


class LLMProviderError(LLMError):
    """Raised when an LLM provider call fails (network / API error)."""


class LLMResponseError(LLMError):
    """Raised when an LLM returns an unusable/unparseable response."""


class TokenBudgetExhausted(LLMError):
    """Raised when the key's daily token budget cannot cover the next call.

    Distinct from :class:`LLMProviderError` because the two need opposite
    handling: a provider error is worth retrying, while an exhausted daily
    budget will not clear until the reset, so every retry burns time and — on
    providers that charge rejected requests — the next day's allowance too.

    Callers treat it like a soft stop: finish with what has been produced, then
    report where the run got to.
    """


class OperationCancelled(BITestPilotError):
    """Raised when the user cancels a run.

    Deliberately *not* a subclass of any domain error: a cancellation is a
    user decision, not a failure, so ``except Exception`` blocks that degrade
    a stage or skip a batch must re-raise it instead of swallowing it.
    """
