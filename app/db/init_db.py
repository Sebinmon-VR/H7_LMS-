import logging
from datetime import datetime

from app.core.firebase import (
    firestore_classes,
    firestore_student_enrollments,
    firestore_subjects,
    firestore_teacher_mappings,
    firestore_users,
    generate_id,
)
from app.core.security import hash_password
from app.core.enums import UserRole

logger = logging.getLogger("init_db")


def _get_user_by_email(email: str) -> dict | None:
    return firestore_users.get_document_by_field("email", email)


def _save_user(user_data: dict) -> dict:
    if user_data.get("id") is None:
        user_data["id"] = generate_id()
    firestore_users.add_document(str(user_data["id"]), user_data)
    return user_data


def _save_entity(entity_data: dict, service) -> dict:
    if entity_data.get("id") is None:
        entity_data["id"] = generate_id()
    service.add_document(str(entity_data["id"]), entity_data)
    return entity_data


def init_db() -> None:
    """
    Seeds initial default records into Firestore.
    """
    admin = _get_user_by_email("admin@lms.com")
    if not admin:
        admin = _save_user({
            "full_name": "System Administrator",
            "email": "admin@lms.com",
            "hashed_password": hash_password("admin123"),
            "role": UserRole.ADMIN.value,
            "is_active": True,
            "created_at": datetime.utcnow().isoformat(),
        })
        logger.info("Created default Admin account: admin@lms.com / admin123")

    teacher_math = _get_user_by_email("teacher.math@lms.com")
    if not teacher_math:
        teacher_math = _save_user({
            "full_name": "Prof. Alan Math",
            "email": "teacher.math@lms.com",
            "hashed_password": hash_password("teacher123"),
            "role": UserRole.TEACHER.value,
            "is_active": True,
            "created_at": datetime.utcnow().isoformat(),
        })

    student_alice = _get_user_by_email("student.alice@lms.com")
    if not student_alice:
        student_alice = _save_user({
            "full_name": "Alice Johnson",
            "email": "student.alice@lms.com",
            "hashed_password": hash_password("student123"),
            "role": UserRole.STUDENT.value,
            "is_active": True,
            "created_at": datetime.utcnow().isoformat(),
        })

    student_bob = _get_user_by_email("student.bob@lms.com")
    if not student_bob:
        student_bob = _save_user({
            "full_name": "Bob Smith",
            "email": "student.bob@lms.com",
            "hashed_password": hash_password("student123"),
            "role": UserRole.STUDENT.value,
            "is_active": True,
            "created_at": datetime.utcnow().isoformat(),
        })

    class_10a = next(
        (c for c in firestore_classes.list_all() if c.get("code") == "CLASS-10A"),
        None
    )
    if class_10a is None:
        class_10a = _save_entity({
            "name": "Grade 10 - Section A",
            "code": "CLASS-10A",
            "description": "High school 10th standard section A",
            "created_at": datetime.utcnow().isoformat(),
        }, firestore_classes)

    subject_math = next(
        (s for s in firestore_subjects.list_all() if s.get("code") == "MATH101"),
        None
    )
    if subject_math is None:
        subject_math = _save_entity({
            "name": "Mathematics",
            "code": "MATH101",
            "description": "Algebra, Trigonometry, and Geometry",
            "created_at": datetime.utcnow().isoformat(),
        }, firestore_subjects)

    if teacher_math and subject_math and class_10a:
        existing_mapping = next(
            (
                m for m in firestore_teacher_mappings.query_documents("teacher_id", "==", teacher_math["id"])
                if m.get("subject_id") == subject_math["id"] and m.get("class_id") == class_10a["id"]
            ),
            None
        )
        if existing_mapping is None:
            _save_entity({
                "teacher_id": teacher_math["id"],
                "subject_id": subject_math["id"],
                "class_id": class_10a["id"],
                "created_at": datetime.utcnow().isoformat(),
            }, firestore_teacher_mappings)

    if student_alice and class_10a:
        existing_enrollment = next(
            (
                e for e in firestore_student_enrollments.query_documents("student_id", "==", student_alice["id"])
                if e.get("class_id") == class_10a["id"]
            ),
            None
        )
        if existing_enrollment is None:
            _save_entity({
                "student_id": student_alice["id"],
                "class_id": class_10a["id"],
                "enrolled_at": datetime.utcnow().isoformat(),
            }, firestore_student_enrollments)

    if student_bob and class_10a:
        existing_enrollment = next(
            (
                e for e in firestore_student_enrollments.query_documents("student_id", "==", student_bob["id"])
                if e.get("class_id") == class_10a["id"]
            ),
            None
        )
        if existing_enrollment is None:
            _save_entity({
                "student_id": student_bob["id"],
                "class_id": class_10a["id"],
                "enrolled_at": datetime.utcnow().isoformat(),
            }, firestore_student_enrollments)

    logger.info("Database seeding completed successfully.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    init_db()
