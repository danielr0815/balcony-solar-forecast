"""Entry-owned work and serialization of learner mutations.

The gate owns task lifetimes, not learner data. Closing it rejects new work,
cancels running and queued callers, and joins them before the store is flushed.
A bootstrap may call the public import operation in the SAME task; only that
explicit nesting is reentrant. Another task must wait or request busy rejection.
"""

import asyncio
from contextlib import asynccontextmanager
from functools import wraps

from homeassistant.exceptions import ServiceValidationError


class LearnerOperations:
    """One lifecycle/serialization boundary per config entry."""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self._owner: asyncio.Task | None = None
        self._tasks: dict[asyncio.Task, int] = {}
        self._closed = False

    @asynccontextmanager
    async def work(self, *, mutate: bool = False, reject_busy: bool = False):
        if self._closed:
            raise ServiceValidationError("This config entry is unloading; retry after reload.")
        task = asyncio.current_task()
        assert task is not None
        nested = self._owner is task
        if mutate and reject_busy and not nested and self.lock.locked():
            raise ServiceValidationError("A learner operation is already running; wait for it to finish.")
        self._tasks[task] = self._tasks.get(task, 0) + 1
        acquired = False
        try:
            if mutate and not nested:
                await self.lock.acquire()
                acquired = True
                self._owner = task
            yield
        finally:
            if acquired:
                self._owner = None
                self.lock.release()
            remaining = self._tasks[task] - 1
            if remaining:
                self._tasks[task] = remaining
            else:
                del self._tasks[task]

    async def close(self) -> None:
        """Stop writers before final persistence; safe to call again on HA stop."""
        self._closed = True
        current = asyncio.current_task()
        pending = [task for task in self._tasks if task is not current]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


def owned_operation(*, mutate: bool = False):
    """Track a coordinator operation without changing its public signature."""
    def decorate(method):
        @wraps(method)
        async def wrapped(self, *args, **kwargs):
            async with self._operations.work(mutate=mutate):
                return await method(self, *args, **kwargs)
        return wrapped
    return decorate
