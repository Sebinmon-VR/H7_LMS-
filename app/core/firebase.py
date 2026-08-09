import os
import logging
import threading
import time
import types
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from enum import Enum
from typing import Any, Type, TypeVar, get_args, get_origin

from app.core.config import settings
from app.core.enums import UserRole

logger = logging.getLogger("firebase_db")

db_firestore = None
firebase_initialized = False

T = TypeVar("T")

# Sentinel distinguishing "not cached" from "cached as None" (a known-missing document).
_MISS = object()


class _DocumentCache:
    """
    Process-wide TTL cache for documents.

    Every Firestore round trip costs hundreds of milliseconds from outside the database's
    region, so the dominant cost of a request is the number of calls it makes, not the
    volume of data. Reference collections (users, classes, subjects) are small, change
    rarely, and are read repeatedly while hydrating list responses - caching them removes
    the majority of those calls.

    Writes invalidate the affected key immediately, so a caller never reads back its own
    stale write.
    """

    def __init__(self) -> None:
        self._store: dict[tuple[str, str], tuple[float, dict | None]] = {}
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def get(self, collection: str, doc_id: str, ttl: float) -> Any:
        key = (collection, str(doc_id))
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self.misses += 1
                return _MISS

            cached_at, value = entry
            if time.monotonic() - cached_at > ttl:
                del self._store[key]
                self.misses += 1
                return _MISS

            self.hits += 1
            return value

    def put(self, collection: str, doc_id: str, value: dict | None) -> None:
        with self._lock:
            self._store[(collection, str(doc_id))] = (time.monotonic(), value)

    def invalidate(self, collection: str, doc_id: str | None = None) -> None:
        with self._lock:
            if doc_id is not None:
                self._store.pop((collection, str(doc_id)), None)
            else:
                for key in [k for k in self._store if k[0] == collection]:
                    del self._store[key]

    def clear(self) -> None:
        with self._lock:
            self._store.clear()
            self.hits = 0
            self.misses = 0

    def stats(self) -> dict:
        with self._lock:
            total = self.hits + self.misses
            return {
                "entries": len(self._store),
                "hits": self.hits,
                "misses": self.misses,
                "hit_rate": round(self.hits / total, 3) if total else 0.0,
            }


document_cache = _DocumentCache()


def serialize_model(instance: Any) -> dict[str, Any]:
    if is_dataclass(instance):
        raw = asdict(instance)
    else:
        raw = dict(instance)

    payload: dict[str, Any] = {}
    for key, value in raw.items():
        if value is None:
            payload[key] = None
        elif isinstance(value, datetime):
            payload[key] = value.isoformat()
        elif isinstance(value, date):
            payload[key] = value.isoformat()
        elif isinstance(value, Enum):
            payload[key] = value.value
        else:
            payload[key] = value
    return payload


def hydrate_model(model_cls: Type[T], data: dict[str, Any] | None) -> T | None:
    if not data:
        return None

    kwargs: dict[str, Any] = {}
    for field_name, field in model_cls.__dataclass_fields__.items():
        if field_name not in data:
            continue
        kwargs[field_name] = _coerce_value(data[field_name], field.type)

    return model_cls(**kwargs)


def _coerce_value(value: Any, annotation: Any) -> Any:
    if value is None:
        return None

    origin = get_origin(annotation)
    if origin is not None:
        if origin in (types.UnionType, getattr(types, "UnionType", None)):
            args = [arg for arg in get_args(annotation) if arg is not type(None)]
            if args:
                return _coerce_value(value, args[0])
        if origin in (list, tuple, set):
            return value

    if isinstance(value, Enum):
        return value

    if annotation is datetime:
        if isinstance(value, datetime):
            return value
        return datetime.fromisoformat(str(value))

    if annotation is date:
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        return date.fromisoformat(str(value))

    if isinstance(annotation, type) and issubclass(annotation, Enum):
        if isinstance(value, annotation):
            return value
        return annotation(value)

    if annotation is bool:
        return bool(value)

    if annotation is int:
        return int(value)

    if annotation is float:
        return float(value)

    return value


