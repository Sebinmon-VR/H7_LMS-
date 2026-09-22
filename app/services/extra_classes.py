"""
Extra classes: a teacher asks, an administrator decides, and only then does the class exist.

The brief asks that a class held outside the timetable needs approval. The rule is enforced
here rather than in the router so that both products obey it identically - a school teacher
scheduling a revision session and a tuition teacher adding a catch-up class are the same
request with a different subject line.

**Approving and creating are two steps.** `decide` records the decision; `materialise` builds
the meeting or the session. They are apart because creation can fail - a timetable clash, a
Meet link that would not generate - long after a human has pressed approve, and an approval
that rolls itself back because of an API error is worse than one that sits visibly in
APPROVED with no class attached yet. The admin sees exactly what happened and can retry.

**The approval requirement is a setting.** A school that trusts its teachers can turn
`extra_class_needs_approval` off, in which case a request is auto-approved on submission and
the endpoint becomes a one-step "schedule an extra class". The flow does not change shape,
which is what keeps the two configurations from being two code paths.
"""

import logging
from datetime import datetime, timedelta

from fastapi import HTTPException

from app.core.enums import ExtraClassStatus, Program, UserRole
from app.core.firebase import (
    firestore_classes, firestore_extra_classes, firestore_subjects,
    firestore_tuition_enrollments, firestore_users, require_document,
)
from app.services import permissions
from app.services.tuition.settings_store import lms_settings

logger = logging.getLogger("extra_classes")

# Requests that are still live - the admin's queue, and what a clash check has to consider.
OPEN_STATES = frozenset({
    ExtraClassStatus.PENDING.value,
    ExtraClassStatus.APPROVED.value,
    ExtraClassStatus.SCHEDULED.value,
})


def _now() -> str:
    return datetime.utcnow().isoformat()


