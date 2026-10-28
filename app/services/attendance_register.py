"""
The register: one attendance mark per student for a class, a subject and a date.

Two ways in, one record. The Attendance page saves a whole register at once; the live class
offers each teacher their own period's register, filled in for them from who joined the
class room, to check, correct and save. Both land here, so a class, subject and date never
has two registers.

The suggestion is only ever a starting point - nothing is saved until the teacher saves it.
"""

from datetime import date, datetime, timedelta, timezone

from fastapi import HTTPException

from app.core.enums import AttendanceStatus, UserRole
from app.core.firebase import firestore_attendance, firestore_student_enrollments, firestore_users
from app.services import class_rooms
from app.services import timetable as timetable_service

# Joined more than this long after the period began: marked Late rather than Present.
LATE_AFTER_MINUTES = 10
# Being in the room this long before the period counts as being there for its start.
EARLY_MINUTES = 15


def _status_value(status) -> str:
    return getattr(status, "value", status)


def save_register(class_id: int, subject_id: int, day: date, items, teacher_id: int) -> list[dict]:
    """
    Saves (or updates) the marks for a class, subject and date. `items` carry `student_id`,
    `status` and `remarks`. Returns the stored records.
    """
    existing = {
        int(r["student_id"]): r
        for r in firestore_attendance.query_documents("class_id", "==", int(class_id))
        if r.get("subject_id") == int(subject_id) and r.get("date") == day.isoformat() and r.get("student_id") is not None
    }
    saved = []
    for item in items:
        student_id = int(item.student_id)
        before = existing.get(student_id)
        record = {
            "student_id": student_id,
            "class_id": int(class_id),
            "subject_id": int(subject_id),
            "teacher_id": int(teacher_id),
            "date": day.isoformat(),
            "status": _status_value(item.status),
            "remarks": item.remarks,
            "created_at": before.get("created_at") if before else datetime.utcnow().isoformat(),
        }
        if before:
            firestore_attendance.add_document(str(before["id"]), record)
            record["id"] = before["id"]
        else:
            new_id = firestore_attendance.get_next_numeric_id()
            firestore_attendance.add_document(str(new_id), record)
            record["id"] = new_id
        saved.append(record)
    return saved


def _naive_utc(moment: datetime) -> datetime:
    return moment.astimezone(timezone.utc).replace(tzinfo=None) if moment.tzinfo else moment


def _period_for(access: dict, user, now: datetime) -> dict | None:
    """
    The period this person is taking the register for: their current one, else the last of
    theirs that has begun today (a teacher saving the register just after the bell). An
    admin, who has no periods, takes the class's current one.
    """
    if user.role == UserRole.ADMIN:
        return access.get("current_period")
    if access.get("my_current_period"):
        return access["my_current_period"]
    mine = [
        p for p in (access.get("periods_today") or [])
        if (p.get("entry") or {}).get("teacher_id") == user.id and p["starts_at"] <= now
    ]
    return max(mine, key=lambda p: p["starts_at"]) if mine else None


def _sessions(events: list[dict]) -> dict[int, list[tuple[datetime, datetime | None]]]:
    """Each person's stretches in the room today, from the join and leave log."""
    events = sorted(
        (e for e in events if e.get("user_id") is not None and e.get("action") in class_rooms.PRESENCE_EVENTS),
        key=lambda e: str(e.get("at") or ""),
    )
    open_at: dict[int, datetime] = {}
    stretches: dict[int, list[tuple[datetime, datetime | None]]] = {}
    for event in events:
        uid = int(event["user_id"])
        at = class_rooms._parse_stored(event.get("at"))
        if at is None:
            continue
        if event["action"] in class_rooms.JOIN_EVENTS:
            open_at.setdefault(uid, at)
        elif uid in open_at:
            stretches.setdefault(uid, []).append((open_at.pop(uid), at))
    for uid, start in open_at.items():
        stretches.setdefault(uid, []).append((start, None))
    return stretches


