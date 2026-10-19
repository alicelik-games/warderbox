"""Retry policies with exponential backoff for failed tool executions.

Usage:
    policy = RetryPolicy(max_retries=3, backoff_base=1.0, backoff_max=30.0)
    shield = WarderBox(budget=5.00)

    @shield.tool(cost=0.03, retry=policy)
    def flaky_api(query: str) -> str:
        ...
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class RetryPolicy:
    """Configures retry behavior for a tool.

    Args:
        max_retries: Maximum number of retry attempts.
        backoff_base: Base delay in seconds (doubles each retry).
        backoff_max: Maximum delay cap in seconds.
        jitter: Add random jitter (0 to jitter seconds) to each delay.
        retry_on: Optional callable that takes the exception and returns
                  True if this error should be retried. Default: retry all.
    """
    max_retries: int = 3
    backoff_base: float = 1.0
    backoff_max: float = 30.0
    jitter: float = 0.5
    retry_on: Callable[[Exception], bool] | None = None

    def should_retry(self, error: Exception, attempt: int) -> bool:
        if attempt >= self.max_retries:
            return False
        if self.retry_on is not None:
            return self.retry_on(error)
        return True

    def delay_for(self, attempt: int) -> float:
        delay = min(self.backoff_base * (2 ** attempt), self.backoff_max)
        if self.jitter > 0:
            delay += random.uniform(0, self.jitter)
        return delay


def execute_with_retry(
    func: Callable,
    args: tuple,
    kwargs: dict,
    policy: RetryPolicy,
) -> tuple[any, list[Exception]]:
    """Execute a function with retry logic.

    Returns (result, errors_encountered).
    Raises the last exception if all retries are exhausted.
    """
    errors: list[Exception] = []

    for attempt in range(policy.max_retries + 1):
        try:
            result = func(*args, **kwargs)
            return result, errors
        except Exception as e:
            errors.append(e)
            if not policy.should_retry(e, attempt + 1):
                raise
            delay = policy.delay_for(attempt)
            time.sleep(delay)

    raise errors[-1]


async def async_execute_with_retry(
    func: Callable,
    args: tuple,
    kwargs: dict,
    policy: RetryPolicy,
) -> tuple[any, list[Exception]]:
    """Async version of execute_with_retry."""
    import asyncio
    import inspect

    errors: list[Exception] = []

    for attempt in range(policy.max_retries + 1):
        try:
            if inspect.iscoroutinefunction(func):
                result = await func(*args, **kwargs)
            else:
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(None, lambda: func(*args, **kwargs))
            return result, errors
        except Exception as e:
            errors.append(e)
            if not policy.should_retry(e, attempt + 1):
                raise
            delay = policy.delay_for(attempt)
            await asyncio.sleep(delay)

    raise errors[-1]
