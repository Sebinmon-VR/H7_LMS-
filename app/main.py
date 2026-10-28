import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.core import firebase
from app.core.sqldb import DatabaseUnavailable

from app.api.v1.auth import router as auth_router
from app.api.v1.calls import router as calls_router
from app.api.v1.class_recordings import router as class_recordings_router
from app.api.v1.admin import router as admin_router
from app.api.v1.arena import manage_router as arena_manage_router, router as arena_router
from app.api.v1.lab import manage_router as lab_manage_router, router as lab_router
from app.api.v1.admissions import router as admissions_router
from app.api.v1.admission_requests import (
    PUBLIC_PREFIX as ADMISSION_REQUESTS_PUBLIC_PREFIX,
    admin_router as admission_requests_admin_router,
    public_router as admission_requests_router,
)
from app.api.v1.families import (
    admin_router as families_admin_router,
    parent_router as parent_router,
)
from app.api.v1.guide import guide_admin_router, guide_router
from app.api.v1.academics import (
    academics_router,
    cover_router,
    homework_router,
    leave_router,
    reports_router as parent_reports_router,
)
from app.api.v1.classes import (
    calendar_router,
    extra_router as extra_classes_router,
    router as live_classes_router,
)
from app.api.v1.finance import (
    admin_router as finance_admin_router,
    parent_router as parent_fees_router,
    student_router as student_fees_router,
)
from app.api.v1.support import (
    admin_router as support_admin_router,
    me_router as me_router,
    router as support_router,
)
from app.api.v1.notices import (
    admin_router as notices_admin_router,
    router as notices_router,
)
from app.api.v1.exams import (
    report_router as report_card_router,
    router as exam_router,
    student_router as student_exam_router,
)
from app.api.v1.teachers import router as teacher_router
from app.api.v1.students import router as student_router
from app.api.v1.storage import router as storage_router
from app.api.v1.tuition_admin import router as tuition_admin_router
from app.api.v1.tuition_admissions import router as tuition_admissions_router
from app.api.v1.tuition_library import router as tuition_library_router
from app.api.v1.tuition_notices import (
    admin_router as tuition_notices_admin_router,
    router as tuition_notices_router,
)
from app.api.v1.tuition_students import router as tuition_student_router
from app.api.v1.tuition_teachers import router as tuition_teacher_router
from app.core.config import settings
from app.db.init_db import init_db
from app.services.recordings import get_scheduler as get_recording_scheduler
from app.services.reminders import get_scheduler
from app.services.tuition.reminders import get_scheduler as get_tuition_scheduler
from app.services.live_classes import get_scheduler as get_class_scheduler

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
    if firebase.database_backend() == "azuresql":
        # Tables are also created lazily on first use; doing it here makes a fresh
        # deployment's first request fast and surfaces a bad connection string at startup.
        try:
            from app.core import sqldb

            sqldb.prepare_schema([c.collection_name for c in firebase.all_collections()])
            logger.info("Azure SQL backend ready: %s", sqldb.health())
        except Exception as exc:
            logger.warning("Azure SQL schema preparation skipped: %s", exc)

    try:
        init_db()
    except Exception as exc:
        logger.warning("Initial database seeding skipped: %s", exc)

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

    # School class housekeeping: opening due classes, closing ones nobody ended, and the
    # parent digests. Like the schedulers above it is a convenience - `class_started_at`
    # derives whether a class has begun without it, so a school whose sweep failed to start
    # still teaches and still lets students in on time.
    class_scheduler = get_class_scheduler()
    try:
        class_scheduler.start()
    except Exception as exc:
        logger.warning("Class maintenance scheduler could not start: %s", exc)

    yield

    try:
        class_scheduler.stop()
    except Exception as exc:
        logger.warning("Class maintenance scheduler did not stop cleanly: %s", exc)

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
        "Runs on Azure: Azure SQL for data, Blob Storage for files, and Azure Communication "
        "Services for live classes and email. "
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


