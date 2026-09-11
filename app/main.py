import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.v1.auth import router as auth_router
from app.api.v1.admin import router as admin_router
from app.api.v1.exams import (
    report_router as report_card_router,
    router as exam_router,
    student_router as student_exam_router,
)
from app.api.v1.teachers import router as teacher_router
from app.api.v1.students import router as student_router
from app.api.v1.storage import router as storage_router
from app.api.v1.tuition_admin import router as tuition_admin_router
from app.api.v1.tuition_library import router as tuition_library_router
from app.api.v1.tuition_students import router as tuition_student_router
from app.api.v1.tuition_teachers import router as tuition_teacher_router
from app.core.config import settings
from app.db.init_db import init_db
from app.services.recordings import get_scheduler as get_recording_scheduler
from app.services.reminders import get_scheduler
from app.services.tuition.reminders import get_scheduler as get_tuition_scheduler

logger = logging.getLogger("lms_app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan manager.

    No startup task may take the API down: seeding and the two background schedulers are all
    conveniences, and a school that cannot log in because a background thread failed to
    start is strictly worse off than one whose reminders are late or whose class recordings
    are filed on the next restart.
    """
    try:
        init_db()
    except Exception as exc:
        logger.warning("Initial Firestore seeding skipped: %s", exc)

    scheduler = get_scheduler()
    try:
        scheduler.start()
    except Exception as exc:
        logger.warning("Class reminder scheduler could not start: %s", exc)

    recording_scheduler = get_recording_scheduler()
    try:
        recording_scheduler.start()
    except Exception as exc:
        logger.warning("Class recording scheduler could not start: %s", exc)

    # The tuition sweep does three jobs on one cadence: class reminders, extending the
    # generated-session horizon, and closing classes nobody ended. Like the two above it is a
    # convenience - a tuition programme whose sweep failed to start still teaches, marks and
    # bills; it just needs the admin's "run maintenance" button.
    tuition_scheduler = get_tuition_scheduler()
    try:
        tuition_scheduler.start()
    except Exception as exc:
        logger.warning("Tuition scheduler could not start: %s", exc)

    yield

    try:
        scheduler.stop()
    except Exception as exc:
        logger.warning("Class reminder scheduler did not stop cleanly: %s", exc)

    try:
        recording_scheduler.stop()
    except Exception as exc:
        logger.warning("Class recording scheduler did not stop cleanly: %s", exc)

    try:
        tuition_scheduler.stop()
    except Exception as exc:
        logger.warning("Tuition scheduler did not stop cleanly: %s", exc)


app = FastAPI(
    title=settings.PROJECT_NAME,
    description=(
        "API-First Backend for LMS (Learning Management System). "
        "Integrated with Firebase Cloud Firestore & Google Cloud Storage APIs. "
        "Supports Authentication & RBAC, Students Module, Teachers Module, Admin Module, "
        "Independent Attendance Logging, Syllabus Topic Tracker, Live Meetings & Recordings, "
        "Book/Notes File Uploads, Online & Offline Exams with Answer Keys and Valuation, "
        "Report Cards, and Admin Monitoring Analytics."
    ),
    version="1.0.0",
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan
)

# CORS for browser frontends.
#
# Origins are listed rather than wildcarded: `allow_origins=["*"]` cannot be combined with
# credentials, and a wildcard on an API that accepts bearer tokens lets any page on the web
# drive it with a token it has stolen. `Authorization` is named explicitly in allow_headers
# because it is not a CORS-safelisted header - without it every authenticated call fails at
# the preflight, which reads in the browser as a generic "blocked by CORS" error.
# `Server-Timing` is exposed so browser devtools and the frontend can read server duration.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_origin_regex=settings.CORS_ALLOW_ORIGIN_REGEX or None,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept", "Origin", "X-Requested-With", "X-Timezone"],
    expose_headers=["Server-Timing", "X-Response-Time-Ms"],
    max_age=600,
)

logger.info(
    "CORS allowed origins: %s (regex: %s)",
    settings.cors_origins or "none",
    settings.CORS_ALLOW_ORIGIN_REGEX or "none",
)


@app.middleware("http")
async def add_timing_header(request: Request, call_next):
    """
    Stamps every response with its server-side duration.

    Response time here is dominated by Firestore round trips, so having the number on each
    response makes a regression visible immediately instead of being guessed at. Slow
    requests are logged so they surface without needing a profiler attached.
    """
    started = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = (time.perf_counter() - started) * 1000

    response.headers["Server-Timing"] = f"app;dur={elapsed_ms:.1f}"
    response.headers["X-Response-Time-Ms"] = f"{elapsed_ms:.1f}"

    if elapsed_ms > 1500:
        logger.warning("Slow request: %s %s took %.0f ms",
                       request.method, request.url.path, elapsed_ms)
    return response

# Serve uploaded study materials static files for local testing
uploads_path = Path(settings.LOCAL_STORAGE_DIR)
uploads_path.mkdir(parents=True, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=settings.LOCAL_STORAGE_DIR), name="uploads")

# Mount API Routers under /api/v1
app.include_router(auth_router, prefix=settings.API_V1_STR)
app.include_router(admin_router, prefix=settings.API_V1_STR)
app.include_router(teacher_router, prefix=settings.API_V1_STR)
app.include_router(student_router, prefix=settings.API_V1_STR)
app.include_router(storage_router, prefix=settings.API_V1_STR)
# The exam module carries its own routers because the same operations are performed by
# three different roles; each endpoint enforces its own role guard.
app.include_router(exam_router, prefix=settings.API_V1_STR)
app.include_router(report_card_router, prefix=settings.API_V1_STR)
app.include_router(student_exam_router, prefix=settings.API_V1_STR)

# The online tuition product. A separate set of routers sharing this application's login,
# user table and infrastructure - see `app.models.tuition` for why the domain underneath is
# genuinely different rather than a filtered view of the LMS. Access is gated by the
# `programs` list on a profile, checked after the role guard on every route.
app.include_router(tuition_admin_router, prefix=settings.API_V1_STR)
app.include_router(tuition_teacher_router, prefix=settings.API_V1_STR)
app.include_router(tuition_student_router, prefix=settings.API_V1_STR)
app.include_router(tuition_library_router, prefix=settings.API_V1_STR)


@app.get("/", tags=["Health Check"])
def root():
    """
    Root API health check endpoint.
    """
    return {
        "status": "online",
        "project": settings.PROJECT_NAME,
        "documentation": "/docs",
        "api_v1": settings.API_V1_STR
    }


@app.get("/health/firestore", tags=["Health Check"])
def firestore_health():
    """
    Whether the database is actually reachable, and which credentials got it there.

    An unauthenticated Firestore client is not an error anywhere in this codebase: every
    read short-circuits to an empty list, so a misconfigured deployment answers every
    endpoint with `200 []` and looks like a database that was never populated. This
    endpoint is the difference between those two states. It reports no secret material -
    only the service account's address, which is what you would grant IAM roles to anyway.
    """
    import os

    from app.core.firebase import initialize_firebase

    database = initialize_firebase()
    source = None
    client_email = None

    for label, path in (
        ("FIREBASE_CREDENTIALS_PATH", settings.FIREBASE_CREDENTIALS_PATH),
        ("GOOGLE_APPLICATION_CREDENTIALS", settings.GOOGLE_APPLICATION_CREDENTIALS),
    ):
        if path and os.path.exists(path):
            source = f"{label}={path}"
            try:
                import json
                with open(path, encoding="utf-8") as handle:
                    client_email = json.load(handle).get("client_email")
            except Exception:  # pragma: no cover - diagnostics must never raise
                pass
            break

    probe = "not attempted"
    if database is not None:
        try:
            # One trivial round trip. A client can construct successfully and still fail
            # here when the key is valid but the project or IAM role is wrong.
            next(iter(database.collection("users").limit(1).stream()), None)
            probe = "ok"
        except Exception as exc:
            probe = f"failed: {exc}"

    return {
        "firestore_available": database is not None,
        "read_probe": probe,
        "project_id": settings.GCP_PROJECT_ID,
        "credentials_source": source or "application default credentials (no key file found)",
        "credentials_json_env_set": bool(settings.FIREBASE_CREDENTIALS_JSON),
        "service_account": client_email,
    }


@app.get("/health/cache", tags=["Health Check"])
def cache_health():
    """
    Reference-cache effectiveness.

    A low hit rate under normal traffic means responses are making more Firestore round
    trips than necessary, which is the main driver of latency in this deployment.
    """
    from app.core.firebase import document_cache

    return {
        "reference_cache": document_cache.stats(),
        "ttl_seconds": settings.REFERENCE_CACHE_TTL_SECONDS,
        "query_concurrency": settings.QUERY_CONCURRENCY,
    }
