"""Bounded retry for READ operations only. Irreversible actions go through ops.py."""
from __future__ import annotations

import time

from .errors import ErrorKind, IntegrationError


def with_retries(fn, ctx, max_attempts: int | None = None, attempts_out: list | None = None):
    max_attempts = max_attempts or ctx.versions.policy["retries"]["read_max_attempts"]
    backoff = ctx.settings.retry_backoff_override
    if backoff is None:
        backoff = ctx.versions.policy["retries"]["backoff_seconds"]
    for attempt in range(1, max_attempts + 1):
        if attempts_out is not None:
            attempts_out.append(attempt)
        try:
            return fn()
        except IntegrationError as e:
            if e.kind != ErrorKind.TRANSIENT or attempt == max_attempts:
                raise
            delay = backoff[min(attempt - 1, len(backoff) - 1)] if backoff else 0
            if delay:
                ctx.sleep(delay)
    raise AssertionError("unreachable")


def default_sleep(seconds: float) -> None:
    time.sleep(seconds)