def initialize_firebase():
    """
    Initializes Firebase Admin SDK using service account JSON credentials certificate or Application Default Credentials.
    Provides Cloud Firestore DB client (`db_firestore`).
    """
    global db_firestore, firebase_initialized

    if firebase_initialized:
        return db_firestore

    try:
        import firebase_admin
        from firebase_admin import credentials, firestore

        cred_path = settings.FIREBASE_CREDENTIALS_PATH
        gcp_cred_path = settings.GOOGLE_APPLICATION_CREDENTIALS

        if os.path.exists(cred_path):
            cred = credentials.Certificate(cred_path)
            firebase_admin.initialize_app(cred)
            logger.info(f"Firebase Admin SDK initialized using certificate: {cred_path}")
        elif os.path.exists(gcp_cred_path):
            cred = credentials.Certificate(gcp_cred_path)
            firebase_admin.initialize_app(cred)
            logger.info(f"Firebase Admin SDK initialized using GCP credentials: {gcp_cred_path}")
        else:
            firebase_admin.initialize_app(options={"projectId": settings.GCP_PROJECT_ID})
            logger.info(f"Firebase Admin SDK initialized using Default Credentials for project: {settings.GCP_PROJECT_ID}")

        db_firestore = firestore.client()
        firebase_initialized = True
        return db_firestore

    except ModuleNotFoundError as e:
        logger.warning(
            "Firebase Admin SDK cannot be imported: %s. Install the 'firebase-admin' package and ensure the application is running in the correct Python environment.",
            e,
        )
        firebase_initialized = False
        return None
    except Exception as e:
        logger.warning(f"Could not initialize Firebase Admin SDK: {e}. Ensure credentials are available and configured correctly.")
        firebase_initialized = False
        return None


