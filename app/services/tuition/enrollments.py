"""
Tuition enrollments: which student takes which subject, and with whom.

This is the module every other tuition module asks a question of. Slots, sessions, library
items, assignments and invoices are all scoped by an enrollment, and "may this person see
this record?" almost always reduces to "are they one of the two people on this enrollment?".

The invariant worth stating plainly, because everything else depends on it: **at most one
active enrollment per (student, subject)**. A student with two physics teachers has no
answer to "who marks this?", "whose attendance report is this?", or "which teacher does the
5pm slot belong to?" - so the second one is rejected at creation rather than discovered
later by whichever report happens to double-count first.
"""

import logging
from datetime import datetime

from fastapi import HTTPException, status

from app.core.enums import (
    Program, TEACHING_ROLE_VALUES, TuitionEnrollmentStatus, UserRole,
)
from app.core.firebase import (
    firestore_subjects, firestore_tuition_enrollments, firestore_tuition_slots,
    firestore_users, hydrate_tuition_enrollment, prefetch_tuition,
)
from app.services.tuition.common import (
    has_program, is_admin, parse_date, user_id_of,
)

logger = logging.getLogger("tuition.enrollments")

ACTIVE_STATUSES = frozenset({
    TuitionEnrollmentStatus.ACTIVE.value,
    TuitionEnrollmentStatus.PAUSED.value,
})


# ---------------------------------------------------------------------------------------
# Participant validation
# ---------------------------------------------------------------------------------------

def require_tuition_user(user_id: int, expected_role: str, label: str) -> dict:
    """
    Loads a participant and checks they are both the right role and in the tuition programme.

    Both halves matter, and the error messages say which failed. "Teacher 42 is not enrolled
    in the tuition programme" tells an admin to tick a box; a generic "invalid teacher" sends
    them looking for a user who is sitting right there in the list.
    """
    record = firestore_users.get_document(str(user_id))
    if not record:
        raise HTTPException(status_code=404, detail=f"{label} {user_id} not found")

    role = str(record.get("role") or "")
    allowed = TEACHING_ROLE_VALUES if expected_role == "TEACHER" else {expected_role}
    if role not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"User {user_id} has role '{role}' and cannot be assigned as a {label.lower()}.",
        )

    if not record.get("is_active", True):
        raise HTTPException(status_code=400, detail=f"{label} {user_id} is deactivated.")

    if not has_program(record, Program.TUITION.value):
        raise HTTPException(
            status_code=400,
            detail=f"{label} {user_id} is not enrolled in the online tuition programme. "
                   f"Grant tuition access on their profile first.",
        )
    return record


def require_subject(subject_id: int) -> dict:
    subject = firestore_subjects.get_document(str(subject_id))
    if not subject:
        raise HTTPException(status_code=404, detail=f"Subject {subject_id} not found")
    return subject


# ---------------------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------------------

def require_enrollment(enrollment_id) -> dict:
    record = firestore_tuition_enrollments.get_document(str(enrollment_id))
    if not record:
        raise HTTPException(status_code=404, detail=f"Tuition enrollment {enrollment_id} not found")
    return record


def participants(enrollment: dict) -> set[int]:
    """The two people an enrollment belongs to."""
    return {enrollment.get("student_id"), enrollment.get("teacher_id")} - {None}


def may_view(enrollment: dict, user) -> bool:
    """An admin, the assigned teacher, or the student. Nobody else - it is a private class."""
    return is_admin(user) or user_id_of(user) in participants(enrollment)


def assert_may_view(enrollment: dict, user, action: str = "view this enrollment") -> None:
    if not may_view(enrollment, user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"You are not a participant in this tuition arrangement and may not {action}.",
        )


def for_student(student_id: int, include_inactive: bool = False) -> list[dict]:
    records = firestore_tuition_enrollments.query_documents("student_id", "==", student_id)
    if include_inactive:
        return records
    return [r for r in records if r.get("status") in ACTIVE_STATUSES]


def for_teacher(teacher_id: int, include_inactive: bool = False) -> list[dict]:
    records = firestore_tuition_enrollments.query_documents("teacher_id", "==", teacher_id)
    if include_inactive:
        return records
    return [r for r in records if r.get("status") in ACTIVE_STATUSES]


