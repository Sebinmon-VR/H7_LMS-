"""
Staff leave: applying, deciding, and what it adds up to.

Two things here are worth stating, because both are easy to get subtly wrong and expensive to
discover late.

**`total_days` is computed once and stored.** The leave *balance* is a running total over
these records, and a balance that changes because somebody adjusted the half-day rules two
years later is not a balance. The days are worked out at application time from the dates and
the day part, and that number is what every later total reads.

**Pending is not the same as taken, and neither is free.** `balance_for` reports them
separately. A school that shows one combined figure approves two teachers for the same week,
because the first request was still pending when the second was looked at.

The affected periods are snapshotted onto the request when it is made, rather than looked up
when it is read. The approver needs to see what they are agreeing to cover, and the timetable
will have moved on by the time anybody audits the decision.
"""

import logging
from datetime import date, datetime, timedelta

from fastapi import HTTPException

from app.core.enums import (
    DayOfWeek, LeaveDayPart, LeaveStatus, LeaveType, TEACHING_ROLE_VALUES, UserRole,
)
from app.core.firebase import (
    firestore_classes, firestore_leave_requests, firestore_subjects, firestore_timetable,
    firestore_users, require_document,
)

logger = logging.getLogger("leave")

# Requests that consume, or may yet consume, the allowance.
LIVE_STATES = frozenset({LeaveStatus.PENDING.value, LeaveStatus.APPROVED.value})


def _now() -> str:
    return datetime.utcnow().isoformat()


def _as_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def require_request(request_id) -> dict:
    return require_document(firestore_leave_requests, request_id, "Leave request")


def count_days(start: date, end: date, day_part: str) -> float:
    """
    How many days a request consumes.

    Half days count as 0.5 and are only valid on a single date, which the schema enforces.
    Weekends and holidays are *not* excluded: this codebase has no holiday calendar, and
    silently not counting Saturdays would be wrong for the many schools that teach on them.
    A school that wants working-day counting should say so, and then it becomes a real
    feature rather than an assumption buried in an arithmetic helper.
    """
    if day_part in (LeaveDayPart.FIRST_HALF.value, LeaveDayPart.SECOND_HALF.value):
        return 0.5
    return float((end - start).days + 1)


def affected_periods(teacher_id, start: date, end: date) -> list[dict]:
    """
    What this teacher was timetabled to take across the dates.

    Expanded from the recurring timetable onto concrete dates, so the approver sees "three
    Class 7 maths periods and a Class 9 double" rather than a date range they have to work
    out for themselves.

    Capped at 60 days: a longer absence is maternity or sabbatical, where listing every
    period is noise rather than information, and expanding it is a lot of synthesis for a
    list nobody reads.
    """
    if (end - start).days > 60:
        return []

    mine = firestore_timetable.query_documents("teacher_id", "==", int(teacher_id))
    by_weekday: dict[int, list[dict]] = {}
    for entry in mine:
        try:
            weekday = DayOfWeek(entry.get("day_of_week")).iso_weekday
        except (ValueError, TypeError):
            continue
        by_weekday.setdefault(weekday, []).append(entry)

    periods = []
    cursor = start
    while cursor <= end:
        for entry in by_weekday.get(cursor.isoweekday(), []):
            class_room = firestore_classes.get_document(str(entry.get("class_id"))) or {}
            subject = firestore_subjects.get_document(str(entry.get("subject_id"))) or {}
            periods.append({
                "timetable_entry_id": entry.get("id"),
                "date": cursor.isoformat(),
                "class_id": entry.get("class_id"),
                "class_name": class_room.get("name"),
                "subject_name": subject.get("name"),
                "start_time": str(entry.get("start_time") or ""),
                "end_time": str(entry.get("end_time") or ""),
            })
        cursor += timedelta(days=1)

    return periods


def overlapping(teacher_id, start: date, end: date, exclude_id=None) -> list[dict]:
    """Live requests from the same teacher covering any of the same dates."""
    found = []
    for other in firestore_leave_requests.query_documents("teacher_id", "==", int(teacher_id)):
        if other.get("status") not in LIVE_STATES:
            continue
        if exclude_id is not None and str(other["id"]) == str(exclude_id):
            continue

        other_start = _as_date(other.get("start_date"))
        other_end = _as_date(other.get("end_date"))
        if not other_start or not other_end:
            continue
        if start <= other_end and other_start <= end:
            found.append(other)
    return found


