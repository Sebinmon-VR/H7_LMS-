# LMS Backend API (Firebase-Only)

## Overview

This repository contains a FastAPI-based Learning Management System (LMS) backend that has been converted to use Firebase Cloud Firestore as its primary persistence layer. The codebase is designed as an API-first backend with support for:

- JWT authentication and role-based access control (RBAC)
- User registration, login, and profile retrieval
- Admin workflows for user management, class/subject creation, teacher assignments, and enrollments
- Teacher workflows for attendance tracking, topic logging, meeting scheduling, study material uploads, and exam grade entry
- Student workflows for viewing enrolled classes, attendance, topics, meetings, materials, and grades
- File uploads to Google Cloud Storage / Firebase Storage, with local storage fallback for development
- Firestore seeding for initial demo data

This implementation intentionally avoids SQLAlchemy and traditional relational database usage, relying instead on Firestore document storage and helper abstractions around the Firebase Admin SDK.

## Architecture

### Application Stack

- Python 3.13+
- FastAPI for HTTP API routing and request validation
- Pydantic for request/response schemas and settings management
- Firebase Admin SDK for Firestore database connectivity
- Google Cloud Storage client for file upload support
- JWT authentication using `python-jose`
- Password hashing via `passlib[bcrypt]`

### Key Concepts

- `app/main.py` defines the FastAPI application, middleware, router mounting, and application lifespan.
- `app/core/firebase.py` provides a generic `FirestoreService` wrapper for CRUD operations, query operations, document normalization, and hydration helpers.
- `app/core/gcp_services.py` provides unified storage handling for cloud uploads and a local fallback.
- `app/core/security.py` handles password hashing, JWT token creation, and token decoding.
- `app/api/v1` contains versioned API routers for authentication, admin operations, teacher operations, student operations, and storage.
- `app/schemas` defines Pydantic request and response models used across the API.
- `app/db/init_db.py` seeds demo users, classes, subjects, teacher mappings, and student enrollments into Firestore at startup.

## Folder Structure

- `app/`
  - `main.py` - Application entrypoint.
  - `api/v1/` - Versioned route modules.
  - `core/` - Application utilities and service wrappers.
  - `db/init_db.py` - Firestore seeding logic.
  - `models/` - Plain dataclass model definitions.
  - `schemas/` - Pydantic request/response schemas.
- `requirements.txt` - Python dependency manifest.
- `firebase_credentials.json` - Default service account credential file expected by config.
- `.env.example` - Example environment variable definitions.

## Configuration

Configuration is centralized in `app/core/config.py` and can be overridden via environment variables or a `.env` file.

Important settings include:

- `PROJECT_NAME` - Application name
- `API_V1_STR` - API prefix, default `/api/v1`
- `SECRET_KEY` - JWT signing key
- `ALGORITHM` - JWT algorithm, default `HS256`
- `ACCESS_TOKEN_EXPIRE_MINUTES` - JWT expiration duration
- `GCP_PROJECT_ID` - Firebase/Google Cloud project ID
- `GCP_BUCKET_NAME` - Google Cloud Storage bucket name
- `GOOGLE_APPLICATION_CREDENTIALS` - Path to service account JSON credentials
- `FIREBASE_CREDENTIALS_PATH` - Path to Firebase Admin SDK credentials JSON
- `USE_FIREBASE_DB` - Enables Firestore usage
- `USE_LOCAL_STORAGE` - Forces local uploads
- `LOCAL_STORAGE_DIR` - Local file upload storage directory

## Authentication

The backend uses JWT bearer tokens with the following flows:

- `POST /api/v1/auth/register`
  - Register a new user with a role (`ADMIN`, `TEACHER`, `STUDENT`).
  - Stores hashed passwords and user metadata in Firestore.

- `POST /api/v1/auth/login`
  - Authenticate using email and password.
  - Returns an access token, role, user ID, and full name.

- `POST /api/v1/auth/token`
  - OAuth2-compatible form login endpoint used by Swagger UI.

- `GET /api/v1/auth/me`
  - Returns the currently authenticated user's profile.

Authentication depends on the `Authorization: Bearer <token>` header in protected endpoints.

## Role-Based Access Control

RBAC is enforced through dependencies in `app/api/v1/dependencies.py`:

- `require_admin` - allows only `ADMIN` users
- `require_teacher` - allows `TEACHER` and `ADMIN`
- `require_student` - allows `STUDENT` and `ADMIN`
- `require_any_authenticated` - allows any authenticated role

These dependencies are applied to routes according to their intended audience.

## Firebase Firestore Usage

The backend uses Firestore collections to store all persistent entities. The Firestore helper exposes these collections as top-level services:

- `users`
- `class_rooms`
- `subjects`
- `teacher_subject_class_mappings`
- `student_enrollments`
- `attendance_records`
- `topics_covered`
- `live_meetings`
- `study_materials`
- `exam_grades`

`FirestoreService` supports:

- `add_document(doc_id, data)` - upsert document with merge semantics
- `get_document(doc_id)` - fetch by ID
- `get_document_by_field(field, value)` - resolve a single document by a field equality query
- `query_documents(field, op, value)` - query with Firestore operators
- `list_all()` - list every document in a collection
- `delete_document(doc_id)` - remove a document
- `get_next_numeric_id()` - compute the next numeric ID by scanning existing document IDs

Hydration helpers are provided for related entities so response objects can include nested metadata such as teacher details, class metadata, and subject metadata.

## API Endpoints

### Authentication

- `POST /api/v1/auth/register`
  - Registers a new user.
  - Request body: `full_name`, `email`, `password`, `role`

- `POST /api/v1/auth/login`
  - Login with email and password.
  - Request body: `email`, `password`