def visible_to(user, include_inactive: bool = False) -> list[dict]:
    """
    Every enrollment a user is entitled to see.

    An admin sees the programme; a teacher sees the students they teach; a student sees the
    subjects they take. There is no fourth case, which is why this is one function rather
    than a filter each router reimplements slightly differently.
    """
    if is_admin(user):
        records = firestore_tuition_enrollments.list_all()
        return records if include_inactive else [
            r for r in records if r.get("status") in ACTIVE_STATUSES
        ]

    user_id = user_id_of(user)
    role = getattr(user, "role", None)
    role_value = role.value if isinstance(role, UserRole) else str(role or "")
    if role_value in TEACHING_ROLE_VALUES:
        return for_teacher(user_id, include_inactive)
    return for_student(user_id, include_inactive)


def hydrate_many(records: list[dict]) -> list[dict]:
    """Expands a list of enrollments, prefetching their references in one batch each."""
    if not records:
        return []
    prefetch_tuition(records)
    return [hydrate_tuition_enrollment(record) for record in records]


def existing_active(student_id: int, subject_id: int, exclude_id=None) -> dict | None:
    """
    The active enrollment already covering this (student, subject), if any.

    Queried on `student_id` and filtered in memory rather than with a compound query: a
    student has a handful of subjects, so the filter is free, and a composite Firestore index
    is one more thing that has to be created before the feature works in a fresh project.
    """
    for record in firestore_tuition_enrollments.query_documents("student_id", "==", student_id):
        if record.get("subject_id") != subject_id:
            continue
        if exclude_id is not None and str(record.get("id")) == str(exclude_id):
            continue
        if record.get("status") in ACTIVE_STATUSES:
            return record
    return None


# ---------------------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------------------

def create_enrollment(payload, actor) -> dict:
    """
    Assigns a teacher to a student for a subject.

    Every participant is validated before anything is written, so a rejected enrollment
    leaves no half-built record behind. The duplicate check is the important one - see the
    module docstring for why a second active teacher on the same subject is a data error
    rather than a richer arrangement.
    """
    student = require_tuition_user(payload.student_id, UserRole.STUDENT.value, "Student")
    require_tuition_user(payload.teacher_id, "TEACHER", "Teacher")
    subject = require_subject(payload.subject_id)

    clash = existing_active(payload.student_id, payload.subject_id)
    if clash:
        clash_teacher = firestore_users.get_document(str(clash.get("teacher_id"))) or {}
        raise HTTPException(
            status_code=409,
            detail=(
                f"{student.get('full_name')} already takes {subject.get('name')} with "
                f"{clash_teacher.get('full_name', 'another teacher')} (enrollment "
                f"{clash.get('id')}). One subject has one teacher: change that enrollment's "
                f"teacher, or end it first."
            ),
        )

    enrollment_id = firestore_tuition_enrollments.get_next_numeric_id()
    document = {
        "student_id": payload.student_id,
        "teacher_id": payload.teacher_id,
        "subject_id": payload.subject_id,
        "status": TuitionEnrollmentStatus.ACTIVE.value,
        "syllabus": payload.syllabus,
        "goals": payload.goals,
        "grade_level": payload.grade_level,
        "default_duration_minutes": payload.default_duration_minutes,
        "fee_plan_id": payload.fee_plan_id,
        "start_date": payload.start_date.isoformat() if payload.start_date else None,
        "end_date": payload.end_date.isoformat() if payload.end_date else None,
        "notes": payload.notes,
        "created_by": user_id_of(actor),
        "created_at": datetime.utcnow().isoformat(),
        "updated_at": None,
    }
    firestore_tuition_enrollments.add_document(str(enrollment_id), document)
    document["id"] = enrollment_id
    logger.info(
        "Tuition enrollment %s created: student %s takes %s with teacher %s.",
        enrollment_id, payload.student_id, subject.get("name"), payload.teacher_id,
    )
    return document