def _as_datetime(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed


def require_request(request_id) -> dict:
    return require_document(firestore_extra_classes, request_id, "Extra class request")


def approval_required(program: str = Program.LMS.value) -> bool:
    """
    Whether this deployment makes teachers ask.

    Read per request rather than captured at import, so turning it off takes effect for the
    next teacher rather than the next restart.
    """
    return bool(lms_settings()["extra_class_needs_approval"])


def _assert_target(payload, actor) -> None:
    """
    Checks the request names something real, and something this teacher may teach.

    A school request needs a class and a subject; a tuition request needs an enrollment.
    Exactly one shape, because a request carrying both has no single answer to "who is this
    class for?".
    """
    program = getattr(payload.program, "value", payload.program)

    if program == Program.TUITION.value:
        if not payload.enrollment_id:
            raise HTTPException(
                status_code=400,
                detail="A tuition extra class must name the enrollment it belongs to.",
            )
        enrollment = require_document(
            firestore_tuition_enrollments, payload.enrollment_id, "Tuition enrollment"
        )
        if actor.role != UserRole.ADMIN and \
                int(enrollment.get("teacher_id", -1)) != int(actor.id):
            raise HTTPException(
                status_code=403,
                detail="You do not teach this enrollment.",
            )
        return

    if not payload.class_id or not payload.subject_id:
        raise HTTPException(
            status_code=400,
            detail="A school extra class must name both a class and a subject.",
        )
    require_document(firestore_classes, payload.class_id, "Class")
    require_document(firestore_subjects, payload.subject_id, "Subject")

    # A teacher may only request a class they actually teach, or one they lead. Checked
    # against the mappings rather than the role: TEACHER and CLASS_TEACHER are the same
    # guard, and what separates them is per class.
    if actor.role == UserRole.ADMIN:
        return
    if permissions.is_class_teacher_of(actor, payload.class_id):
        return

    from app.core.firebase import firestore_teacher_mappings
    mine = firestore_teacher_mappings.query_documents("teacher_id", "==", int(actor.id))
    if not any(
        int(m.get("class_id", -1)) == int(payload.class_id)
        and int(m.get("subject_id", -1)) == int(payload.subject_id)
        for m in mine
    ):
        raise HTTPException(
            status_code=403,
            detail="You are not mapped to teach this subject to this class.",
        )


def clashes_for(teacher_id, start: datetime, minutes: int,
                exclude_request_id=None) -> list[dict]:
    """
    Other live extra-class requests this teacher already has overlapping the slot.

    Only extra classes are checked, not the whole timetable. That is a deliberate limit: the
    timetable clash check lives in `app.services.timetable` and answers a different question
    with different data, and duplicating a weaker version of it here would give two answers
    to "is this teacher free?". What this catches is the common case - a teacher submitting
    the same request twice, or two overlapping catch-up sessions.
    """
    end = start + timedelta(minutes=int(minutes or 45))
    found = []

    for other in firestore_extra_classes.query_documents("requested_by", "==", int(teacher_id)):
        if other.get("status") not in OPEN_STATES:
            continue
        if exclude_request_id is not None and str(other["id"]) == str(exclude_request_id):
            continue

        other_start = _as_datetime(other.get("scheduled_time"))
        if not other_start:
            continue
        other_end = other_start + timedelta(minutes=int(other.get("duration_minutes") or 45))

        if start < other_end and other_start < end:
            found.append(other)
    return found


def create_request(payload, actor) -> dict:
    """
    Files a request for an extra class.

    Auto-approved when the school has turned the approval requirement off, so the same
    endpoint serves both configurations. Still recorded either way - an extra class that
    nobody had to approve is still an extra class somebody should be able to find later.
    """
    _assert_target(payload, actor)

    start = payload.scheduled_time
    if start.tzinfo:
        start = start.replace(tzinfo=None)

    clashes = clashes_for(actor.id, start, payload.duration_minutes)
    if clashes:
        other = clashes[0]
        raise HTTPException(
            status_code=409,
            detail=(
                f"You already have an extra class ('{other.get('title')}') at "
                f"{other.get('scheduled_time')}. Cancel it or pick another time."
            ),
        )

    program = getattr(payload.program, "value", payload.program)
    needs_approval = approval_required(program)

    request_id = firestore_extra_classes.get_next_numeric_id()
    document = {
        "requested_by": int(actor.id),
        "title": payload.title.strip(),
        "scheduled_time": start.isoformat(),
        "program": program,
        "duration_minutes": int(payload.duration_minutes or 45),
        "reason": payload.reason,
        "class_id": int(payload.class_id) if payload.class_id else None,
        "subject_id": int(payload.subject_id) if payload.subject_id else None,
        "enrollment_id": payload.enrollment_id,
        "status": (
            ExtraClassStatus.PENDING.value if needs_approval
            else ExtraClassStatus.APPROVED.value
        ),
        "created_at": _now(),
    }
    if not needs_approval:
        document["decided_at"] = _now()
        document["decision_note"] = "Auto-approved: this school does not require approval."

    firestore_extra_classes.add_document(str(request_id), document)
    document["id"] = request_id
    logger.info("Extra class request %s filed by %s (%s).",
                request_id, actor.id, document["status"])
    return document


def decide(request: dict, approve: bool, actor, note: str | None = None) -> dict:
    """
    Records an administrator's decision. Does not create the class; see `materialise`.

    A rejection needs a note. "No" with no reason is the message that gets escalated, and
    the teacher asking why is a conversation the system could have had for free.
    """
    if request.get("status") not in (ExtraClassStatus.PENDING.value,):
        raise HTTPException(
            status_code=409,
            detail=f"This request is already {request['status'].lower()} and cannot be "
                   "decided again.",
        )
    if not approve and not str(note or "").strip():
        raise HTTPException(
            status_code=400,
            detail="A rejection needs a reason the teacher can read.",
        )

    updates = {
        "status": (
            ExtraClassStatus.APPROVED.value if approve else ExtraClassStatus.REJECTED.value
        ),
        "decided_by": int(actor.id),
        "decided_at": _now(),
        "decision_note": note,
        "updated_at": _now(),
    }
    firestore_extra_classes.add_document(str(request["id"]), updates)
    logger.info("Extra class request %s %s by %s.",
                request["id"], updates["status"], actor.id)
    return {**request, **updates}


def cancel(request: dict, actor) -> dict:
    """
    Withdraws a request.

    The teacher who filed it and an admin may both do this. Kept distinct from REJECTED in
    the status, because the timetable outcome is the same and the conversation is not.
    """
    if request.get("status") in (ExtraClassStatus.REJECTED.value,
                                 ExtraClassStatus.CANCELLED.value):
        raise HTTPException(
            status_code=409, detail=f"This request is already {request['status'].lower()}."
        )
    if actor.role != UserRole.ADMIN and int(request.get("requested_by", -1)) != int(actor.id):
        raise HTTPException(
            status_code=403, detail="You may only withdraw your own requests."
        )

    updates = {
        "status": ExtraClassStatus.CANCELLED.value,
        "cancelled_by": int(actor.id),
        "updated_at": _now(),
    }
    firestore_extra_classes.add_document(str(request["id"]), updates)
    return {**request, **updates}


def materialise(request: dict, actor) -> dict:
    """
    Creates the approved class - a school meeting or a tuition session - and links it back.

    Separate from `decide` on purpose; see the module docstring. Idempotent: a request that
    already produced a class returns it rather than creating a second one, which matters
    because "approve and schedule" is two calls and the second is exactly the kind that gets
    retried.
    """
    if request.get("status") == ExtraClassStatus.SCHEDULED.value:
        return request
    if request.get("status") != ExtraClassStatus.APPROVED.value:
        raise HTTPException(
            status_code=409,
            detail=f"Only an approved request can be scheduled; this one is "
                   f"{request['status'].lower()}.",
        )

    start = _as_datetime(request.get("scheduled_time"))
    if not start:
        raise HTTPException(
            status_code=400, detail="This request has no readable scheduled time."
        )

    if request.get("program") == Program.TUITION.value:
        created = _materialise_tuition(request, start, actor)
        key = "created_session_id"
    else:
        created = _materialise_school(request, start, actor)
        key = "created_meeting_id"

    updates = {
        "status": ExtraClassStatus.SCHEDULED.value,
        key: created,
        "scheduled_by": int(actor.id),
        "updated_at": _now(),
    }
    firestore_extra_classes.add_document(str(request["id"]), updates)
    logger.info("Extra class request %s scheduled as %s %s.", request["id"], key, created)
    return {**request, **updates}


def _materialise_school(request: dict, start: datetime, actor):
    """
    Creates the school live meeting, reusing the path every other meeting goes through.

    Deliberately not a shortcut straight to Firestore: going through `schedule_meeting` is
    what gets the extra class a real Meet link, the students invited and the recording armed,
    exactly like a timetabled one. An extra class that quietly lacked a link would be the
    first thing anybody noticed.
    """
    from app.schemas.meeting import LiveMeetingCreate
    from app.services.content import schedule_meeting

    teacher = require_document(firestore_users, request["requested_by"], "Teacher")

    meeting_in = LiveMeetingCreate(
        class_id=int(request["class_id"]),
        subject_id=int(request["subject_id"]),
        title=request["title"],
        scheduled_time=start,
        duration_minutes=int(request.get("duration_minutes") or 45),
    )
    meeting = schedule_meeting(
        meeting_in,
        teacher_id=int(teacher["id"]),
        teacher_email=teacher.get("email"),
    )
    return meeting.get("id")


def _materialise_tuition(request: dict, start: datetime, actor):
    """Creates the tuition session through the tuition module's own ad-hoc path."""
    from app.services.tuition import sessions as tuition_sessions

    payload = type("AdHoc", (), {
        "enrollment_id": request["enrollment_id"],
        "scheduled_start_at": start,
        "duration_minutes": int(request.get("duration_minutes") or 45),
        "topic": request.get("title"),
        "notes": request.get("reason"),
    })()
    session = tuition_sessions.create_ad_hoc(payload, actor, allow_conflicts=False)
    return session.get("id")


# ---------------------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------------------

def list_requests(status: str | None = None, program: str | None = None,
                  teacher_id=None) -> list[dict]:
    requests = firestore_extra_classes.list_all()

    if status:
        requests = [r for r in requests if r.get("status") == str(status).upper()]
    if program:
        requests = [r for r in requests
                    if (r.get("program") or Program.LMS.value) == str(program).upper()]
    if teacher_id is not None:
        requests = [r for r in requests
                    if str(r.get("requested_by")) == str(teacher_id)]

    # Pending first - it is a queue, and an admin opening it wants the decisions, not the
    # history - then by when the class is due.
    requests.sort(key=lambda r: (
        r.get("status") != ExtraClassStatus.PENDING.value,
        str(r.get("scheduled_time") or ""),
    ))
    return requests


def present(request: dict) -> dict:
    teacher = firestore_users.get_document(str(request.get("requested_by"))) or {}
    decider = (
        firestore_users.get_document(str(request["decided_by"]))
        if request.get("decided_by") is not None else None
    )

    class_name = subject_name = None
    if request.get("class_id"):
        class_name = (firestore_classes.get_document(str(request["class_id"])) or {}).get("name")
    if request.get("subject_id"):
        subject_name = (firestore_subjects.get_document(str(request["subject_id"])) or {}).get("name")

    return {
        "id": int(request["id"]),
        "requested_by": int(request["requested_by"]),
        "teacher_name": teacher.get("full_name"),
        "title": request.get("title"),
        "scheduled_time": request.get("scheduled_time"),
        "duration_minutes": int(request.get("duration_minutes") or 45),
        "program": request.get("program") or Program.LMS.value,
        "reason": request.get("reason"),
        "class_id": request.get("class_id"),
        "class_name": class_name,
        "subject_id": request.get("subject_id"),
        "subject_name": subject_name,
        "enrollment_id": request.get("enrollment_id"),
        "status": request.get("status") or ExtraClassStatus.PENDING.value,
        "decided_by": request.get("decided_by"),
        "decided_by_name": (decider or {}).get("full_name"),
        "decided_at": request.get("decided_at"),
        "decision_note": request.get("decision_note"),
        "created_meeting_id": request.get("created_meeting_id"),
        "created_session_id": request.get("created_session_id"),
        "created_at": request.get("created_at"),
    }