# ---------------------------------------------------------------------------------------
# The public admission form
#
# The school's website is a different site on a different domain, and it posts the admission
# form here with no login. The listed-origins policy above exists to protect endpoints that
# accept a bearer token; these two accept none, so they answer to any origin. Handled by a
# middleware added AFTER CORSMiddleware - which makes it the outer one - because the inner
# one would otherwise refuse the preflight from an unlisted origin before any route ran.
# ---------------------------------------------------------------------------------------

_PUBLIC_CORS_PATH = f"{settings.API_V1_STR}{ADMISSION_REQUESTS_PUBLIC_PREFIX}"
_PUBLIC_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Accept, Origin, X-Requested-With, X-Timezone",
    "Access-Control-Max-Age": "600",
    "Vary": "Origin",
}


@app.middleware("http")
async def public_admission_form_cors(request: Request, call_next):
    path = request.url.path
    if path == _PUBLIC_CORS_PATH or path.startswith(_PUBLIC_CORS_PATH + "/"):
        if request.method == "OPTIONS":
            from fastapi.responses import Response

            return Response(status_code=204, headers=_PUBLIC_CORS_HEADERS)
        response = await call_next(request)
        for key, value in _PUBLIC_CORS_HEADERS.items():
            response.headers[key] = value
        # Credentials and a wildcard origin are mutually exclusive; the inner middleware may
        # have added this for a listed origin, and it must not travel with the wildcard.
        if "access-control-allow-credentials" in response.headers:
            del response.headers["access-control-allow-credentials"]
        return response
    return await call_next(request)


# ---------------------------------------------------------------------------------------
# When the database says no
#
# Left unhandled, a database outage surfaces as a bare "Internal Server Error" with no body,
# which the frontend renders verbatim and the person reading it takes for a bug in the app. A
# 503 with the reason is the same failure made actionable.
# ---------------------------------------------------------------------------------------


@app.exception_handler(DatabaseUnavailable)
async def sql_unavailable(request: Request, exc: DatabaseUnavailable):
    logger.error("Azure SQL failed on %s %s: %s", request.method, request.url.path, exc)
    return JSONResponse(
        status_code=503,
        headers={"Retry-After": "30"},
        # The driver's own words stay in the log above; the person sees a plain sentence.
        content={"detail": "The service is temporarily unavailable. Please try again in a minute.",
                 "code": "database_unavailable"},
    )


