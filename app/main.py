import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.v1.auth import router as auth_router
from app.api.v1.admin import router as admin_router
from app.api.v1.teachers import router as teacher_router
from app.api.v1.students import router as student_router
from app.api.v1.storage import router as storage_router
from app.core.config import settings
from app.db.init_db import init_db
from app.services.reminders import get_scheduler

logger = logging.getLogger("lms_app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan manager.

    Neither startup task may take the API down: seeding and the reminder scheduler are both
    conveniences, and a school that cannot log in because a background thread failed to
    start is strictly worse off than one whose reminders are late.
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

    yield

    try:
        scheduler.stop()
    except Exception as exc:
        logger.warning("Class reminder scheduler did not stop cleanly: %s", exc)


app = FastAPI(
    title=settings.PROJECT_NAME,
    description=(
        "API-First Backend for LMS (Learning Management System). "
        "Integrated with Firebase Cloud Firestore & Google Cloud Storage APIs. "
        "Supports Authentication & RBAC, Students Module, Teachers Module, Admin Module, "
        "Independent Attendance Logging, Syllabus Topic Tracker, Live Meetings & Recordings, "
        "Book/Notes File Uploads, Exam Mark Entry, and Admin Monitoring Analytics."
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
    allow_headers=["Authorization", "Content-Type", "Accept", "Origin", "X-Requested-With"],
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
