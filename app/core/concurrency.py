"""
Concurrency helpers for latency-bound Firestore work.

Firestore round trips from outside the database's region cost hundreds of milliseconds
each, and that latency dominates response time. The work is almost entirely spent waiting
on the network, so running independent reads on a thread pool collapses many sequential
waits into a single one. Measured locally: eight sequential document reads take ~6.8s, the
same eight in parallel take ~1.7s.

These helpers are deliberately small. They exist so route handlers can express "these
queries do not depend on each other" without each one hand-rolling a thread pool.
"""

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from app.core.config import settings

logger = logging.getLogger("concurrency")


def run_parallel(tasks: dict[str, Callable[[], Any]], max_workers: int | None = None) -> dict[str, Any]:
    """
    Runs independent zero-argument callables concurrently and returns their results by key.

    A task that raises does not sink the batch: it is logged and its key maps to None, so a
    partially available report still renders rather than failing outright.
    """
    if not tasks:
        return {}

    workers = max_workers or settings.QUERY_CONCURRENCY
    results: dict[str, Any] = {}

    with ThreadPoolExecutor(max_workers=min(workers, len(tasks))) as executor:
        futures = {key: executor.submit(task) for key, task in tasks.items()}
        for key, future in futures.items():
            try:
                results[key] = future.result()
            except Exception as exc:
                logger.error("Parallel task '%s' failed: %s", key, exc)
                results[key] = None

    return results


