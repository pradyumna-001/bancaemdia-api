from celery import Celery
from celery.backends.base import DisabledBackend
from celery.result import AsyncResult


def ignored_task_result(task_id: str, *, app: Celery) -> AsyncResult:
    """Build an ignored publication receipt without initializing an external result backend.

    HTTP publishers track completion in PostgreSQL and discard this receipt. Celery still
    creates it even with ignore_result=True; its default factory resolves a backend per thread.
    The worker's configured backend and results used by chains remain unchanged.
    """
    return AsyncResult(task_id, app=app, backend=DisabledBackend(app=app))