def apply_update(enrollment: dict, payload, actor) -> dict:
    """
    Applies a partial update, cascading a teacher change down to the slots.

    The cascade is the part that would be easy to miss and expensive to get wrong. A slot
    stores `teacher_id` denormalized so conflict detection is one query instead of a join;
    re-assigning the subject to a new teacher without rewriting those slots would leave the
    old teacher holding the timetable while the new one is answerable for the marks. Future
    *sessions* are re-pointed too - past ones are history and keep the teacher who actually
    took them.
    """
    updates: dict = {}
    changed = payload.model_dump(exclude_unset=True)

    if "teacher_id" in changed and changed["teacher_id"] != enrollment.get("teacher_id"):
        require_tuition_user(changed["teacher_id"], "TEACHER", "Teacher")
        updates["teacher_id"] = changed["teacher_id"]

    if "status" in changed and changed["status"] is not None:
        new_status = changed["status"]
        updates["status"] = new_status.value if hasattr(new_status, "value") else str(new_status)
        # Re-activating has to respect the one-teacher rule just as creation does, or a
        # paused enrollment becomes the back door around it.
        if updates["status"] in ACTIVE_STATUSES:
            clash = existing_active(
                enrollment["student_id"], enrollment["subject_id"], exclude_id=enrollment["id"]
            )
            if clash:
                raise HTTPException(
                    status_code=409,
                    detail=f"This student already has an active enrollment ({clash.get('id')}) "
                           f"for that subject. End it before re-activating this one.",
                )

    for field in ("syllabus", "goals", "grade_level", "notes", "default_duration_minutes",
                  "fee_plan_id"):
        if field in changed:
            updates[field] = changed[field]

    for field in ("start_date", "end_date"):
        if field in changed:
            value = parse_date(changed[field])
            updates[field] = value.isoformat() if value else None

    if not updates:
        return enrollment

    updates["updated_at"] = datetime.utcnow().isoformat()
    firestore_tuition_enrollments.add_document(str(enrollment["id"]), updates)
    merged = {**enrollment, **updates}

    if "teacher_id" in updates:
        _recut_teacher(enrollment["id"], updates["teacher_id"], actor)

    return merged


def _recut_teacher(enrollment_id, teacher_id: int, actor) -> None:
    """
    Re-points an enrollment's slots and its still-to-come sessions at a new teacher.

    Deliberately does *not* re-run conflict detection on the moved slots. It could, but
    refusing a teacher change because the incoming teacher is busy on Thursdays would leave
    the enrollment pointing at a teacher who has already left, which is worse. Instead the
    move goes through and the clash surfaces on the admin's conflict report, where it can be
    resolved by moving the slot rather than by blocking the reassignment.
    """
    from app.core.firebase import firestore_tuition_sessions
    from app.services.tuition.common import now_utc, to_utc

    stamp = datetime.utcnow().isoformat()
    slots = firestore_tuition_slots.query_documents("enrollment_id", "==", enrollment_id)
    for slot in slots:
        firestore_tuition_slots.add_document(
            str(slot["id"]), {"teacher_id": teacher_id, "updated_at": stamp}
        )

    moment = now_utc()
    sessions = firestore_tuition_sessions.query_documents("enrollment_id", "==", enrollment_id)
    moved = 0
    for session in sessions:
        start = to_utc(session.get("scheduled_start_at"))
        if start is None or start <= moment:
            continue
        if session.get("status") != "SCHEDULED":
            continue
        firestore_tuition_sessions.add_document(
            str(session["id"]), {"teacher_id": teacher_id, "updated_at": stamp}
        )
        moved += 1

    logger.info(
        "Enrollment %s reassigned to teacher %s: %s slots and %s upcoming sessions moved.",
        enrollment_id, teacher_id, len(slots), moved,
    )


def delete_enrollment(enrollment_id) -> dict[str, int]:
    """
    Removes an enrollment and the schedule hanging off it.

    Slots go, because a recurring rule with no arrangement behind it would keep generating
    classes for a student who no longer takes the subject. Sessions and library items stay:
    they are the record of teaching that actually happened, and an invoice already issued
    against them must remain explicable. Ending an enrollment - status COMPLETED - is almost
    always the right operation; this exists for the record created in error.
    """
    from app.core.firebase import firestore_tuition_sessions
    from app.services.tuition.common import now_utc, to_utc

    removed = {"slots": 0, "future_sessions": 0}

    for slot in firestore_tuition_slots.query_documents("enrollment_id", "==", enrollment_id):
        firestore_tuition_slots.delete_document(str(slot["id"]))
        removed["slots"] += 1

    moment = now_utc()
    for session in firestore_tuition_sessions.query_documents("enrollment_id", "==", enrollment_id):
        start = to_utc(session.get("scheduled_start_at"))
        if start and start > moment and session.get("status") == "SCHEDULED":
            firestore_tuition_sessions.delete_document(str(session["id"]))
            removed["future_sessions"] += 1

    firestore_tuition_enrollments.delete_document(str(enrollment_id))
    return removed
