# LMS Backend API (Firebase + Google Workspace)

## Overview

This repository contains a FastAPI-based Learning Management System (LMS) backend built on Firebase Cloud Firestore as its persistence layer and Google Workspace for identity, conferencing, and document storage. The codebase is API-first and supports:

- **Firebase Authentication** for login, password management, and session revocation (the backend never stores passwords)
- Role-based access control (RBAC) for `ADMIN`, `TEACHER`, and `STUDENT`
- Admin workflows for user management, class/subject CRUD, teacher assignments, and enrollments
- Teacher workflows for attendance, topic logging, meeting scheduling, material uploads, and grade entry
- Student workflows for viewing classes, attendance, topics, meetings, materials, and grades
- **Google Meet links generated automatically** as real Google Calendar events, with enrolled students invited
- **Pluggable file storage**: Google Cloud Storage, a Workspace Shared Drive, or local disk
- **Full update and delete coverage** across every resource, with referential-integrity guards
- Firestore seeding for initial demo data

The implementation avoids SQLAlchemy and relational databases entirely, relying on Firestore document storage with helper abstractions around the Firebase Admin SDK.

---

## What's New

### Public registration removed (security fix)

`POST /api/v1/auth/register` accepted a `role` field from an **unauthenticated** request
body. Any caller who could reach the API was able to create themselves an `ADMIN` account.
The endpoint is gone.

Accounts are now created only by an administrator through `POST /api/v1/admin/users`, or by
the provisioning scripts. Signing in with Google grants **no role by itself** — a Google
account with no LMS profile is rejected with `403`, never given a default role.

The system therefore needs one account to exist before anyone can sign in. That bootstrap
administrator is created at startup from `BOOTSTRAP_ADMIN_EMAIL` and
`BOOTSTRAP_ADMIN_PASSWORD` if it is absent, and left untouched if it already exists.

### No mock data

Startup previously seeded four demo users, a class, a subject, a teacher mapping, and two
enrollments. It now creates **only** the bootstrap administrator, so a fresh database
starts genuinely empty. Set `SEED_DEMO_DATA=True` if you want sample records locally.

`scripts/reset_data.py` wipes every collection and recreates that single admin, for
starting over from zero.

### Performance: 4.8x faster overall, 1800x on the monitoring report

The API was slow because **every Firestore round trip costs ~850 ms** from outside the
database's region. Response time was therefore driven almost entirely by *how many* calls a
request made, not by how much data it moved. Measured on the real database:

| Access pattern | Cost per document |
| --- | --- |
| Sequential `get_document` | 852 ms |
| Parallel (8 threads) | 214 ms |
| Batched `get_all` | **52 ms** |

Four changes followed from that, measured across all 17 read endpoints:

| Endpoint | Before | After (warm) | Gain |
| --- | --- | --- | --- |
| `GET /admin/reports/monitoring` | 21 651 ms | 12 ms | 1800x |
| `GET /admin/enrollments` | 7 622 ms | 414 ms | 18x |
| `GET /auth/me` | 1 101 ms | 11 ms | 100x |
| `GET /admin/users` | 2 687 ms | 370 ms | 7x |
| **All 17 endpoints** | **61.4 s** | **12.7 s** | **4.8x** |

**1. Batched reads replaced the N+1 hydration.** Hydrating a list resolved each record's
teacher, class, and subject one call at a time, so an N-row response cost 3N round trips.
`prefetch_references()` now collects every referenced ID across the whole result set and
issues one batched `get_all` per collection, concurrently. Per-record hydration afterwards
hits the cache and performs no I/O.

**2. Reference data is cached.** Users, classes, and subjects are small and change rarely,
yet were re-read constantly. They now sit behind a 60-second TTL cache
(`REFERENCE_CACHE_TTL_SECONDS`) that reaches a **92% hit rate** in normal use. Writes
invalidate the affected key immediately, so a caller never reads back a stale version of
its own write. Transactional collections such as attendance and grades are deliberately
*not* cached.

**3. The monitoring report was rewritten.** It previously issued
`4 + 4×teachers + 3×students` sequential queries — over 21 seconds, growing with every user
added. It now reads each collection once in parallel and derives every per-teacher and
per-student figure by grouping in memory. **Query count is constant regardless of school
size.** Results are cached for `MONITORING_REPORT_TTL_SECONDS` (default 5 minutes).

**4. Creating a record no longer scans a collection.** `get_next_numeric_id()` streamed
every document in the collection on *every single create* to find the maximum ID — a full
round trip that grew with collection size. IDs now come from a monotonic clock with a
per-process counter, requiring no Firestore access at all, in the same
millisecond-timestamp format already stored.

Delete dependency checks were also parallelised (7 sequential checks → one wave), taking
deletes from ~6 s to ~1.6 s.

**Observability.** Every response carries `Server-Timing` and `X-Response-Time-Ms`, and any
request over 1500 ms is logged as a warning. `GET /health/cache` reports cache hit rate.

**The single biggest remaining win is infrastructure, not code:** the ~850 ms per-call
latency is the distance between your server and the Firestore region. Deploying the backend
in the same region as the database would cut the remaining floor by roughly an order of
magnitude, and no amount of application-level work substitutes for it.

### Background jobs with progress reporting

Work too slow to sit inside a request now runs in the background and reports progress
instead of making the user watch a spinner.

```
POST /api/v1/admin/reports/monitoring/refresh   ->  202 { job_id, poll_url, status }
GET  /api/v1/admin/jobs/{job_id}                ->  { status, percent, message, duration_seconds }
GET  /api/v1/admin/jobs                         ->  recent jobs, for a notifications panel
```

