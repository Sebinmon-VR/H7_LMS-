import os
import logging
import types
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from enum import Enum
from typing import Any, Type, TypeVar, get_args, get_origin

from app.core.config import settings

logger = logging.getLogger("firebase_db")

db_firestore = None
firebase_initialized = False

T = TypeVar("T")


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

    def __init__(self, collection_name: str):
        self.collection_name = collection_name
        self._db = initialize_firebase()

    @property
    def is_available(self) -> bool:
        return self._db is not None

    def add_document(self, doc_id: str, data: dict) -> dict:
        """
        Creates or updates a Firestore document using merge semantics.
        """
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

    def get_document(self, doc_id: str) -> dict | None:
        """Fetches a single document by ID from Firestore."""
        if not self.is_available:
            return None

        doc_ref = self._db.collection(self.collection_name).document(str(doc_id))
        doc = doc_ref.get()
        if doc.exists:
            data = doc.to_dict()
            data["id"] = doc.id
            return self._normalize_document(data)
        return None

    def get_document_by_field(self, field: str, value: Any) -> dict | None:
        """Fetches the first matching document for a field equality query."""
        matches = self.query_documents(field, "==", value)
        return matches[0] if matches else None

    def get_next_numeric_id(self) -> int:
        """Returns the next numeric ID based on existing Firestore document IDs."""
        if not self.is_available:
            return int(datetime.utcnow().timestamp() * 1000)

        max_id = 0
        docs = self._db.collection(self.collection_name).stream()
        for doc in docs:
            try:
                current_id = int(doc.id)
                max_id = max(max_id, current_id)
            except ValueError:
                continue
        return max_id + 1

    def query_documents(self, field: str, op: str, value: Any) -> list[dict]:
        """Queries Firestore collection matching field conditions."""
        if not self.is_available:
            return []

        docs = self._db.collection(self.collection_name).where(field, op, value).stream()
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


def generate_id() -> int:
    return int(datetime.utcnow().timestamp() * 1000)


def _resolve_document(collection_service: FirestoreService, key: str | int) -> dict | None:
    return collection_service.get_document(str(key)) if key is not None else None


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


# Global Firestore Collection Helper Services
firestore_users = FirestoreService("users")
firestore_classes = FirestoreService("class_rooms")
firestore_subjects = FirestoreService("subjects")
firestore_teacher_mappings = FirestoreService("teacher_subject_class_mappings")
firestore_student_enrollments = FirestoreService("student_enrollments")
firestore_attendance = FirestoreService("attendance_records")
firestore_topics = FirestoreService("topics_covered")
firestore_meetings = FirestoreService("live_meetings")
firestore_materials = FirestoreService("study_materials")
firestore_grades = FirestoreService("exam_grades")