def apply_for_leave(payload, actor) -> dict:
    """
    Files a leave application.

    Refuses an overlap with the applicant's own live request, which is nearly always a double
    submission rather than a genuine intention to be away twice at once.
    """
    profile = require_document(firestore_users, actor.id, "User")
    if str(profile.get("role")) not in TEACHING_ROLE_VALUES | {UserRole.ADMIN.value}:
        raise HTTPException(
            status_code=403, detail="Only staff may apply for leave."
        )

    clash = overlapping(actor.id, payload.start_date, payload.end_date)
    if clash:
        other = clash[0]
        raise HTTPException(
            status_code=409,
            detail=(
                f"You already have a {other.get('status', '').lower()} leave request for "
                f"{other.get('start_date')} to {other.get('end_date')}."
            ),
        )

    request_id = firestore_leave_requests.get_next_numeric_id()
    day_part = getattr(payload.day_part, "value", payload.day_part)

    document = {
        "teacher_id": int(actor.id),
        "leave_type": getattr(payload.leave_type, "value", payload.leave_type),
        "start_date": payload.start_date.isoformat(),
        "end_date": payload.end_date.isoformat(),
        "day_part": day_part,
        # Computed once; see the module docstring.
        "total_days": count_days(payload.start_date, payload.end_date, day_part),
        "reason": payload.reason,
        "contact_during_leave": payload.contact_during_leave,
        "status": LeaveStatus.PENDING.value,
        "affected_periods": affected_periods(
            actor.id, payload.start_date, payload.end_date
        ),
        "attachments": payload.attachments or [],
        "created_at": _now(),
    }
    firestore_leave_requests.add_document(str(request_id), document)
    document["id"] = request_id
    logger.info("Leave request %s filed by %s (%s days).",
                request_id, actor.id, document["total_days"])
    return document


def decide(request: dict, approve: bool, actor, note: str | None = None,
           substitute_teacher_id=None) -> dict:
    """
    Records the decision.

    A rejection needs a note, for the same reason an extra class does: "no" with no reason is
    the message that gets escalated to whoever is above the person who sent it.
    """
    if request.get("status") != LeaveStatus.PENDING.value:
        raise HTTPException(
            status_code=409,
            detail=f"This request is already {request['status'].lower()}.",
        )
    if not approve and not str(note or "").strip():
        raise HTTPException(
            status_code=400, detail="A rejection needs a reason the applicant can read."
        )
    if substitute_teacher_id is not None:
        substitute = require_document(firestore_users, substitute_teacher_id, "Substitute teacher")
        if str(substitute.get("role")) not in TEACHING_ROLE_VALUES:
            raise HTTPException(
                status_code=400,
                detail=f"{substitute.get('full_name')} is not a teacher and cannot cover classes.",
            )
        if int(substitute_teacher_id) == int(request["teacher_id"]):
            raise HTTPException(
                status_code=400,
                detail="A teacher cannot cover their own leave.",
            )

    updates = {
        "status": LeaveStatus.APPROVED.value if approve else LeaveStatus.REJECTED.value,
        "decided_by": int(actor.id),
        "decided_at": _now(),
        "decision_note": note,
        "substitute_teacher_id": (
            int(substitute_teacher_id) if substitute_teacher_id is not None else None
        ),
        "updated_at": _now(),
    }
    firestore_leave_requests.add_document(str(request["id"]), updates)
    logger.info("Leave request %s %s by %s.", request["id"], updates["status"], actor.id)
    return {**request, **updates}


def withdraw(request: dict, actor) -> dict:
    """
    The applicant taking their own request back.

    Allowed while pending, and also after approval - plans change, and a teacher who no
    longer needs the day should be able to give it back rather than having it counted
    against them. An admin cancelling somebody else's approved leave is a different act and
    is recorded as CANCELLED.
    """
    if request.get("status") not in LIVE_STATES:
        raise HTTPException(
            status_code=409,
            detail=f"This request is already {request['status'].lower()}.",
        )

    is_owner = int(request.get("teacher_id", -1)) == int(actor.id)
    if not is_owner and getattr(actor, "role", None) != UserRole.ADMIN:
        raise HTTPException(
            status_code=403, detail="You may only withdraw your own leave requests."
        )

    updates = {
        "status": (
            LeaveStatus.WITHDRAWN.value if is_owner else LeaveStatus.CANCELLED.value
        ),
        "withdrawn_by": int(actor.id),
        "updated_at": _now(),
    }
    firestore_leave_requests.add_document(str(request["id"]), updates)
    return {**request, **updates}


# ---------------------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------------------