`status` is `QUEUED`, `RUNNING`, `SUCCEEDED`, or `FAILED`; `percent` and `message` are what
a progress bar or toast should display. Requesting a refresh while one is already running
returns the *same* job rather than starting a duplicate.

Job state is held in memory, which carries two honest limitations: it does not survive a
process restart, and with multiple server workers a poll may land on a worker that never
saw the job. Both are acceptable here because every job is a recomputation of derived data,
never a source of truth. A shared store or real task queue is the fix if that changes.

### Authentication moved to Firebase Auth (breaking change for clients)

Login previously verified a bcrypt hash stored in the Firestore `users` collection and issued a backend-signed HS256 JWT. That is gone.

Clients now sign in with the **Firebase SDK** (email/password or Google sign-in) and send the resulting **Firebase ID token** as `Authorization: Bearer <idToken>`. The backend verifies it with the Admin SDK.

- Passwords, resets, email verification, and token revocation are owned by Firebase.
- `hashed_password` is no longer written to Firestore.
- User profiles link to Firebase accounts via a new `firebase_uid` field. **Numeric user IDs are unchanged**, so all existing references (`teacher_id`, `student_id`) and client code keep working.
- The LMS role is mirrored into Firebase **custom claims** (`role`, `lms_user_id`) so a frontend can gate UI directly from the token. The Firestore profile remains authoritative for the backend.
- `ALLOW_LEGACY_JWT_LOGIN=True` keeps previously issued backend JWTs valid during the transition. Set it to `False` after cutover.

### Google Meet integration

`POST /api/v1/teachers/meetings` now creates a real Google Calendar event with an attached Meet link, owned by the teacher, with enrolled students added as attendees so they receive calendar invitations. Editing or deleting the meeting propagates to Calendar.

If Meet generation is unavailable — delegation not yet propagated, teacher outside the Workspace domain, quota exhausted — the endpoint still returns `201` with a null `meeting_link` rather than failing. **A misconfiguration degrades the feature; it never takes the endpoint down.** The response now carries `meet_status` (`CREATED` / `FAILED` / `MANUAL` / `SKIPPED`) and `meet_error`, so a missing link is explained instead of silent, and `POST /admin/meetings/{id}/regenerate-link` retries once the configuration is fixed.

Every event is stamped with `GOOGLE_CALENDAR_TIMEZONE`. Calendar rejects a naive `dateTime` that arrives without a zone, which was the most common reason link generation produced nothing at all.

**Scopes are probed, not assumed.** Google fails the *entire* token exchange with `unauthorized_client` if any single requested scope is missing from the domain-wide delegation grant, so requesting extra scopes "just in case" breaks an otherwise working setup. The client tries `calendar.events` first (all that Meet requires), then wider sets, and remembers whichever authenticates. Pin the set with `GOOGLE_CALENDAR_SCOPES` if you prefer. `GET /admin/integrations` reports the granted scopes and the numeric Client ID the delegation form asks for.

### Google Drive storage option

`StorageService` changed from a local/cloud boolean into a three-way `STORAGE_PROVIDER` switch (`GCS` | `DRIVE` | `LOCAL`). Two Drive layouts are supported:

- **Shared Drive** (`GOOGLE_DRIVE_SHARED_DRIVE_ID`) — recommended. Files are owned by the organization, so the service account's zero personal Drive quota is irrelevant and no impersonation is needed.
- **Ordinary folder** (`GOOGLE_DRIVE_FOLDER_ID`) — requires `GOOGLE_DRIVE_IMPERSONATION=True`, because a service account cannot own files outside a Shared Drive.

Either value may be pasted as a full `drive.google.com` URL; the ID is unwrapped automatically.

Each material records which backend stored it (`storage_provider`), so GCS-era and Drive-era files coexist and delete correctly. When the configured provider cannot be reached the file still saves to local disk, but the response says so via `storage_provider: "LOCAL"` plus a `storage_warning` — set `STORAGE_STRICT=True` to return `502` instead of falling back.

**Recommendation:** keep `GCS` as the default. It is cheaper and faster for plain file serving. Use `DRIVE` when teachers need in-place preview and collaborative editing — Drive's value here is collaboration, not storage.

### Weekly timetable

A timetable entry is a *recurring rule* — "Grade 7A does Maths with Mr Rahman on Tuesdays, 09:00–09:45, Room 12, from 1 Sep to 20 Dec" — not one row per calendar day. A term's schedule is a few dozen rows.

Times are stored as local `HH:MM` and interpreted in `SCHOOL_TIMEZONE`, because a period is a wall-clock fact: assembly is at 08:00 whether or not the clocks changed last weekend. Storing an absolute instant would drift across a DST boundary.

Writes are checked for clashes — a class cannot be in two lessons at once, and a teacher cannot be in two rooms at once — and refused with `409` naming the conflicting entries. `?allow_conflicts=true` overrides. Room double-booking is deliberately *not* checked: schools reuse room labels loosely and rejecting on it would block real timetables for a cosmetic reason.

The `/timetable/day` and `/timetable/upcoming` endpoints resolve those rules against real dates and return absolute `starts_at` / `ends_at` plus `starts_in_minutes` and `is_current`, so no client has to reimplement weekday and timezone arithmetic.

### Class reminders

A background sweep every `REMINDER_SCAN_INTERVAL_SECONDS` finds periods entering a reminder window and emails the enrolled students, plus the teacher when `REMIND_TEACHERS` is on. `CLASS_REMINDER_MINUTES_BEFORE` accepts several offsets (`"60,15"`) for more than one nudge. When a live meeting is scheduled for the same class, subject, and slot, its Meet link is included in the email.

