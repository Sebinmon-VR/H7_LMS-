"""
Background job registry with progress reporting.

Some work is too slow to sit inside a request even after optimisation - rebuilding the
monitoring report, bulk provisioning, large imports. Rather than making a user watch a
spinner for the full duration, the request starts a job and returns immediately with a job
id; the client polls for progress and completion.

Jobs run on a shared thread pool and their state is held in memory. That is deliberate for
this deployment's size, and it carries two honest limitations:

  * Job state does not survive a process restart.
  * With multiple server workers, a poll may land on a worker that never saw the job, so a
    client can get 404 for a job that is genuinely running elsewhere.

Moving to a shared store (Firestore, Redis) or a real task queue (Celery, Cloud Tasks) is
the fix when either becomes a problem. Until then, jobs are safe to lose: every one is a
recomputation of derived data, never a source of truth.
"""

import logging
import threading
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from enum import Enum
from typing import Any, Callable

logger = logging.getLogger("jobs")

# Bounded so a burst of refresh requests cannot exhaust the process.
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="lms-job")

# Completed jobs are retained so a client that polls slightly late still sees the outcome.
_MAX_RETAINED_JOBS = 100


class JobStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class Job:
    """A unit of background work and its observable progress."""

    def __init__(self, job_id: str, name: str, requested_by: int | None = None):
        self.id = job_id
        self.name = name
        self.requested_by = requested_by
        self.status = JobStatus.QUEUED
        self.percent = 0
        self.message = "Queued"
        self.result: Any = None
        self.error: str | None = None
        self.created_at = datetime.utcnow()
        self.started_at: datetime | None = None
        self.finished_at: datetime | None = None
        self._lock = threading.Lock()

    def set_progress(self, percent: int, message: str) -> None:
        with self._lock:
            self.percent = max(0, min(100, percent))
            self.message = message

    @property
    def duration_seconds(self) -> float | None:
        if self.started_at is None:
            return None
        end = self.finished_at or datetime.utcnow()
        return round((end - self.started_at).total_seconds(), 2)

    def to_dict(self) -> dict:
        with self._lock:
            return {
                "job_id": self.id,
                "name": self.name,
                "status": self.status.value,
                "percent": self.percent,
                "message": self.message,
                "error": self.error,
                "created_at": self.created_at.isoformat(),
                "started_at": self.started_at.isoformat() if self.started_at else None,
                "finished_at": self.finished_at.isoformat() if self.finished_at else None,
                "duration_seconds": self.duration_seconds,
            }


class JobRegistry:
    """Tracks submitted jobs and prevents duplicate concurrent runs of the same work."""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def submit(
        self,
        name: str,
        func: Callable[..., Any],
        requested_by: int | None = None,
        dedupe: bool = True,
    ) -> Job:
        """
        Queues `func` for background execution.

        `func` receives a single `progress(percent, message)` callable it may invoke to
        report advancement. When `dedupe` is set and a job of the same name is already in
        flight, that existing job is returned instead of starting a redundant second run.
        """
        with self._lock:
            if dedupe:
                for job in self._jobs.values():
                    if job.name == name and job.status in (JobStatus.QUEUED, JobStatus.RUNNING):
                        logger.info("Reusing in-flight job '%s' (%s)", name, job.id)
                        return job

            job = Job(str(uuid.uuid4()), name, requested_by)
            self._jobs[job.id] = job
            self._evict_locked()

        _executor.submit(self._run, job, func)
        return job

    def _run(self, job: Job, func: Callable[..., Any]) -> None:
        job.status = JobStatus.RUNNING
        job.started_at = datetime.utcnow()
        job.set_progress(0, "Starting")

        try:
            job.result = func(job.set_progress)
            job.status = JobStatus.SUCCEEDED
            job.set_progress(100, "Completed")
        except Exception as exc:
            job.status = JobStatus.FAILED
            job.error = str(exc)
            job.set_progress(job.percent, f"Failed: {exc}")
            logger.error("Job '%s' (%s) failed: %s\n%s",
                         job.name, job.id, exc, traceback.format_exc())
        finally:
            job.finished_at = datetime.utcnow()

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list_jobs(self, limit: int = 20) -> list[Job]:
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)
        return jobs[:limit]

    def _evict_locked(self) -> None:
        """Drops the oldest finished jobs once the retention cap is exceeded."""
        if len(self._jobs) <= _MAX_RETAINED_JOBS:
            return

        finished = sorted(
            (j for j in self._jobs.values()
             if j.status in (JobStatus.SUCCEEDED, JobStatus.FAILED)),
            key=lambda j: j.finished_at or j.created_at,
        )
        for job in finished[: len(self._jobs) - _MAX_RETAINED_JOBS]:
            self._jobs.pop(job.id, None)


job_registry = JobRegistry()


class ResultCache:
    """
    TTL cache for expensive derived results such as the monitoring report.

    Kept separate from the document cache because entries are whole computed responses
    rather than Firestore documents, and they carry a generation timestamp so clients can
    show how fresh the figures are.
    """

    def __init__(self) -> None:
        self._store: dict[str, tuple[datetime, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: str, ttl_seconds: float) -> Any | None:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            generated_at, value = entry

        if (datetime.utcnow() - generated_at).total_seconds() > ttl_seconds:
            return None
        return value

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._store[key] = (datetime.utcnow(), value)

    def generated_at(self, key: str) -> datetime | None:
        with self._lock:
            entry = self._store.get(key)
        return entry[0] if entry else None

    def invalidate(self, key: str) -> None:
        with self._lock:
            self._store.pop(key, None)


report_cache = ResultCache()