class FirestoreService:
    """
    Firestore repository helper for CRUD operations on a specific Firestore collection.
    """

    def __init__(self, collection_name: str, cacheable: bool = False):
        self.collection_name = collection_name
        # Only reference collections are cached. Transactional records (attendance, grades)
        # must always read through so a client never sees its own write go missing.
        self.cacheable = cacheable
        self._db = initialize_firebase()

    @property
    def is_available(self) -> bool:
        return self._db is not None

    @property
    def _ttl(self) -> float:
        return settings.REFERENCE_CACHE_TTL_SECONDS

    def add_document(self, doc_id: str, data: dict) -> dict:
        """
        Creates or updates a Firestore document using merge semantics.
        """
        if self.cacheable:
            document_cache.invalidate(self.collection_name, doc_id)

        if not self.is_available:
            logger.warning(f"Firestore unavailable. Mocking add to collection '{self.collection_name}'.")
            normalized = self._normalize_document(dict(data))
            normalized["id"] = doc_id
            return normalized

        doc_ref = self._db.collection(self.collection_name).document(str(doc_id))
        doc_ref.set(data, merge=True)
        normalized = self._normalize_document(dict(data))
        normalized["id"] = doc_id
        return normalized

    def create_document(self, doc_id: str, data: dict) -> bool:
        """
        Creates a document only if that ID is free. Returns False when it already exists.

        Unlike `add_document`, which merges, this maps onto Firestore's `create` and is
        therefore atomic. That is what makes it usable as a claim: two server workers racing
        to send the same class reminder both attempt the create, exactly one wins, and the
        loser skips instead of sending a duplicate email.
        """
        if self.cacheable:
            document_cache.invalidate(self.collection_name, doc_id)

        if not self.is_available:
            logger.warning(
                "Firestore unavailable. Cannot claim '%s/%s'.", self.collection_name, doc_id
            )
            return False

        try:
            self._db.collection(self.collection_name).document(str(doc_id)).create(data)
            return True
        except Exception as exc:
            # AlreadyExists is the expected, uninteresting case; anything else is worth a log.
            if type(exc).__name__ == "AlreadyExists":
                return False
            logger.warning(
                "Could not create '%s/%s': %s", self.collection_name, doc_id, exc
            )
            return False

    def get_document(self, doc_id: str) -> dict | None:
        """Fetches a single document by ID, served from cache for reference collections."""
        if not self.is_available:
            return None

        if self.cacheable:
            cached = document_cache.get(self.collection_name, doc_id, self._ttl)
            if cached is not _MISS:
                return cached

        doc_ref = self._db.collection(self.collection_name).document(str(doc_id))
        doc = doc_ref.get()

        result = None
        if doc.exists:
            data = doc.to_dict()
            data["id"] = doc.id
            result = self._normalize_document(data)

        if self.cacheable:
            document_cache.put(self.collection_name, doc_id, result)
        return result

    def get_documents(self, doc_ids: list[str | int]) -> dict[str, dict]:
        """
        Fetches many documents in a single round trip via Firestore's batched get.

        This is the core performance primitive: resolving 8 documents one at a time costs
        roughly 850 ms each, while one batched call resolves all 8 in about 50 ms each.
        Cached entries are served locally and only the remainder is requested.

        Returns a mapping of string document ID to document.
        """
        resolved: dict[str, dict] = {}
        wanted = {str(d) for d in doc_ids if d is not None}
        if not wanted or not self.is_available:
            return resolved

        outstanding = set(wanted)

        if self.cacheable:
            for doc_id in list(outstanding):
                cached = document_cache.get(self.collection_name, doc_id, self._ttl)
                if cached is not _MISS:
                    outstanding.discard(doc_id)
                    if cached is not None:
                        resolved[doc_id] = cached

        if not outstanding:
            return resolved

        collection = self._db.collection(self.collection_name)
        refs = [collection.document(doc_id) for doc_id in outstanding]

        try:
            snapshots = list(self._db.get_all(refs))
        except Exception as exc:
            # Fall back to individual reads so a batch failure degrades speed, not behaviour.
            logger.warning("Batched read failed for '%s' (%s); falling back to single reads.",
                           self.collection_name, exc)
            for doc_id in outstanding:
                found = self.get_document(doc_id)
                if found:
                    resolved[doc_id] = found
            return resolved

        returned: set[str] = set()
        for snapshot in snapshots:
            returned.add(snapshot.id)
            if snapshot.exists:
                data = snapshot.to_dict()
                data["id"] = snapshot.id
                document = self._normalize_document(data)
                resolved[snapshot.id] = document
                if self.cacheable:
                    document_cache.put(self.collection_name, snapshot.id, document)
            elif self.cacheable:
                document_cache.put(self.collection_name, snapshot.id, None)

        # Cache negatives for ids Firestore did not return at all, so repeated hydration of
        # a dangling reference does not re-query every time.
        if self.cacheable:
            for missing in outstanding - returned:
                document_cache.put(self.collection_name, missing, None)

        return resolved

    def get_document_by_field(self, field: str, value: Any) -> dict | None:
        """Fetches the first matching document for a field equality query."""
        matches = self.query_documents(field, "==", value)
        return matches[0] if matches else None

    def get_next_numeric_id(self) -> int:
        """
        Returns a new numeric document ID.

        This previously streamed the entire collection on every create to find the maximum
        ID, costing a full round trip (~880 ms) and growing linearly with collection size.
        IDs are now generated from a monotonic clock with a per-process counter, which
        needs no Firestore access at all and stays consistent with the millisecond-timestamp
        IDs already in the database.
        """
        return generate_id()

    def query_documents(self, field: str, op: str, value: Any) -> list[dict]:
        """Queries Firestore collection matching field conditions."""
        if not self.is_available:
            return []

        collection = self._db.collection(self.collection_name)
        try:
            from google.cloud.firestore_v1.base_query import FieldFilter
            query = collection.where(filter=FieldFilter(field, op, value))
        except ImportError:
            # Older client libraries only support the positional form.
            query = collection.where(field, op, value)

        docs = query.stream()
        results = []
        for doc in docs:
            d = doc.to_dict()
            d["id"] = doc.id
            results.append(self._normalize_document(d))
        return results

    def list_all(self) -> list[dict]:
        """Lists all documents in a Firestore collection."""
        if not self.is_available:
            return []

        docs = self._db.collection(self.collection_name).stream()
        results = []
        for doc in docs:
            d = doc.to_dict()
            d["id"] = doc.id
            results.append(self._normalize_document(d))
        return results

    def delete_document(self, doc_id: str) -> bool:
        """Deletes a document from Firestore."""
        if self.cacheable:
            document_cache.invalidate(self.collection_name, doc_id)

        if not self.is_available:
            return False

        self._db.collection(self.collection_name).document(str(doc_id)).delete()
        return True

    def _normalize_document(self, data: dict) -> dict:
        if data is None:
            return data

        normalized = dict(data)
        if "id" in normalized and isinstance(normalized["id"], str) and normalized["id"].isdigit():
            normalized["id"] = int(normalized["id"])
        return normalized


