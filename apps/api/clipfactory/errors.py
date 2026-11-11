"""Typed errors. Every failure that reaches a job stage or an HTTP response is one of these."""

from __future__ import annotations

from typing import Any


class ClipFactoryError(Exception):
    code = "internal_error"
    http_status = 500
    retryable = False

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "retryable": self.retryable, "details": self.details}


class ValidationFailed(ClipFactoryError):
    code = "validation_failed"
    http_status = 422


class NotFound(ClipFactoryError):
    code = "not_found"
    http_status = 404


class Conflict(ClipFactoryError):
    code = "conflict"
    http_status = 409


class DependencyMissing(ClipFactoryError):
    code = "dependency_missing"
    http_status = 503


class MediaError(ClipFactoryError):
    code = "media_error"
    http_status = 422


class SubprocessFailed(MediaError):
    code = "subprocess_failed"
    retryable = True


class IngestError(ClipFactoryError):
    code = "ingest_error"
    http_status = 422


class TranscriptionError(ClipFactoryError):
    code = "transcription_error"
    retryable = True


class RenderError(ClipFactoryError):
    code = "render_error"
    retryable = True


class ExportInvalid(ClipFactoryError):
    """A rendered file failed validation; it must never be reported as completed."""

    code = "export_invalid"


class JobCancelled(ClipFactoryError):
    code = "cancelled"


class WorkerShutdown(ClipFactoryError):
    """The worker is stopping; the job goes back to the queue, not to 'failed'."""

    code = "worker_shutdown"


class StageTimeout(ClipFactoryError):
    code = "stage_timeout"
    retryable = True


# ── AI gateway ──────────────────────────────────────────────────────────────


class AIError(ClipFactoryError):
    code = "ai_error"


class AINotConfigured(AIError):
    code = "ai_not_configured"


class AIAuthError(AIError):
    code = "ai_auth"


class AIInsufficientCredits(AIError):
    code = "ai_insufficient_credits"


class AIRateLimited(AIError):
    code = "ai_rate_limited"
    retryable = True


class AIProviderError(AIError):
    code = "ai_provider_error"
    retryable = True


class AITimeout(AIError):
    code = "ai_timeout"
    retryable = True


class AIBadRequest(AIError):
    code = "ai_bad_request"


class AIInvalidResponse(AIError):
    code = "ai_invalid_response"
    retryable = True


class AIModelUnavailable(AIError):
    code = "ai_model_unavailable"


class AIBudgetExceeded(AIError):
    code = "ai_budget_exceeded"