Two properties matter more than anything else, because the failure mode of a reminder system is not silence — it is spam:

- **Exactly once.** Before sending, the sweep *claims* a Firestore document keyed by (entry, date, offset, recipient) using an atomic `create`. A second server worker, a restart mid-sweep, or a manually triggered run all lose the race and skip. The claim is written *before* the email, so the worst case is a dropped reminder rather than a duplicate — the right way round for something that lands in someone's inbox.
- **No catch-up floods.** A reminder that came due more than `REMINDER_MAX_LATENESS_MINUTES` ago is dropped. A server that was down all morning comes back quiet instead of emailing everyone about classes that already finished.

Reminders are **opt-out** per user (`reminder_opt_in`): enrolling a student means intending to tell them when class starts.

### Richer user profiles

`UserCreate` and `UserUpdate` now carry contact, address, student (admission number, roll number, guardian contact, blood group) and staff (employee ID, qualification, joining date) detail. Everything is optional — a school onboarding a hundred students has partial data for most of them, and refusing the record until every field is filled just pushes the gap into a spreadsheet. `admission_number` and `employee_id` are rejected when already held by another user.

`PUT /admin/users/{id}` is genuinely partial: omitted **and** `null` fields are both left unchanged, so a form that defaults its untouched inputs to null cannot erase data the editor never saw.

### Permanent user deletion

`DELETE /admin/users/{id}` still soft-deletes by default. `?permanent=true` erases the Firestore profile and the Firebase Auth account, refusing with `409` while anything still references the user and listing what; `&force=true` cascades. Two guards apply to both modes: an admin cannot permanently delete the account they are signed in with, and the last active admin cannot be removed at all — locking everyone out of the system is never the intended outcome of a delete.

### Integration diagnostics

`GET /api/v1/admin/integrations` probes Drive, Cloud Storage, Calendar/Meet, and SMTP and reports `ok` plus a `detail` naming the precise misconfiguration — Drive API not enabled on the project, domain-wide delegation refused, a Shared Drive ID the service account cannot see, a folder layout that will hit `storageQuotaExceeded`. `POST /api/v1/admin/integrations/storage/test-upload` proves a real write works by uploading a probe file and deleting it. Teachers get the storage half at `GET /api/v1/storage/status`.

### Full update and delete coverage

The API previously had exactly one delete endpoint (a soft user deactivation). It now has **18 update/delete endpoints**.

- **Users soft-delete** — deactivated, not removed, so attendance and grade history stay intact and keep resolving. The linked Firebase account is disabled and its sessions revoked. A new `POST /admin/users/{id}/reactivate` reverses it.
- **Everything else hard-deletes**, guarded by referential-integrity checks. Deleting a class that still has enrollments returns `409` naming the blocking resources and counts. Pass `?force=true` to cascade.
- **Teacher records are ownership-scoped.** A teacher may only modify records they created; admins bypass the check.

### Fixes and cleanup

- Fixed a `NameError` crash in `hydrate_teacher_mapping` and `hydrate_student_enrollment` that made `GET /admin/mappings/teacher-subject-class` and `GET /admin/enrollments` return `500`.
- Fixed silent file overwrites: uploads used the raw filename as the object path, so two teachers uploading `notes.pdf` to the same class clobbered each other. Filenames now carry a unique prefix.
- Migrated Firestore queries to the modern `FieldFilter` form; the positional `.where(field, op, value)` call was deprecated and emitting warnings.
- Removed `hash_password` / `verify_password` / `create_access_token` and dropped the `passlib` and `bcrypt` dependencies. Leaving password-hashing helpers in a codebase that no longer stores passwords invites their reintroduction.
- Added the shared helpers `require_document`, `assert_owner`, `count_references`, and `delete_with_dependencies` in `app/core/firebase.py`, replacing repeated fetch-check-raise blocks across the routers.

---

## Architecture

### Application Stack

- Python 3.13+
- FastAPI for HTTP API routing and request validation
- Pydantic for request/response schemas and settings management
- Firebase Admin SDK for Firestore and Firebase Authentication
- Google API Python Client for Calendar (Meet) and Drive
- Google Cloud Storage client for bucket uploads
- `python-jose` for validating legacy tokens only

### Key Modules

| Path | Responsibility |
| --- | --- |
| `app/main.py` | FastAPI application, middleware, router mounting, lifespan |
| `app/core/firebase.py` | `FirestoreService` CRUD wrapper, batched reads, reference cache, hydration helpers, delete guards |
| `app/core/concurrency.py` | `run_parallel` for independent, latency-bound Firestore work |
| `app/core/jobs.py` | Background job registry with progress reporting, and the result cache |
| `app/core/firebase_auth.py` | Firebase Auth: token verification, account creation, custom claims, disable/revoke |
| `app/core/google_meet.py` | Google Meet link generation via the Calendar API, plus `check_access()` diagnostics |
| `app/core/google_drive.py` | Shared Drive / folder uploads, folder resolution, deletion, plus `check_access()` diagnostics |
| `app/core/gcp_services.py` | Storage provider switch (GCS / Drive / Local) |
| `app/core/security.py` | Legacy JWT decoding only; removable after cutover |
| `app/api/v1/` | Versioned routers: auth, admin, teachers, students, storage |
| `app/services/content.py` | Meeting scheduling and material storage shared by the teacher and admin routers |
| `app/services/timetable.py` | Recurring period storage, clash detection, resolution against calendar dates |
| `app/services/reminders.py` | Timetable-driven reminder sweep and its background scheduler |
| `app/schemas/` | Pydantic request/response models |
| `app/db/init_db.py` | Firestore seeding at startup |
| `scripts/migrate_users_to_firebase_auth.py` | One-time migration linking existing users to Firebase Auth |

