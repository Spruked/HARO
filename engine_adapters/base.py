"""Shared helpers for adapters: guarded execution and result construction."""
from __future__ import annotations

import os
import time
from typing import Callable

from .contract import (
    STATUS_CANCELLED, STATUS_ERROR, STATUS_OK, STATUS_REFUSED, STATUS_TIMEOUT,
    AdapterError, CancelToken, CancelledByHost, EngineUnavailable, HealthReport,
    InferenceRequest, InferenceResult, UnsupportedOperation,
)
from . import transport

Body = Callable[[InferenceResult, float], None]


def guarded_execute(adapter_id: str, capabilities: tuple[str, ...], request: InferenceRequest,
                    cancel: CancelToken, body: Body) -> InferenceResult:
    """Run ``body`` and translate every failure into an explicit result status.

    ``body(result, started_monotonic)`` fills ``result.output``, ``timings`` and
    ``engine_metrics``. Nothing here retries, substitutes another engine, or
    reports partial output as success.
    """
    result = InferenceResult(request.request_id, adapter_id, request.capability, request.model, STATUS_ERROR)
    started = time.monotonic()
    try:
        request.validate()
        if request.capability not in capabilities:
            raise UnsupportedOperation(f"{adapter_id} does not serve capability {request.capability!r}")
        body(result, started)
        result.status = STATUS_OK
    except CancelledByHost as exc:
        result.status, result.cancel_reason = STATUS_CANCELLED, str(exc)
    except TimeoutError as exc:
        result.status, result.error = STATUS_TIMEOUT, str(exc)
    except (UnsupportedOperation, ValueError) as exc:
        result.status, result.error = STATUS_REFUSED, f"{type(exc).__name__}: {exc}"
    except AdapterError as exc:
        result.status, result.error = STATUS_ERROR, f"{type(exc).__name__}: {exc}"
    result.timings["total_s"] = round(time.monotonic() - started, 4)
    if result.status != STATUS_OK:
        # Partial output from a failed or cancelled run is kept for diagnosis only.
        result.output = {"partial": result.output} if result.output else {}
    return result


def timed_health(base_url: str, path: str, *, headers: dict | None = None, timeout_s: float = 5.0,
                 ok: Callable[[transport.HttpResponse], bool] = lambda r: True) -> HealthReport:
    started = time.monotonic()
    try:
        resp = transport.request("GET", base_url, path, headers=headers, timeout_s=timeout_s)
        healthy = ok(resp)
        detail = f"HTTP {resp.status}" + ("" if healthy else "; unexpected health payload")
        return HealthReport(healthy, round(time.monotonic() - started, 4), detail)
    except (AdapterError, TimeoutError) as exc:
        return HealthReport(False, None, f"{type(exc).__name__}: {exc}")


def bearer_headers(api_key_env: str | None) -> dict[str, str]:
    """Read a credential from the environment at call time. The value is never stored or logged."""
    if not api_key_env:
        return {}
    value = os.environ.get(api_key_env)
    if not value:
        raise EngineUnavailable(f"credential environment variable {api_key_env} is not set")
    return {"Authorization": f"Bearer {value}"}