_id_lock = threading.Lock()
_last_issued_id = 0


def generate_id() -> int:
    """
    Generates a millisecond-timestamp ID without touching Firestore.

    Two creates inside the same millisecond would otherwise collide, so the counter never
    returns a value it has already issued in this process: it advances by one instead.
    IDs stay sortable by creation time and match the format already stored.
    """
    global _last_issued_id

    with _id_lock:
        candidate = int(datetime.utcnow().timestamp() * 1000)
        if candidate <= _last_issued_id:
            candidate = _last_issued_id + 1
        _last_issued_id = candidate
        return candidate


def prefetch_references(records: list[dict], *specs: tuple[str, "FirestoreService"]) -> None:
    """
    Warms the cache for every document a set of records is about to hydrate.

    Hydration resolves references one at a time, so rendering N records costs N x (number
    of reference fields) round trips - the classic N+1 problem, and the reason a four-row
    enrollment list took over seven seconds. Calling this first collapses all of those
    lookups into one batched read per collection; the subsequent per-record hydration then
    hits the cache and performs no I/O.

    Each spec is a (field_name, service) pair, e.g. ("teacher_id", firestore_users).
    """
    if not records:
        return

    by_service: dict[int, tuple[FirestoreService, set]] = {}
    for field_name, service in specs:
        _, ids = by_service.setdefault(id(service), (service, set()))
        for record in records:
            value = record.get(field_name)
            if value is not None:
                ids.add(value)

    # One batched read per collection, and the collections are independent of each other,
    # so issue them concurrently: three sequential batches cost three network waits, in
    # parallel they cost one.
    pending = {
        service.collection_name: (service, list(ids))
        for service, ids in by_service.values()
        if ids
    }
    if not pending:
        return

    if len(pending) == 1:
        service, ids = next(iter(pending.values()))
        service.get_documents(ids)
        return

    from app.core.concurrency import run_parallel

    run_parallel({
        name: (lambda s=service, i=ids: s.get_documents(i))
        for name, (service, ids) in pending.items()
    })


def prefetch_academic(records: list[dict]) -> None:
    """
    Warms the cache for the reference fields every academic record shares.

    Attendance, topics, meetings, materials, and grades all hydrate the same trio of
    student/teacher, class, and subject, so one helper covers every list endpoint.
    """
    prefetch_references(
        records,
        ("student_id", firestore_users),
        ("teacher_id", firestore_users),
        ("class_id", firestore_classes),
        ("subject_id", firestore_subjects),
    )


def _resolve_document(collection_service: FirestoreService, key: str | int) -> dict | None:
    return collection_service.get_document(str(key)) if key is not None else None


def require_document(collection_service: FirestoreService, doc_id: str | int, label: str) -> dict:
    """
    Fetches a document or raises a 404 naming the resource.
    Replaces the repeated get-then-check-then-raise blocks across the routers.
    """
    from fastapi import HTTPException

    document = collection_service.get_document(str(doc_id))
    if not document:
        raise HTTPException(status_code=404, detail=f"{label} not found")
    return document


def assert_owner(record: dict, current_user: Any, label: str = "record") -> None:
    """
    Ensures the acting user owns the record. Admins bypass the check.
    Raises 403 when a teacher targets another teacher's record.
    """
    from fastapi import HTTPException

    if getattr(current_user, "role", None) == UserRole.ADMIN:
        return

    if record.get("teacher_id") != current_user.id:
        raise HTTPException(
            status_code=403,
            detail=f"You can only modify your own {label}",
        )