Every Google integration **fails soft**: missing credentials or unauthorized delegation produce a logged warning and a degraded response, never a crash. This keeps the API importable and testable without a live Google project. Failing soft is not the same as failing silently — the reason travels back to the caller in `meet_error` / `storage_warning`, and `GET /admin/integrations` reports it in full.

---

## Configuration

Configuration is centralized in `app/core/config.py` and overridable via environment variables or `.env`. See `.env.example` for the full annotated list.

### Core

| Variable | Default | Purpose |
| --- | --- | --- |
| `PROJECT_NAME` | `LMS Backend API` | Application name |
| `API_V1_STR` | `/api/v1` | API prefix |
| `DEBUG` | `True` | Debug mode |
| `GCP_PROJECT_ID` | — | Firebase/Google Cloud project ID |
| `FIREBASE_CREDENTIALS_PATH` | `./firebase_credentials.json` | Firebase Admin SDK credentials |
| `GOOGLE_APPLICATION_CREDENTIALS` | `./service_account.json` | Service account JSON (falls back to the above) |
| `USE_FIREBASE_DB` | `True` | Enables Firestore usage |

### Performance

| Variable | Default | Purpose |
| --- | --- | --- |
| `REFERENCE_CACHE_TTL_SECONDS` | `60` | Lifetime of cached user/class/subject documents |
| `QUERY_CONCURRENCY` | `8` | Thread-pool width for independent Firestore reads |
| `MONITORING_REPORT_TTL_SECONDS` | `300` | How long a computed monitoring report stays fresh |

### Authentication

| Variable | Default | Purpose |
| --- | --- | --- |
| `AUTH_PROVIDER` | `FIREBASE` | `FIREBASE` or `LEGACY_JWT` |
| `ALLOW_LEGACY_JWT_LOGIN` | `True` | Accept legacy backend-issued JWTs during migration |
| `FIREBASE_WEB_API_KEY` | `""` | Firebase Web API Key. Required **only** for the dev password-login helper, the Swagger auth modal, and `verify_lms.py` |
| `SECRET_KEY` / `ALGORITHM` / `ACCESS_TOKEN_EXPIRE_MINUTES` | — | Legacy token validation only |

### Google Workspace

| Variable | Default | Purpose |
| --- | --- | --- |
| `GOOGLE_WORKSPACE_DOMAIN` | `""` | Your Workspace domain, e.g. `yourschool.com` |
| `GOOGLE_IMPERSONATION_FALLBACK` | `""` | Workspace user impersonated when a teacher's email is outside the domain |
| `ENABLE_GOOGLE_MEET` | `True` | Auto-create Calendar events with Meet links |
| `GOOGLE_CALENDAR_ID` | `primary` | Target calendar |
| `GOOGLE_CALENDAR_TIMEZONE` | `Asia/Dubai` | IANA zone stamped on events whose time carries no offset. Calendar rejects a naive time without it |
| `GOOGLE_CALENDAR_IMPERSONATION` | `True` | Set `False` on a non-Workspace project; `GOOGLE_CALENDAR_ID` must then name a calendar shared with the service account |
| `GOOGLE_MEET_INVITE_ATTENDEES` | `True` | Invite enrolled students as Calendar guests. Turning it off still yields a Meet link |
| `GOOGLE_CALENDAR_SCOPES` | `""` | Pin the Calendar OAuth scopes. Blank probes narrowest-first and settles on whatever delegation granted |

### Timetable & reminders

| Variable | Default | Purpose |
| --- | --- | --- |
| `SCHOOL_TIMEZONE` | falls back to `GOOGLE_CALENDAR_TIMEZONE` | IANA zone timetable periods are interpreted in |
| `ENABLE_CLASS_REMINDERS` | `True` | Run the background reminder sweep |
| `CLASS_REMINDER_MINUTES_BEFORE` | `15` | Minutes before a period to email. Comma-separated for several nudges, e.g. `60,15` |
| `REMINDER_SCAN_INTERVAL_SECONDS` | `120` | Sweep interval. Keep below the smallest offset or a window can be stepped over |
| `REMIND_TEACHERS` | `True` | Also email the teacher taking the period |
| `REMINDER_MAX_LATENESS_MINUTES` | `10` | Drop reminders overdue by more than this, so a restart cannot flood everyone |

### Storage

| Variable | Default | Purpose |
| --- | --- | --- |
| `STORAGE_PROVIDER` | `GCS` | `GCS`, `DRIVE`, or `LOCAL` |
| `USE_LOCAL_STORAGE` | `False` | Legacy flag. Forces `LOCAL` **only when `STORAGE_PROVIDER` is left unset** — it no longer silently overrides an explicit choice |
| `STORAGE_STRICT` | `False` | `True` returns `502` when the cloud provider is unreachable instead of falling back to local disk |
| `GCP_BUCKET_NAME` | — | Cloud Storage bucket |
| `GOOGLE_DRIVE_SHARED_DRIVE_ID` | `""` | Shared Drive destination. A full `drive.google.com` URL is accepted and unwrapped |
| `GOOGLE_DRIVE_FOLDER_ID` | `""` | Ordinary-folder destination; requires `GOOGLE_DRIVE_IMPERSONATION=True` |
| `GOOGLE_DRIVE_IMPERSONATION` | `True` | Act as `GOOGLE_IMPERSONATION_FALLBACK` for Drive. Leave `False` with a Shared Drive — no delegation needed |
| `GOOGLE_DRIVE_LINK_SHARING` | `True` | Grant "anyone with the link can view" on uploads |
| `GOOGLE_DRIVE_ROOT_FOLDER_NAME` | `H7 LMS Materials` | Folder created inside the destination to group LMS uploads |
| `LOCAL_STORAGE_DIR` | `./uploads` | Local upload directory |