- `POST /api/v1/auth/token`
  - OAuth2-compatible login form for Swagger documentation.

- `GET /api/v1/auth/me`
  - Get current authenticated user's profile.

### Admin Module

- `POST /api/v1/admin/users`
  - Creates a new user account.

- `GET /api/v1/admin/users`
  - Lists all users with optional role filter.

- `PUT /api/v1/admin/users/{user_id}`
  - Updates user properties or active status.

- `DELETE /api/v1/admin/users/{user_id}`
  - Deactivates a user account.

- `POST /api/v1/admin/classes`
  - Creates a new class/section.

- `GET /api/v1/admin/classes`
  - Lists all classes.

- `POST /api/v1/admin/subjects`
  - Creates a new subject.

- `GET /api/v1/admin/subjects`
  - Lists all subjects.

- `POST /api/v1/admin/mappings/teacher-subject-class`
  - Assigns a teacher to a subject and class.

- `GET /api/v1/admin/mappings/teacher-subject-class`
  - Lists teacher/subject/class mappings.

- `POST /api/v1/admin/enrollments`
  - Enrolls a student in a class.

- `GET /api/v1/admin/enrollments`
  - Lists all student enrollments.

- `GET /api/v1/admin/reports/monitoring`
  - Returns system monitoring analytics including overall stats, teacher activity, and student performance.

### Teacher Module

- `GET /api/v1/teachers/my-classes`
  - Returns assigned classes and subject mappings.

- `GET /api/v1/teachers/classes/{class_id}/students`
  - Lists enrolled students in a class.

- `POST /api/v1/teachers/attendance`
  - Batch-mark attendance for students.

- `GET /api/v1/teachers/attendance`
  - Retrieve attendance records created by the teacher.

- `POST /api/v1/teachers/topics`
  - Log a syllabus topic covered.

- `GET /api/v1/teachers/topics`
  - Retrieve topics logged by the teacher.

- `POST /api/v1/teachers/meetings`
  - Schedule a live meeting or recording link.

- `GET /api/v1/teachers/meetings`
  - Retrieve scheduled meetings.

- `POST /api/v1/teachers/materials`
  - Upload study materials and save a Firestore record.

- `GET /api/v1/teachers/materials`
  - List study materials uploaded by the teacher.

- `POST /api/v1/teachers/grades`
  - Enter exam grades for a student.

- `GET /api/v1/teachers/grades`
  - List grades submitted by the teacher.

### Student Module

- `GET /api/v1/students/my-classes`
  - Returns the classes the student is enrolled in.

- `GET /api/v1/students/attendance`
  - Returns the student’s attendance history.

- `GET /api/v1/students/topics`
  - Returns topics covered for the student’s class.

- `GET /api/v1/students/meetings`
  - Returns live meeting and recording entries for the student’s class.

- `GET /api/v1/students/materials`
  - Returns uploaded materials for the student’s class.

- `GET /api/v1/students/grades`
  - Returns the student’s exam grades.

### Storage

- `POST /api/v1/storage/upload`
  - Uploads a file to Google Cloud Storage / Firebase Storage or local storage if cloud storage is unavailable.
  - Requires teacher or admin role.

## Data Models

The backend uses Pydantic schemas for validation and serialization, while Firestore persists raw document dictionaries.

Important schema modules:

- `app/schemas/auth.py`
- `app/schemas/user.py`
- `app/schemas/academic.py`
- `app/schemas/attendance.py`
- `app/schemas/topic.py`
- `app/schemas/meeting.py`
- `app/schemas/material.py`
- `app/schemas/grade.py`
- `app/schemas/reports.py`

Plain dataclass entities are defined in `app/models`, but Firestore operations are performed through `FirestoreService` and dictionaries rather than SQLAlchemy.

## Startup and Local Development

1. Install dependencies:

```bash
python -m pip install -r requirements.txt
```

2. Provide Firebase credentials either via `firebase_credentials.json` or by setting `GOOGLE_APPLICATION_CREDENTIALS` in environment variables.

3. Optionally configure settings in a `.env` file based on `.env.example`.

4. Start the server:

```bash
uvicorn app.main:app --reload --port 8000
```

5. Visit API docs:

- Swagger UI: `http://127.0.0.1:8000/docs`
- ReDoc: `http://127.0.0.1:8000/redoc`

## Seed Data

During startup, `app/db/init_db.py` seeds initial demo data if it does not already exist:

- Admin user: `admin@lms.com` / `admin123`
- Teacher user: `teacher.math@lms.com` / `teacher123`
- Student users: `student.alice@lms.com` / `student123`, `student.bob@lms.com` / `student123`
- Demo class: `CLASS-10A`
- Demo subject: `MATH101`
- Teacher assignment and student enrollments

## Notes and Limitations

- The backend is Firebase-only for persistence and no longer relies on SQLAlchemy.
- Firestore document IDs are treated as numeric strings and next IDs are generated by scanning existing documents. This is a simple strategy and not ideal for high-concurrency production use.
- `app/core/gcp_services.py` supports automatic fallback to local storage when Google Cloud Storage is unavailable.
- Email values are currently validated as strings in schemas and do not depend on `email-validator`.
- The project expects a valid Firebase Admin SDK credential file or default application credentials.

## Future Improvements

- Replace numeric ID generation with Firestore auto-generated IDs or dedicated ID sequence management.
- Add comprehensive unit and integration tests.
- Add request logging, error handling middleware, and rate limiting.
- Harden JWT secret management for production.
- Support more advanced grading and attendance reporting features.

## Contact

This backend is implemented as a foundation for an LMS system, with FastAPI as the web framework and Firebase as the primary storage service.

For issues or extensions, inspect the API routers under `app/api/v1/` and the Firestore service helpers in `app/core/firebase.py`.