@app.middleware("http")
async def add_timing_header(request: Request, call_next):
    """
    Stamps every response with its server-side duration.

    Response time here is dominated by database round trips, so having the number on each
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

# Files saved to the API server's own disk: local development, and anything uploaded while
# cloud storage was unreachable.
uploads_path = Path(settings.LOCAL_STORAGE_DIR)
uploads_path.mkdir(parents=True, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=settings.LOCAL_STORAGE_DIR), name="uploads")


@app.get("/files/{blob_path:path}", include_in_schema=False)
def open_stored_file(blob_path: str, download: bool = False):
    """
    Every file in Blob storage is addressed as /files/<blob>, and answered here with a
    redirect to a read link that expires in AZURE_BLOB_LINK_MINUTES. The container itself is
    private, so a copied link stops working rather than serving the file for ever.

    No sign-in is asked for, because files open in new tabs, image tags and iframes that
    carry no bearer token - the same "anyone with the link" the files had on Google. The
    addresses carry a random part, so they cannot be guessed.
    """
    from app.core import azure_blob

    if not blob_path or ".." in blob_path.split("/"):
        return JSONResponse(status_code=404, content={"detail": "File not found"})
    if azure_blob.is_private(blob_path):
        # Class recordings and admission documents: opened through the LMS, which signs a
        # link for whoever may see them.
        return JSONResponse(status_code=403, content={"detail": "Open this file from the LMS."})
    try:
        return RedirectResponse(azure_blob.read_url(blob_path, download=download), status_code=307)
    except azure_blob.BlobStorageError as exc:
        return JSONResponse(status_code=503, content={"detail": str(exc)})

# Mount API Routers under /api/v1
app.include_router(auth_router, prefix=settings.API_V1_STR)
app.include_router(calls_router, prefix=settings.API_V1_STR)
app.include_router(class_recordings_router, prefix=settings.API_V1_STR)
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
app.include_router(tuition_admissions_router, prefix=settings.API_V1_STR)
app.include_router(tuition_notices_admin_router, prefix=settings.API_V1_STR)
app.include_router(tuition_notices_router, prefix=settings.API_V1_STR)

# Shared modules. These belong to neither product: a school running both announces one set of
# term dates, bills one household and keeps one set of session years. Each router carries its
# own role guards, and where a record concerns one product it says so with a `program` field.
app.include_router(admissions_router, prefix=settings.API_V1_STR)
# The website's admission form (public) and the office's queue for it (admin).
app.include_router(admission_requests_router, prefix=settings.API_V1_STR)
app.include_router(admission_requests_admin_router, prefix=settings.API_V1_STR)
app.include_router(families_admin_router, prefix=settings.API_V1_STR)
app.include_router(parent_router, prefix=settings.API_V1_STR)
app.include_router(notices_admin_router, prefix=settings.API_V1_STR)
app.include_router(notices_router, prefix=settings.API_V1_STR)
app.include_router(finance_admin_router, prefix=settings.API_V1_STR)
app.include_router(student_fees_router, prefix=settings.API_V1_STR)
app.include_router(parent_fees_router, prefix=settings.API_V1_STR)
app.include_router(live_classes_router, prefix=settings.API_V1_STR)
app.include_router(extra_classes_router, prefix=settings.API_V1_STR)
app.include_router(calendar_router, prefix=settings.API_V1_STR)
app.include_router(homework_router, prefix=settings.API_V1_STR)
app.include_router(leave_router, prefix=settings.API_V1_STR)
app.include_router(academics_router, prefix=settings.API_V1_STR)
app.include_router(cover_router, prefix=settings.API_V1_STR)
app.include_router(guide_router, prefix=settings.API_V1_STR)
app.include_router(guide_admin_router, prefix=settings.API_V1_STR)
app.include_router(parent_reports_router, prefix=settings.API_V1_STR)
app.include_router(support_router, prefix=settings.API_V1_STR)
app.include_router(support_admin_router, prefix=settings.API_V1_STR)
app.include_router(me_router, prefix=settings.API_V1_STR)
# Quiz battles, rewards and leaderboards (docs/ARENA.md).
app.include_router(arena_router, prefix=settings.API_V1_STR)
app.include_router(arena_manage_router, prefix=settings.API_V1_STR)
# The virtual chemistry lab: assignments and results (the chemistry runs in the browser).
app.include_router(lab_router, prefix=settings.API_V1_STR)
app.include_router(lab_manage_router, prefix=settings.API_V1_STR)


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


@app.get("/health/database", tags=["Health Check"])
def database_health():
    """
    Which document backend is in use, and whether it answers.

    `azuresql` keeps the collections as JSON-document tables inside one schema of an Azure
    SQL database. `firestore` was the original, kept only as a frozen backup of the data
    from before the move to Azure.
    """
    backend = firebase.database_backend()
    if backend == "azuresql":
        from app.core import sqldb

        return {"backend": backend, **sqldb.health()}
    return {"backend": backend, "detail": "Firestore is retired; set DATABASE_BACKEND=azuresql."}


@app.get("/health/cache", tags=["Health Check"])
def cache_health():
    """
    Reference-cache effectiveness.

    A low hit rate under normal traffic means responses are making more database round
    trips than necessary, which is the main driver of latency in this deployment.
    """
    from app.core.firebase import document_cache

    return {
        "reference_cache": document_cache.stats(),
        "ttl_seconds": settings.REFERENCE_CACHE_TTL_SECONDS,
        "query_concurrency": settings.QUERY_CONCURRENCY,
    }