---

## Google Cloud Console Setup

Meet and Drive require Workspace configuration before they function. Firestore and Firebase Auth work without these steps.

### 1. Enable APIs

```bash
gcloud config set project <YOUR_PROJECT_ID>
gcloud services enable \
  identitytoolkit.googleapis.com \
  firestore.googleapis.com \
  storage.googleapis.com \
  calendar-json.googleapis.com \
  drive.googleapis.com \
  iamcredentials.googleapis.com
```

| API | Console name | Needed for |
| --- | --- | --- |
| `identitytoolkit` | Identity Toolkit API | Firebase Auth |
| `calendar-json` | Google Calendar API | Meet link generation |
| `drive` | Google Drive API | Shared Drive uploads |
| `iamcredentials` | IAM Service Account Credentials API | Delegation token minting |

### 2. Enable Firebase Auth providers

Firebase Console → **Authentication → Get started**:

- Enable **Email/Password**.
- Enable **Google**, restricted to your Workspace domain.
- Under **Settings → Authorized domains**, add `localhost` and your deployed hostname.

### 3. Authorize domain-wide delegation (requires a Workspace super-admin)

This is what allows the backend to act as a teacher without a per-user consent screen.

1. Cloud Console → **IAM & Admin → Service Accounts** → open the service account → **Details** tab → copy the **Unique ID** (a ~21-digit number, *not* the email).
2. Go to **admin.google.com** → **Security → Access and data control → API controls → Manage Domain Wide Delegation → Add new**.
3. **Client ID** = the numeric Unique ID from step 1.
4. **OAuth scopes**, comma-separated with no spaces:
   ```
   https://www.googleapis.com/auth/calendar.events,https://www.googleapis.com/auth/drive
   ```
5. Authorize. Propagation is usually under a minute, but Google documents up to 24 hours.

> **Scope discipline:** delegation lets the backend impersonate *any* user in the domain within the granted scopes. Grant only these two.

### 4. Create the Shared Drive (only if using Drive storage)

1. **drive.google.com → Shared drives → New**.
2. **Manage members** → add the service account email as **Content manager**.
3. Copy the ID from the URL `drive.google.com/drive/folders/<ID>` into `GOOGLE_DRIVE_SHARED_DRIVE_ID`.

### 5. Grant IAM roles

Grant the service account `roles/datastore.user`, `roles/storage.objectAdmin`, and `roles/firebaseauth.admin`.

---

## Authentication

### Client flow

1. The client signs in with the Firebase Web/Mobile SDK — email/password, or Google sign-in restricted to the Workspace domain.
2. Firebase returns an **ID token**.
3. The client sends it on every request as `Authorization: Bearer <idToken>`.
4. The backend verifies signature, expiry, audience, and revocation, then resolves the Firestore profile.

Profile lookup is by `firebase_uid`, falling back to email. When a profile predates the migration and matches by email, the `firebase_uid` link is backfilled automatically on first authenticated request.

### Endpoints

- `POST /api/v1/auth/register` — creates the Firebase Auth account and the Firestore profile. `password` is optional; omit it for Google-sign-in-only accounts or to have the user set one via a Firebase reset email.
- `GET /api/v1/auth/me` — returns the authenticated user's profile.
- `POST /api/v1/auth/login` — **development helper.** Exchanges email and password for a Firebase ID token via the Identity Toolkit REST API. Requires `FIREBASE_WEB_API_KEY`, and returns `501` when unset. Production clients should not use this.
- `POST /api/v1/auth/token` — OAuth2 form variant for the Swagger UI auth modal.

### Role-Based Access Control

Enforced by dependencies in `app/api/v1/dependencies.py`:

- `require_admin` — `ADMIN` only
- `require_teacher` — `TEACHER` and `ADMIN`
- `require_student` — `STUDENT` and `ADMIN`
- `require_any_authenticated` — any authenticated role

Roles are read from the Firestore profile regardless of authentication provider; Firebase custom claims mirror them for frontend convenience only.

---

## Firestore Usage

Collections exposed as top-level services:

`users`, `class_rooms`, `subjects`, `teacher_subject_class_mappings`, `student_enrollments`, `attendance_records`, `topics_covered`, `live_meetings`, `study_materials`, `exam_grades`, `drive_folders`

### `FirestoreService`

- `add_document(doc_id, data)` — upsert with merge semantics; invalidates the cache entry
- `get_document(doc_id)` — fetch by ID, served from cache for reference collections
- `get_documents(ids)` — **fetch many in one round trip**; the core performance primitive
- `get_document_by_field(field, value)` — resolve one document by field equality
- `query_documents(field, op, value)` — query using `FieldFilter`
- `list_all()` — list every document in a collection
- `delete_document(doc_id)` — remove a document; invalidates the cache entry
- `get_next_numeric_id()` — generate an ID with no Firestore access

Services constructed with `cacheable=True` (users, classes, subjects) read through a TTL
cache. Transactional collections are not cached.

### Shared helpers