def list_requests(teacher_id=None, status: str | None = None,
                  leave_type: str | None = None,
                  from_date: date | None = None, to_date: date | None = None) -> list[dict]:
    requests = firestore_leave_requests.list_all()

    if teacher_id is not None:
        requests = [r for r in requests if str(r.get("teacher_id")) == str(teacher_id)]
    if status:
        requests = [r for r in requests if r.get("status") == str(status).upper()]
    if leave_type:
        requests = [r for r in requests if r.get("leave_type") == str(leave_type).upper()]

    if from_date or to_date:
        filtered = []
        for request in requests:
            start = _as_date(request.get("start_date"))
            end = _as_date(request.get("end_date"))
            if not start or not end:
                continue
            if from_date and end < from_date:
                continue
            if to_date and start > to_date:
                continue
            filtered.append(request)
        requests = filtered

    # Pending first - it is a queue - then by start date.
    requests.sort(key=lambda r: (
        r.get("status") != LeaveStatus.PENDING.value,
        str(r.get("start_date") or ""),
    ))
    return requests


def balance_for(teacher_id, academic_year_id=None) -> dict:
    """
    One teacher's leave, totalled by type.

    Taken and pending are reported separately; see the module docstring for why combining
    them gets two teachers approved for the same week.

    Scoped to an academic year when one is given, using the year's own start and end rather
    than a calendar year - a school's allowance runs with its session, not with January.
    """
    from app.services import admissions as admission_service

    window_start = window_end = None
    if academic_year_id is not None:
        year = admission_service.require_year(academic_year_id)
        window_start = _as_date(year.get("start_date"))
        window_end = _as_date(year.get("end_date"))

    taken: dict[str, float] = {}
    pending: dict[str, float] = {}
    counted = 0

    for request in firestore_leave_requests.query_documents("teacher_id", "==", int(teacher_id)):
        status = request.get("status")
        if status not in LIVE_STATES:
            continue

        start = _as_date(request.get("start_date"))
        if window_start and start and not (window_start <= start <= window_end):
            continue

        leave_type = request.get("leave_type") or LeaveType.OTHER.value
        days = float(request.get("total_days") or 0)
        bucket = taken if status == LeaveStatus.APPROVED.value else pending
        bucket[leave_type] = round(bucket.get(leave_type, 0.0) + days, 2)
        counted += 1

    teacher = firestore_users.get_document(str(teacher_id)) or {}
    return {
        "teacher_id": int(teacher_id),
        "teacher_name": teacher.get("full_name"),
        "academic_year_id": int(academic_year_id) if academic_year_id is not None else None,
        "taken_days": taken,
        "pending_days": pending,
        "total_taken": round(sum(taken.values()), 2),
        "total_pending": round(sum(pending.values()), 2),
        "request_count": counted,
    }


def on_leave_on(day: date) -> list[dict]:
    """
    Who is approved to be away on a date.

    What a daily cover sheet is built from, and what a timetable view should grey out. Only
    approved leave counts: a pending request is not yet an absence, and treating it as one
    would have the office arranging cover for a day off nobody has granted.
    """
    away = []
    for request in firestore_leave_requests.query_documents(
        "status", "==", LeaveStatus.APPROVED.value
    ):
        start = _as_date(request.get("start_date"))
        end = _as_date(request.get("end_date"))
        if start and end and start <= day <= end:
            away.append(request)
    return away


def present(request: dict) -> dict:
    teacher = firestore_users.get_document(str(request.get("teacher_id"))) or {}
    decider = (
        firestore_users.get_document(str(request["decided_by"]))
        if request.get("decided_by") is not None else None
    )
    substitute = (
        firestore_users.get_document(str(request["substitute_teacher_id"]))
        if request.get("substitute_teacher_id") is not None else None
    )

    return {
        "id": int(request["id"]),
        "teacher_id": int(request["teacher_id"]),
        "teacher_name": teacher.get("full_name"),
        "employee_id": teacher.get("employee_id"),
        "leave_type": request.get("leave_type") or LeaveType.OTHER.value,
        "start_date": request.get("start_date"),
        "end_date": request.get("end_date"),
        "day_part": request.get("day_part") or LeaveDayPart.FULL_DAY.value,
        "total_days": float(request.get("total_days") or 0),
        "reason": request.get("reason"),
        "contact_during_leave": request.get("contact_during_leave"),
        "status": request.get("status") or LeaveStatus.PENDING.value,
        "decided_by": request.get("decided_by"),
        "decided_by_name": (decider or {}).get("full_name"),
        "decided_at": request.get("decided_at"),
        "decision_note": request.get("decision_note"),
        "substitute_teacher_id": request.get("substitute_teacher_id"),
        "substitute_teacher_name": (substitute or {}).get("full_name"),
        "affected_periods": request.get("affected_periods") or [],
        "attachments": request.get("attachments") or [],
        "created_at": request.get("created_at"),
    }
