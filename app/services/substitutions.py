"""
Covers: a substitute teaching one timetable period on one date.

A cover is a dated exception, never an edit to the timetable. The recurring entry keeps its
teacher, so next week's Tuesday period is unchanged and the cover ends with the day it names.
`timetable.resolve_on_date` applies covers, so the student's day, the teacher's day and the
class room's access all follow the cover without knowing it exists.

Covers are set one date at a time, by the teacher for their own period or by an admin, with
or without any leave. A leave can also carry a cover plan - the classes it takes away and the
substitute the applicant chose for each - which becomes covers for the leave's own days when
it is approved, and is taken back if that leave is withdrawn.
"""

import logging
from datetime import date

from fastapi import HTTPException

from app.core.enums import TEACHING_ROLE_VALUES
from app.core.firebase import (
    firestore_period_substitutions, firestore_timetable, firestore_users, require_document,
)
from app.services import timetable as timetable_service

logger = logging.getLogger("substitutions")


def cover_key(entry_id, on_date: date) -> str:
    return f"{int(entry_id)}:{on_date.isoformat()}"


def covers_on(on_date: date) -> list[dict]:
    return firestore_period_substitutions.query_documents("on_date", "==", on_date.isoformat())


def _clash_for(substitute_id: int, entry: dict, on_date: date) -> str | None:
    """
    The period the substitute already has at this time that day, or None when it is free.

    Checked against the whole day with covers applied, so a teacher already covering another
    class at 09:00 cannot be given a second class at 09:00.
    """
    own = timetable_service.resolve_on_date([entry], on_date)
    if not own:
        return None
    wanted = own[0]

    for period in timetable_service.resolve_on_date(timetable_service.list_entries(), on_date):
        if period["entry"]["id"] == entry["id"]:
            continue
        if (period["entry"].get("teacher_id") or None) != substitute_id:
            continue
        if period["starts_at"] < wanted["ends_at"] and wanted["starts_at"] < period["ends_at"]:
            class_name = (period["entry"].get("class_room") or {}).get("name") or "another class"
            return f"already teaching {class_name} at {timetable_service.format_time(period['entry']['start_time'])}"
    return None


def create(
    entry_id: int,
    on_date: date,
    substitute_teacher_id: int,
    actor=None,
    note: str | None = None,
    leave_request_id: int | None = None,
) -> dict:
    """Records a cover for one period on one date, after checking it is possible."""
    entry = require_document(firestore_timetable, entry_id, "Timetable period")
    if not timetable_service.entry_active_on(entry, on_date):
        raise HTTPException(status_code=400, detail=f"This period does not run on {on_date.isoformat()}.")
    if entry.get("teacher_id") == substitute_teacher_id:
        raise HTTPException(status_code=400, detail="The timetabled teacher already takes this period.")

    substitute = require_document(firestore_users, substitute_teacher_id, "Substitute teacher")
    if str(substitute.get("role")) not in TEACHING_ROLE_VALUES:
        raise HTTPException(
            status_code=400,
            detail=f"{substitute.get('full_name')} is not a teacher and cannot cover a period.",
        )

    clash = _clash_for(int(substitute_teacher_id), entry, on_date)
    if clash:
        raise HTTPException(
            status_code=409,
            detail=f"{substitute.get('full_name')} is {clash}.",
        )

    record = {
        "timetable_entry_id": int(entry_id),
        "on_date": on_date.isoformat(),
        "substitute_teacher_id": int(substitute_teacher_id),
        "note": (note or "").strip() or None,
        "leave_request_id": leave_request_id,
        "created_by": int(actor.id) if actor is not None and getattr(actor, "id", None) is not None else None,
    }
    firestore_period_substitutions.add_document(cover_key(entry_id, on_date), record)
    logger.info("Cover for period %s on %s given to %s.", entry_id, on_date, substitute_teacher_id)
    return record


def remove(entry_id: int, on_date: date) -> None:
    key = cover_key(entry_id, on_date)
    if not firestore_period_substitutions.get_document(key):
        raise HTTPException(status_code=404, detail="No cover is set for this period on that date.")
    firestore_period_substitutions.delete_document(key)


def cover_from_plan(request: dict, actor) -> list[dict]:
    """
    Turns an approved leave's cover plan into covers, for that leave's days only.

    Each class in the plan with a chosen substitute is covered on every date of the leave it
    falls on (the periods were snapshotted when the leave was made). A period that cannot be
    covered - the substitute is teaching then, or the period no longer runs - is returned as a
    gap with its reason instead of stopping the approval.
    """
    chosen = {
        int(item["timetable_entry_id"]): int(item["substitute_teacher_id"])
        for item in (request.get("cover_plan") or [])
        if item.get("substitute_teacher_id") is not None
    }
    gaps = []
    for period in request.get("affected_periods") or []:
        entry_id = period.get("timetable_entry_id")
        if entry_id is None or int(entry_id) not in chosen:
            continue
        try:
            create(
                int(entry_id),
                date.fromisoformat(str(period["date"])),
                chosen[int(entry_id)],
                actor=actor,
                note="Leave cover",
                leave_request_id=int(request["id"]),
            )
        except HTTPException as exc:
            gaps.append({
                "date": str(period["date"]),
                "class_name": period.get("class_name"),
                "subject_name": period.get("subject_name"),
                "start_time": period.get("start_time"),
                "reason": exc.detail,
            })
    return gaps


def drop_for_leave(leave_request_id: int) -> int:
    """Removes the covers an approved leave created, when that leave is withdrawn or cancelled."""
    dropped = 0
    for cover in firestore_period_substitutions.query_documents("leave_request_id", "==", int(leave_request_id)):
        firestore_period_substitutions.delete_document(
            cover_key(cover["timetable_entry_id"], date.fromisoformat(cover["on_date"]))
        )
        dropped += 1
    return dropped