- `require_document(service, doc_id, label)` — fetch or raise `404` naming the resource
- `assert_owner(record, current_user, label)` — raise `403` unless the user owns the record; admins bypass
- `delete_with_dependencies(service, doc_id, label, dependencies, force)` — refuse with `409` while references remain, or cascade when `force=True`; checks run concurrently
- `prefetch_references(records, *specs)` — batch-load every document a result set will hydrate
- `prefetch_academic(records)` — the same, for the student/teacher/class/subject fields shared by all academic records

**Every list endpoint calls a prefetch helper before hydrating.** Omitting it silently
reintroduces the N+1 pattern and the endpoint gets slow again.

Hydration helpers embed related entities (teacher, class, subject) into response objects.

---

## API Endpoints

New endpoints added in this release are marked **NEW**.

### Authentication

| Method | Path | Description |
| --- | --- | --- |
| `POST` | `/api/v1/auth/register` | Register a user (`full_name`, `email`, `password?`, `role`) |
| `POST` | `/api/v1/auth/login` | Dev-only password login; returns a Firebase ID token |
| `POST` | `/api/v1/auth/token` | OAuth2 form login for Swagger |
| `GET` | `/api/v1/auth/me` | Current user profile |

### Admin Module

| Method | Path | Description |
| --- | --- | --- |
| `POST` | `/api/v1/admin/users` | Create a user (also creates the Firebase Auth account) |
| `GET` | `/api/v1/admin/users` | List users, optional `?role=` filter |
| `PUT` | `/api/v1/admin/users/{user_id}` | Update profile; syncs email/name/disabled state to Firebase |
| `DELETE` | `/api/v1/admin/users/{user_id}` | Deactivate, disable the Firebase account, revoke sessions. **NEW** — `?permanent=true` erases the account (`&force=true` cascades) |
| `POST` | `/api/v1/admin/users/{user_id}/reactivate` | **NEW** — re-enable a deactivated user |
| `POST` | `/api/v1/admin/classes` | Create a class |
| `GET` | `/api/v1/admin/classes` | List classes |
| `PUT` | `/api/v1/admin/classes/{class_id}` | **NEW** — update name, code, or description |
| `DELETE` | `/api/v1/admin/classes/{class_id}` | **NEW** — delete; `409` if referenced, `?force=true` cascades |
| `POST` | `/api/v1/admin/subjects` | Create a subject |
| `GET` | `/api/v1/admin/subjects` | List subjects |
| `PUT` | `/api/v1/admin/subjects/{subject_id}` | **NEW** — update name, code, or description |
| `DELETE` | `/api/v1/admin/subjects/{subject_id}` | **NEW** — delete; `409` if referenced, `?force=true` cascades |
| `POST` | `/api/v1/admin/mappings/teacher-subject-class` | Assign a teacher to a subject and class |
| `GET` | `/api/v1/admin/mappings/teacher-subject-class` | List mappings |
| `DELETE` | `/api/v1/admin/mappings/teacher-subject-class/{mapping_id}` | **NEW** — remove an assignment |
| `POST` | `/api/v1/admin/enrollments` | Enroll a student |
| `GET` | `/api/v1/admin/enrollments` | List enrollments |
| `DELETE` | `/api/v1/admin/enrollments/{enrollment_id}` | **NEW** — un-enroll; history preserved |
| `POST` | `/api/v1/admin/materials` | **NEW** — upload a study material, optionally on behalf of a `teacher_id` |
| `GET` | `/api/v1/admin/materials` | **NEW** — every material system-wide; filter by `class_id`, `subject_id`, `teacher_id` |
| `PUT` | `/api/v1/admin/materials/{material_id}` | **NEW** — rename any material or change its type |
| `DELETE` | `/api/v1/admin/materials/{material_id}` | **NEW** — delete any material; `?keep_file=true` leaves the stored file |
| `POST` | `/api/v1/admin/meetings` | **NEW** — schedule a live session, optionally on behalf of a `teacher_id`; generates the Meet link |
| `GET` | `/api/v1/admin/meetings` | **NEW** — every meeting system-wide; filter by `class_id`, `subject_id`, `teacher_id` |
| `PUT` | `/api/v1/admin/meetings/{meeting_id}` | **NEW** — reschedule any meeting; propagates to the Calendar event |
| `POST` | `/api/v1/admin/meetings/{meeting_id}/regenerate-link` | **NEW** — retry Meet generation for a meeting saved without a link |
| `DELETE` | `/api/v1/admin/meetings/{meeting_id}` | **NEW** — cancel any meeting and delete its Calendar event |
| `POST` | `/api/v1/admin/timetable` | **NEW** — add a recurring period; `409` on a clash, `?allow_conflicts=true` overrides |
| `POST` | `/api/v1/admin/timetable/bulk` | **NEW** — upload a week or term at once; `replace_existing` makes re-uploads idempotent |
| `GET` | `/api/v1/admin/timetable` | **NEW** — filter by `class_id`, `teacher_id`, `subject_id`, `day_of_week` |
| `PUT` | `/api/v1/admin/timetable/{entry_id}` | **NEW** — move, reassign, or deactivate a period |
| `DELETE` | `/api/v1/admin/timetable/{entry_id}` | **NEW** — remove a period |
| `GET` | `/api/v1/admin/timetable/class/{class_id}/day` | **NEW** — a class's periods resolved against a date |
| `GET` | `/api/v1/admin/reminders/status` | **NEW** — scheduler health, offsets, last sweep, server local time |
| `GET` | `/api/v1/admin/reminders/preview` | **NEW** — what the next sweep would send, sending nothing |
| `POST` | `/api/v1/admin/reminders/run` | **NEW** — trigger a sweep now; returns a job id |
| `GET` | `/api/v1/admin/reminders/log` | **NEW** — reminders actually delivered, newest first |
| `GET` | `/api/v1/admin/integrations` | **NEW** — live health of Drive, Cloud Storage, Meet, and SMTP, with the exact misconfiguration named |
| `POST` | `/api/v1/admin/integrations/storage/test-upload` | **NEW** — upload a probe file, report where it landed, delete it again |
| `GET` | `/api/v1/admin/reports/monitoring` | Overall stats, teacher activity, student performance. Cached; `?refresh=true` recomputes |
| `POST` | `/api/v1/admin/reports/monitoring/refresh` | **NEW** — rebuild in the background, returns `202` with a job id |
| `GET` | `/api/v1/admin/jobs/{job_id}` | **NEW** — job status, percent, and message for a progress bar |
| `GET` | `/api/v1/admin/jobs` | **NEW** — recent jobs, for a notifications panel |