def delete_with_dependencies(
    collection_service: FirestoreService,
    doc_id: str | int,
    label: str,
    dependencies: list[tuple[str, FirestoreService, str]],
    force: bool = False,
) -> None:
    """
    Deletes a document after checking that nothing still references it.

    `dependencies` is a list of (human_label, referencing_service, field_name) tuples.
    Without `force`, a blocking reference raises 409 naming the resource and count.
    With `force`, the referencing documents are deleted first (cascade).
    """
    from fastapi import HTTPException

    from app.core.concurrency import run_parallel

    require_document(collection_service, doc_id, label)

    key = _as_key(doc_id)

    # The dependency checks are independent of one another, so run them concurrently.
    # Sequentially, seven checks meant seven network waits and a delete that took ~6s.
    found = run_parallel({
        dep_label: (lambda s=dep_service, f=dep_field: s.query_documents(f, "==", key))
        for dep_label, dep_service, dep_field in dependencies
    })

    services_by_label = {dep_label: dep_service for dep_label, dep_service, _ in dependencies}

    blocking: list[str] = []
    cascade: list[tuple[FirestoreService, dict]] = []

    for dep_label, _, _ in dependencies:
        referencing = found.get(dep_label) or []
        if not referencing:
            continue
        if force:
            cascade.extend((services_by_label[dep_label], doc) for doc in referencing)
        else:
            blocking.append(f"{len(referencing)} {dep_label}")

    if blocking:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cannot delete {label} because it is still referenced by: {', '.join(blocking)}. "
                "Remove them first, or retry with ?force=true to cascade."
            ),
        )

    if cascade:
        run_parallel({
            f"{dep_service.collection_name}:{doc['id']}":
                (lambda s=dep_service, d=doc: s.delete_document(str(d["id"])))
            for dep_service, doc in cascade
        })

    collection_service.delete_document(str(doc_id))


def _as_key(doc_id: str | int) -> Any:
    """
    Firestore equality queries are type-sensitive. Reference fields are stored as integers,
    so a string path parameter must be coerced before querying.
    """
    if isinstance(doc_id, str) and doc_id.isdigit():
        return int(doc_id)
    return doc_id


def hydrate_teacher_mapping(mapping: dict) -> dict:
    return {
        "id": mapping["id"],
        "teacher": _resolve_document(firestore_users, mapping.get("teacher_id")),
        "subject": _resolve_document(firestore_subjects, mapping.get("subject_id")),
        "class_room": _resolve_document(firestore_classes, mapping.get("class_id")),
    }


def hydrate_student_enrollment(enrollment: dict) -> dict:
    return {
        "id": enrollment["id"],
        "student": _resolve_document(firestore_users, enrollment.get("student_id")),
        "class_room": _resolve_document(firestore_classes, enrollment.get("class_id")),
    }


def hydrate_attendance(record: dict) -> dict:
    hydrated = {
        "id": record["id"],
        "student_id": record.get("student_id"),
        "student": _resolve_document(firestore_users, record.get("student_id")),
        "class_id": record.get("class_id"),
        "class_room": _resolve_document(firestore_classes, record.get("class_id")),
        "subject_id": record.get("subject_id"),
        "subject": _resolve_document(firestore_subjects, record.get("subject_id")),
        "teacher_id": record.get("teacher_id"),
        "date": record.get("date"),
        "status": record.get("status"),
        "remarks": record.get("remarks"),
        "created_at": record.get("created_at"),
    }
    return hydrated


def hydrate_topic(topic: dict) -> dict:
    return {
        "id": topic["id"],
        "class_id": topic.get("class_id"),
        "class_room": _resolve_document(firestore_classes, topic.get("class_id")),
        "subject_id": topic.get("subject_id"),
        "subject": _resolve_document(firestore_subjects, topic.get("subject_id")),
        "teacher_id": topic.get("teacher_id"),
        "teacher": _resolve_document(firestore_users, topic.get("teacher_id")),
        "topic_title": topic.get("topic_title"),
        "description": topic.get("description"),
        "date_covered": topic.get("date_covered"),
        "completion_percentage": topic.get("completion_percentage"),
        "created_at": topic.get("created_at"),
    }