def suggest(stretches: list[tuple[datetime, datetime | None]], starts: datetime, ends: datetime, now: datetime) -> dict:
    """What the room log says about one student for one period (all times naive UTC)."""
    window_start = starts - timedelta(minutes=EARLY_MINUTES)
    first_in = None
    minutes = 0.0
    for start, end in stretches:
        end = end or now
        overlap_start = max(start, window_start)
        overlap_end = min(end, ends)
        if overlap_end <= overlap_start:
            continue
        counted_start = max(start, starts)
        if overlap_end > counted_start:
            minutes += (overlap_end - counted_start).total_seconds() / 60.0
        first_in = start if first_in is None else min(first_in, start)
    if first_in is None:
        return {"suggested": AttendanceStatus.ABSENT.value, "joined_at": None, "minutes": 0.0}
    late = first_in > starts + timedelta(minutes=LATE_AFTER_MINUTES)
    return {
        "suggested": (AttendanceStatus.LATE if late else AttendanceStatus.PRESENT).value,
        "joined_at": first_in.replace(tzinfo=timezone.utc).isoformat(),
        "minutes": round(minutes, 1),
    }


def period_register(class_room: dict, user, reference: datetime | None = None) -> dict:
    """
    This teacher's period register for the class room: every student, the mark the room log
    suggests, and the mark already saved if the register was taken.
    """
    tz = timetable_service.school_timezone()
    now = reference or datetime.now(tz)
    if now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    access = class_rooms.access_for(class_room, user, reference=now)
    period = _period_for(access, user, now)
    if not period:
        raise HTTPException(
            status_code=409,
            detail="You have no period in this class today yet, so there is no register to take.",
        )

    entry = period.get("entry") or {}
    class_id = int(class_room["id"])
    subject_id = int(entry.get("subject_id"))
    day = period["starts_at"].astimezone(tz).date()
    starts = _naive_utc(period["starts_at"])
    ends = _naive_utc(period["ends_at"])
    now_utc = _naive_utc(now)

    day_start = _naive_utc(datetime.combine(day, datetime.min.time(), tzinfo=tz))
    stretches = _sessions(class_rooms.events_for_class(class_id, since=day_start, limit=5000))

    saved = {
        int(r["student_id"]): r
        for r in firestore_attendance.query_documents("class_id", "==", class_id)
        if r.get("subject_id") == subject_id and r.get("date") == day.isoformat() and r.get("student_id") is not None
    }
    student_ids = sorted({
        int(e["student_id"]) for e in firestore_student_enrollments.query_documents("class_id", "==", class_id)
        if e.get("student_id") is not None
    })
    users = firestore_users.get_documents(student_ids)

    rows = []
    for uid in student_ids:
        doc = users.get(uid) or users.get(str(uid)) or {}
        if doc.get("is_active") is False:
            continue
        # A period still running is judged up to now.
        guess = suggest(stretches.get(uid, []), starts, min(ends, now_utc), now_utc)
        record = saved.get(uid)
        rows.append({
            "student_id": uid,
            "name": doc.get("full_name") or f"Student {uid}",
            "suggested": guess["suggested"],
            "status": record.get("status") if record else guess["suggested"],
            "saved_status": record.get("status") if record else None,
            "remarks": record.get("remarks") if record else None,
            "joined_at": guess["joined_at"],
            "minutes": guess["minutes"],
            "in_room_now": any(end is None for _, end in stretches.get(uid, [])),
        })
    rows.sort(key=lambda r: r["name"].lower())

    saved_by = None
    if saved:
        teacher_id = next(iter(saved.values())).get("teacher_id")
        saved_by = (firestore_users.get_document(str(teacher_id)) or {}).get("full_name") if teacher_id else None

    return {
        "class_id": class_id,
        "class_name": class_room.get("name") or f"Class {class_id}",
        "subject_id": subject_id,
        "subject_name": (entry.get("subject") or {}).get("name"),
        "date": day.isoformat(),
        "starts_at": period["starts_at"],
        "ends_at": period["ends_at"],
        "taken": bool(saved),
        "taken_by": saved_by,
        "late_after_minutes": LATE_AFTER_MINUTES,
        "students": rows,
    }


def save_period_register(class_room: dict, user, subject_id: int, on_date: date, items) -> dict:
    """Saves a period register from the live class, after checking it is this teacher's to take."""
    current = period_register(class_room, user)
    if int(subject_id) != current["subject_id"] or on_date.isoformat() != current["date"]:
        raise HTTPException(
            status_code=409,
            detail="That register is not for your period. Reopen the register and save again.",
        )
    enrolled = {row["student_id"] for row in current["students"]}
    strangers = [item.student_id for item in items if int(item.student_id) not in enrolled]
    if strangers:
        raise HTTPException(status_code=400, detail="Some of those students are not in this class.")
    save_register(current["class_id"], current["subject_id"], on_date, items, user.id)
    return period_register(class_room, user)