### Teacher Module

All update and delete routes enforce ownership: teachers may only modify their own records; admins may modify any.

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/api/v1/teachers/my-classes` | Assigned classes and subjects |
| `GET` | `/api/v1/teachers/classes/{class_id}/students` | Enrolled students |
| `POST` | `/api/v1/teachers/attendance` | Batch-mark attendance |
| `GET` | `/api/v1/teachers/attendance` | Attendance logged by this teacher |
| `PUT` | `/api/v1/teachers/attendance/{record_id}` | **NEW** — correct status, remarks, or date |
| `DELETE` | `/api/v1/teachers/attendance/{record_id}` | **NEW** — delete a record |
| `POST` | `/api/v1/teachers/topics` | Log a covered topic |
| `GET` | `/api/v1/teachers/topics` | Topics logged by this teacher |
| `PUT` | `/api/v1/teachers/topics/{topic_id}` | **NEW** — edit a topic |
| `DELETE` | `/api/v1/teachers/topics/{topic_id}` | **NEW** — remove a topic |
| `POST` | `/api/v1/teachers/meetings` | Schedule a session; auto-creates a Meet link |
| `GET` | `/api/v1/teachers/meetings` | Meetings created by this teacher |
| `PUT` | `/api/v1/teachers/meetings/{meeting_id}` | **NEW** — reschedule; propagates to Calendar |
| `DELETE` | `/api/v1/teachers/meetings/{meeting_id}` | **NEW** — cancel; deletes the Calendar event |
| `POST` | `/api/v1/teachers/materials` | Upload study material (multipart) |
| `GET` | `/api/v1/teachers/materials` | Materials uploaded by this teacher |
| `PUT` | `/api/v1/teachers/materials/{material_id}` | **NEW** — rename or change type |
| `DELETE` | `/api/v1/teachers/materials/{material_id}` | **NEW** — delete record and stored file; `?keep_file=true` retains the file |
| `POST` | `/api/v1/teachers/grades` | Enter an exam grade |
| `GET` | `/api/v1/teachers/grades` | Grades entered by this teacher |
| `PUT` | `/api/v1/teachers/grades/{grade_id}` | **NEW** — correct marks or remarks |
| `DELETE` | `/api/v1/teachers/grades/{grade_id}` | **NEW** — delete a grade |

### Student Module

All accept an optional `?subject_id=` filter.

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/api/v1/students/my-classes` | Enrolled class, subjects, and teachers |
| `GET` | `/api/v1/students/attendance` | Personal attendance history |
| `GET` | `/api/v1/students/topics` | Topics covered for the student's class |
| `GET` | `/api/v1/students/meetings` | Meeting links and recordings |
| `GET` | `/api/v1/students/materials` | Study materials |
| `GET` | `/api/v1/students/grades` | Exam grades and remarks |

### Storage

| Method | Path | Description |
| --- | --- | --- |
| `POST` | `/api/v1/storage/upload` | Direct upload to the configured backend. Teacher or admin. Returns `access_url` and `storage_provider` |

---

## Scheduling a Meeting

`POST /api/v1/teachers/meetings` accepts:

| Field | Default | Meaning |
| --- | --- | --- |
| `class_id`, `subject_id`, `title`, `scheduled_time` | — | Required |
| `auto_create_meet` | `true` | Generate a Calendar event with a Meet link |
| `duration_minutes` | `60` | Event length |
| `invite_students` | `true` | Add enrolled students as attendees |
| `meeting_link` | `null` | Supply manually to skip generation |
| `recording_url`, `status` | — | Optional metadata |

The response adds `google_event_id` and `google_calendar_id` when a Calendar event backs the meeting.

**Clients must handle a null `meeting_link` on a `201` response.** Meet generation is best-effort by design, so the meeting is always persisted even when the link cannot be created.

---

## Error Semantics

| Status | Meaning |
| --- | --- |
| `400` | Empty update body, or invalid values (e.g. `marks_obtained` above `max_marks`, `max_marks` ≤ 0) |
| `401` | Missing, malformed, expired, or revoked token |
| `403` | Wrong role, ownership violation, inactive account, or authenticated with Google but no LMS profile exists |
| `404` | Resource not found |
| `409` | Delete blocked by references. `detail` names each blocking resource and count; retry with `?force=true` to cascade |
| `501` | `/auth/login` called while `FIREBASE_WEB_API_KEY` is unset |

Example `409` body:

```json
{
  "detail": "Cannot delete ClassRoom because it is still referenced by: 1 student enrollment(s), 1 teacher mapping(s). Remove them first, or retry with ?force=true to cascade."
}
```

---

## Schema Changes