def hydrate_live_meeting(meeting: dict) -> dict:
    return {
        "id": meeting["id"],
        "class_id": meeting.get("class_id"),
        "class_room": _resolve_document(firestore_classes, meeting.get("class_id")),
        "subject_id": meeting.get("subject_id"),
        "subject": _resolve_document(firestore_subjects, meeting.get("subject_id")),
        "teacher_id": meeting.get("teacher_id"),
        "teacher": _resolve_document(firestore_users, meeting.get("teacher_id")),
        "title": meeting.get("title"),
        "meeting_link": meeting.get("meeting_link"),
        "recording_url": meeting.get("recording_url"),
        "scheduled_time": meeting.get("scheduled_time"),
        "status": meeting.get("status"),
        "created_at": meeting.get("created_at"),
        "google_event_id": meeting.get("google_event_id"),
        "google_calendar_id": meeting.get("google_calendar_id"),
        "meet_status": meeting.get("meet_status"),
        "meet_error": meeting.get("meet_error"),
    }


def hydrate_study_material(material: dict) -> dict:
    return {
        "id": material["id"],
        "class_id": material.get("class_id"),
        "class_room": _resolve_document(firestore_classes, material.get("class_id")),
        "subject_id": material.get("subject_id"),
        "subject": _resolve_document(firestore_subjects, material.get("subject_id")),
        "teacher_id": material.get("teacher_id"),
        "teacher": _resolve_document(firestore_users, material.get("teacher_id")),
        "title": material.get("title"),
        "material_type": material.get("material_type"),
        "file_url": material.get("file_url"),
        "uploaded_at": material.get("uploaded_at"),
        "storage_provider": material.get("storage_provider"),
        "storage_warning": material.get("storage_warning"),
    }


def hydrate_timetable_entry(entry: dict) -> dict:
    return {
        "id": entry["id"],
        "class_id": entry.get("class_id"),
        "class_room": _resolve_document(firestore_classes, entry.get("class_id")),
        "subject_id": entry.get("subject_id"),
        "subject": _resolve_document(firestore_subjects, entry.get("subject_id")),
        "teacher_id": entry.get("teacher_id"),
        "teacher": _resolve_document(firestore_users, entry.get("teacher_id")),
        "day_of_week": entry.get("day_of_week"),
        "start_time": entry.get("start_time"),
        "end_time": entry.get("end_time"),
        "room": entry.get("room"),
        "period_label": entry.get("period_label"),
        "effective_from": entry.get("effective_from"),
        "effective_to": entry.get("effective_to"),
        "is_active": entry.get("is_active", True),
        "created_at": entry.get("created_at"),
        "updated_at": entry.get("updated_at"),
    }


def hydrate_exam_grade(grade: dict) -> dict:
    return {
        "id": grade["id"],
        "student_id": grade.get("student_id"),
        "student": _resolve_document(firestore_users, grade.get("student_id")),
        "class_id": grade.get("class_id"),
        "class_room": _resolve_document(firestore_classes, grade.get("class_id")),
        "subject_id": grade.get("subject_id"),
        "subject": _resolve_document(firestore_subjects, grade.get("subject_id")),
        "teacher_id": grade.get("teacher_id"),
        "teacher": _resolve_document(firestore_users, grade.get("teacher_id")),
        "exam_name": grade.get("exam_name"),
        "marks_obtained": grade.get("marks_obtained"),
        "max_marks": grade.get("max_marks"),
        "remarks": grade.get("remarks"),
        "created_at": grade.get("created_at"),
    }


# Global Firestore Collection Helper Services.
# `cacheable=True` marks small, slow-changing reference data that hydration reads over and
# over; writes invalidate the affected key immediately.
firestore_users = FirestoreService("users", cacheable=True)
firestore_classes = FirestoreService("class_rooms", cacheable=True)
firestore_subjects = FirestoreService("subjects", cacheable=True)
firestore_teacher_mappings = FirestoreService("teacher_subject_class_mappings")
firestore_student_enrollments = FirestoreService("student_enrollments")
firestore_attendance = FirestoreService("attendance_records")
firestore_topics = FirestoreService("topics_covered")
firestore_meetings = FirestoreService("live_meetings")
firestore_materials = FirestoreService("study_materials")
firestore_grades = FirestoreService("exam_grades")
# Timetables change rarely and are read on every reminder sweep and every student's
# "today" view, so they earn the reference cache.
firestore_timetable = FirestoreService("timetable_entries", cacheable=True)
# One document per reminder actually sent, so a restart or an overlapping sweep cannot
# email the same student about the same period twice.
firestore_reminder_log = FirestoreService("reminder_log")
