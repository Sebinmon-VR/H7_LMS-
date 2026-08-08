import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.v1.auth import router as auth_router
from app.api.v1.admin import router as admin_router
from app.api.v1.teachers import router as teacher_router
from app.api.v1.students import router as student_router
from app.api.v1.storage import router as storage_router
from app.core.config import settings
from app.db.init_db import init_db

logger = logging.getLogger("lms_app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan manager.
    """
    try:
        init_db()
    except Exception as exc:
        logger.warning("Initial Firestore seeding skipped: %s", exc)
    yield


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

# Enable CORS for frontend applications (React, Next.js, Vue, Flutter, mobile apps)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