| Schema | Change |
| --- | --- |
| `UserCreate` | `password` is now optional |
| `UserOut` | Added `firebase_uid` |
| `LiveMeetingCreate` | Added `auto_create_meet`, `duration_minutes`, `invite_students` |
| `LiveMeetingOut` | Added `google_event_id`, `google_calendar_id` |
| `StudyMaterialOut` | Added `storage_provider` |
| New partial-update models | `ClassRoomUpdate`, `SubjectUpdate`, `AttendanceUpdate`, `TopicUpdate`, `LiveMeetingUpdate`, `StudyMaterialUpdate`, `GradeEntryUpdate` |

All update models are partial: omitted fields are left unchanged, and an entirely empty body is rejected with `400`.

---

## Startup and Local Development

1. Install dependencies with the same interpreter that will run the app:

   ```bash
   python -m pip install -r requirements.txt
   ```

2. Provide Firebase credentials via `firebase_credentials.json` or `GOOGLE_APPLICATION_CREDENTIALS`. The repository does not include service account credentials.

3. Copy `.env.example` to `.env` and fill in your values.

4. Start the server:

   ```bash
   python -m uvicorn app.main:app --reload --port 8000
   ```

5. Visit the docs:
   - Swagger UI: `http://127.0.0.1:8000/docs`
   - ReDoc: `http://127.0.0.1:8000/redoc`

---

## Migrating Existing Users to Firebase Auth

Run once after deploying this release. It finds or creates a Firebase Auth account for every user document lacking a `firebase_uid`, writes the role custom claims, links the uid back, and clears the legacy `hashed_password`.

```bash
python -m scripts.migrate_users_to_firebase_auth --dry-run     # report only
python -m scripts.migrate_users_to_firebase_auth               # apply
python -m scripts.migrate_users_to_firebase_auth --keep-hashes # apply, retain legacy hashes
```

Migrated accounts have **no password** in Firebase. Users either sign in with Google Workspace or complete a Firebase password-reset email.

To preserve existing passwords instead, import the bcrypt hashes with the Firebase CLI *before* running the script — it will then link the imported accounts rather than create new ones:

```bash
firebase auth:import users.json --hash-algo=BCRYPT --project <YOUR_PROJECT_ID>
```

Documents whose `email` field is not a valid address will fail; the script reports them and continues.

---

## Verification

`verify_lms.py` exercises the API end to end: health check, authentication, admin and teacher workflows, student views, monitoring reports, update endpoints, `409` dependency guards, ownership enforcement, deletions, soft delete and reactivation, and `404` handling.

```bash
python verify_lms.py
```

It authenticates over the dev password endpoint, so it requires `FIREBASE_WEB_API_KEY` and users that exist in Firebase Auth. Run the migration first.

---

## Seed Data

`app/db/init_db.py` seeds demo data at startup when absent. Credentials are created in Firebase Auth, not stored locally:

- Admin: `admin@lms.com` / `admin123`
- Teacher: `teacher.math@lms.com` / `teacher123`
- Students: `student.alice@lms.com`, `student.bob@lms.com` / `student123`
- Class `CLASS-10A`, subject `MATH101`, plus the matching assignment and enrollments

---

## Notes and Limitations

- Persistence is Firebase-only; no SQLAlchemy.
- Document IDs are millisecond timestamps generated in-process. Collisions are avoided within a single process, but two server workers creating records in the same millisecond could still collide. Firestore auto-IDs or a counter document would remove the risk entirely.
- The reference cache is per-process. With multiple workers each holds its own copy, so a write on one worker is invisible to another's cache for up to `REFERENCE_CACHE_TTL_SECONDS`. Lower the TTL or move to a shared cache if that matters.
- Background job state is in-memory: it does not survive restarts, and polling across multiple workers can miss a job. Every job is a recomputation of derived data, so losing one is safe.
- Every Google integration fails soft. Absent credentials or unauthorized delegation degrade the feature and log a warning rather than raising.
- Google Meet and Drive require Google Workspace. Domain-wide delegation cannot be configured for personal Gmail accounts.
- A service account has no Drive storage quota, so Drive uploads must target a Shared Drive.
- `USE_LOCAL_STORAGE=True` silently overrides `STORAGE_PROVIDER`. Set it to `False` to use the configured provider.
- Emails are validated as plain strings and do not depend on `email-validator`.
- `app/core/security.py` and the `python-jose` dependency exist only for legacy token validation and can be removed once `ALLOW_LEGACY_JWT_LOGIN` is permanently `False`.

---

## Future Improvements

- **Deploy the backend in the same region as the Firestore database.** This is the single
  largest remaining performance win: the ~850 ms per-call latency is pure network distance,
  and it sets the floor for every endpoint no matter how few calls the code makes.
- Replace timestamp ID generation with Firestore auto-generated IDs or a counter document.
- Move the reference cache and job registry to a shared store (Redis, Firestore) so they
  work correctly across multiple server workers.
- Auto-populate `recording_url` from the Google Meet REST API (`conferenceRecords.recordings.list`), which requires `meet.googleapis.com`, the `meetings.space.created` scope, and a Workspace tier that includes Meet recording.
- Add comprehensive unit and integration tests.
- Add request logging, error-handling middleware, and rate limiting.
- Tighten CORS beyond the current `allow_origins=["*"]` before production.

---

## Contact

This backend is a foundation for an LMS, using FastAPI as the web framework, Firebase for identity and storage, and Google Workspace for conferencing and documents.

For issues or extensions, inspect the routers under `app/api/v1/`, the Firestore helpers in `app/core/firebase.py`, and the Google integrations in `app/core/firebase_auth.py`, `app/core/google_meet.py`, and `app/core/google_drive.py`.
