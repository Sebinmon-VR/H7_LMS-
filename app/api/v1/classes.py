"""
Joining a live class, requesting an extra one, and the calendar.

Three concerns that all answer "what am I doing, and when?":

  * `/classes/{id}/timing` and `/join` - the class clock and the gated join.
  * `/extra-classes` - a teacher asks for a slot outside the timetable; an admin decides.
  * `/calendar` - everything on one person's calendar, for the tile and calendar views.

The join endpoint is the one to read. It is the server-side half of the disabled join
button: the button is a courtesy, this is the rule. A student who bookmarks a Meet link and
opens it twenty minutes early gets nothing from here, which is the only place that can
actually be enforced.
"""

from datetime import date, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.v1.dependencies import (
    require_admin, require_any_authenticated, require_teacher,
)
from app.core.enums import ExtraClassStatus, Program, UserRole
from app.core.firebase import firestore_meetings
from app.schemas.calendar import CalendarDay, CalendarEvent, CalendarOut
from app.schemas.user import UserOut
from app.schemas.workflow import (
    ClassRoomAccessOut, ClassTimingOut, ExtraClassCreate, ExtraClassDecision, ExtraClassOut,
    JoinClassOut, JoinRoomOut,
)
from app.schemas.workflow import RoomPresenceOut
from app.services import calendar as calendar_service
from app.services import class_rooms
from app.services import extra_classes as extra_service
from app.services import live_classes
from app.services import permissions

router = APIRouter(prefix="/classes", tags=["Live Classes"])
extra_router = APIRouter(prefix="/extra-classes", tags=["Extra Classes"])
calendar_router = APIRouter(prefix="/calendar", tags=["Calendar"])


def _assert_may_see(meeting: dict, user: UserOut) -> None:
    """
    Whether this user is entitled to the class at all, before any timing question.

    Separate from `assert_may_join`, which answers "yet?". Conflating them gives a student
    from another class the message "this class has not started yet", which quietly confirms
    the class exists.
    """
    if user.role == UserRole.ADMIN:
        return

    class_id = meeting.get("class_id")
    if user.role in (UserRole.TEACHER, UserRole.CLASS_TEACHER):
        if int(meeting.get("teacher_id", -1)) == int(user.id):
            return
        if class_id is not None and permissions.is_class_teacher_of(user, class_id):
            return
        raise HTTPException(status_code=403, detail="This is not your class.")

    if user.role == UserRole.STUDENT:
        from app.core.firebase import firestore_student_enrollments
        mine = {
            int(e["class_id"]) for e in
            firestore_student_enrollments.query_documents("student_id", "==", int(user.id))
            if e.get("class_id") is not None
        }
        if class_id is not None and int(class_id) in mine:
            return

    raise HTTPException(status_code=404, detail="Meeting not found")


@router.get("/{meeting_id}/timing", response_model=ClassTimingOut)
def class_timing(meeting_id: int, user: UserOut = Depends(require_any_authenticated)):
    """
    The class clock: when it starts, whether it has, how long is left, and whether you may
    join right now.

    Computed server-side so a teacher's screen and a student's screen cannot disagree about
    when the class ends. Poll this for a live countdown; bind the join button to `may_join`
    and show `join_blocked_reason` when it is false.
    """
    meeting = live_classes.require_meeting(meeting_id)
    _assert_may_see(meeting, user)
    return ClassTimingOut(**live_classes.timing_view(meeting))


@router.post("/{meeting_id}/join", response_model=JoinClassOut)
def join_class(meeting_id: int, user: UserOut = Depends(require_any_authenticated)):
    """
    Enter a live class, returning the meeting link only if you actually may.

    409 with a readable reason when the class has not opened, has not been started by the
    teacher, or is over. Teachers and admins may open a class before it starts - somebody has
    to be first - but are refused once it has ended.
    """
    meeting = live_classes.require_meeting(meeting_id)
    _assert_may_see(meeting, user)
    timing = live_classes.assert_may_join(meeting, user)

    if meeting.get("class_id") is not None:
        class_rooms.log_event(
            meeting["class_id"], class_rooms.EVENT_JOINED_SESSION, user,
            meeting_id=meeting["id"], detail=meeting.get("title"),
        )

    return JoinClassOut(
        meeting_id=int(meeting["id"]),
        title=meeting.get("title") or "Live class",
        meeting_link=meeting.get("meeting_link"),
        timing=ClassTimingOut(**timing),
    )


@router.post("/{meeting_id}/start", response_model=ClassTimingOut)
def start_class(meeting_id: int, teacher: UserOut = Depends(require_teacher)):
    """
    Open the class, letting students in.

    Only needed when auto-start is off. With it on, the class opens on the timetable and this
    is a no-op. Idempotent - starting a running class returns it unchanged.

    `started_at` records the real time, so a class opened eight minutes late is recorded as
    such rather than as having started on schedule.
    """
    meeting = live_classes.require_meeting(meeting_id)
    _assert_may_see(meeting, teacher)
    started = live_classes.start_meeting(meeting, teacher.id)
    if meeting.get("class_id") is not None and started is not meeting:
        class_rooms.log_event(
            meeting["class_id"], class_rooms.EVENT_STARTED, teacher,
            meeting_id=meeting["id"], detail=meeting.get("title"),
        )
    return ClassTimingOut(**live_classes.timing_view(started))


@router.post("/{meeting_id}/end", response_model=ClassTimingOut)
def end_class(meeting_id: int, teacher: UserOut = Depends(require_teacher)):
    """
    Close the class. Students can no longer join, and the live badge clears from dashboards.
    """
    meeting = live_classes.require_meeting(meeting_id)
    _assert_may_see(meeting, teacher)
    ended = live_classes.end_meeting(meeting, teacher.id)
    if meeting.get("class_id") is not None and ended is not meeting:
        class_rooms.log_event(
            meeting["class_id"], class_rooms.EVENT_ENDED, teacher,
            meeting_id=meeting["id"], detail=meeting.get("title"),
        )
    return ClassTimingOut(**live_classes.timing_view(ended))


@router.get("/live", response_model=List[dict])
def my_live_classes(user: UserOut = Depends(require_any_authenticated)):
    """
    Classes that are open right now for this user - the dashboard's "join now" strip.

    Ordered by start time. Only classes the viewer may actually join appear, so a client can
    render the list without checking each one.
    """
    from app.core.firebase import firestore_student_enrollments

    if user.role == UserRole.STUDENT:
        class_ids = {
            int(e["class_id"]) for e in
            firestore_student_enrollments.query_documents("student_id", "==", int(user.id))
            if e.get("class_id") is not None
        }
        candidates = [
            m for m in firestore_meetings.list_all()
            if int(m.get("class_id", -1)) in class_ids
        ]
    elif user.role == UserRole.ADMIN:
        candidates = firestore_meetings.list_all()
    else:
        led = permissions.led_class_ids(user)
        candidates = [
            m for m in firestore_meetings.list_all()
            if int(m.get("teacher_id", -1)) == int(user.id)
            or int(m.get("class_id", -1)) in led
        ]

    live = []
    for meeting in candidates:
        timing = live_classes.timing_view(meeting)
        if not timing["is_live"] and not timing["join_window_open"]:
            continue
        if timing["is_closed"] or timing["is_expired"]:
            continue
        live.append({
            "meeting_id": int(meeting["id"]),
            "title": meeting.get("title"),
            "class_id": meeting.get("class_id"),
            "subject_id": meeting.get("subject_id"),
            "meeting_link": meeting.get("meeting_link") if timing["may_join"] else None,
            "timing": timing,
        })

    live.sort(key=lambda m: str(m["timing"]["scheduled_start_at"] or ""))
    return live


# ---------------------------------------------------------------------------------------
# Class rooms: the one standing Meet room per class
#
# The classroom model. A session (above) is one teacher's period; the room is where the
# whole class sits all day and where every period happens. Students are let in while any
# period is on; teachers and admins may always enter their class's room.
# ---------------------------------------------------------------------------------------

@router.get("/rooms/mine", response_model=List[ClassRoomAccessOut])
def my_class_rooms(user: UserOut = Depends(require_any_authenticated)):
    """
    The rooms this user belongs to, with today's periods and whether they may enter now.

    A student gets their class; a teacher every class they teach or lead; an admin all of
    them. Bind the join button to `may_join` and show `join_blocked_reason` when it is
    false. `room_link` is present only when the caller may enter.
    """
    return [
        ClassRoomAccessOut(**class_rooms.access_for(c, user))
        for c in class_rooms.visible_classes(user)
    ]


@router.get("/rooms/{class_id}", response_model=ClassRoomAccessOut)
def class_room_access(class_id: int, user: UserOut = Depends(require_any_authenticated)):
    """One class's room as this user sees it right now. Poll it for the day's clock."""
    class_room = class_rooms.require_class(class_id)
    class_rooms.assert_may_see(class_room, user)
    return ClassRoomAccessOut(**class_rooms.access_for(class_room, user))


@router.post("/rooms/{class_id}/join", response_model=JoinRoomOut)
def join_class_room(class_id: int, user: UserOut = Depends(require_any_authenticated)):
    """
    Enter the class's room, returning the link only if you actually may.

    A student is let in while any period of the class is on (from the join window before it
    to the grace period after) or any scheduled session is live; otherwise 409 with a
    readable reason. A teacher or admin may always enter, and if the class has no room yet
    one is created for them on the way in.
    """
    class_room = class_rooms.require_class(class_id)
    class_rooms.assert_may_see(class_room, user)
    return JoinRoomOut(**class_rooms.join_room(class_room, user))


@router.post("/rooms/{class_id}/leave", response_model=ClassRoomAccessOut)
def leave_class_room(class_id: int, user: UserOut = Depends(require_any_authenticated)):
    """
    Record that you have left the room.

    The LMS cannot see a Meet tab close, so leaving is something you tell it; the class's
    log then carries your join and leave times. Nothing is done to the Meet call itself.
    """
    class_room = class_rooms.require_class(class_id)
    class_rooms.assert_may_see(class_room, user)
    return ClassRoomAccessOut(**class_rooms.leave_room(class_room, user))


@router.get("/rooms/{class_id}/presence", response_model=RoomPresenceOut)
def class_room_presence(class_id: int, user: UserOut = Depends(require_any_authenticated)):
    """
    Who is in the room right now, by name - for the teachers of the class and the office.

    From the LMS log: a student is in when their last word today was a join. Poll it while
    a teacher's panel is open. A student may not see who else is in.
    """
    class_room = class_rooms.require_class(class_id)
    class_rooms.assert_may_see(class_room, user)
    if user.role == UserRole.STUDENT:
        raise HTTPException(status_code=403, detail="Only teachers and the office can see who is in the room.")
    return RoomPresenceOut(**class_rooms.presence_for_class(class_room))


# ---------------------------------------------------------------------------------------
# Extra classes
# ---------------------------------------------------------------------------------------

@extra_router.post("", response_model=ExtraClassOut, status_code=status.HTTP_201_CREATED)
def request_extra_class(payload: ExtraClassCreate, teacher: UserOut = Depends(require_teacher)):
    """
    Ask to hold a class outside the timetable.

    Goes to an administrator for approval. When the school has turned
    `extra_class_needs_approval` off in settings, it is approved on submission instead and
    this becomes a one-step "schedule an extra class" - the flow does not change shape.

    Refused if you already have another extra class overlapping the slot. A teacher may only
    request classes they are mapped to teach, or any class they lead.
    """
    return ExtraClassOut(
        **extra_service.present(extra_service.create_request(payload, teacher))
    )


@extra_router.get("", response_model=List[ExtraClassOut])
def list_extra_classes(
    status_filter: Optional[ExtraClassStatus] = Query(None, alias="status"),
    program: Optional[Program] = Query(None),
    mine_only: bool = Query(
        False, description="Only my own requests. Forced on for non-admins."
    ),
    user: UserOut = Depends(require_teacher),
):
    """
    Extra class requests, pending ones first - it is a queue.

    A teacher sees only their own requests whatever `mine_only` says; another teacher's
    pending request is not their business.
    """
    teacher_id = user.id if (mine_only or user.role != UserRole.ADMIN) else None
    requests = extra_service.list_requests(
        status=status_filter.value if status_filter else None,
        program=program.value if program else None,
        teacher_id=teacher_id,
    )
    return [ExtraClassOut(**extra_service.present(r)) for r in requests]


@extra_router.get("/{request_id}", response_model=ExtraClassOut)
def get_extra_class(request_id: int, user: UserOut = Depends(require_teacher)):
    """One extra class request."""
    request = extra_service.require_request(request_id)
    if user.role != UserRole.ADMIN and int(request.get("requested_by", -1)) != int(user.id):
        raise HTTPException(status_code=403, detail="This request was filed by somebody else.")
    return ExtraClassOut(**extra_service.present(request))


@extra_router.post("/{request_id}/decide", response_model=ExtraClassOut)
def decide_extra_class(
    request_id: int, payload: ExtraClassDecision, admin: UserOut = Depends(require_admin)
):
    """
    [Admin Only] Approve or reject a request.

    Records the decision only - the class is created by `/schedule`. The two are separate
    because creating it can fail on a clash or a Meet error long after you pressed approve,
    and a decision that rolls itself back because of an API error is worse than one that sits
    visibly in APPROVED awaiting a retry.

    A rejection needs a note: "no" with no reason is the message that gets escalated.
    """
    request = extra_service.require_request(request_id)
    decided = extra_service.decide(request, payload.approve, admin, payload.note)
    return ExtraClassOut(**extra_service.present(decided))


@extra_router.post("/{request_id}/schedule", response_model=ExtraClassOut)
def schedule_extra_class(request_id: int, admin: UserOut = Depends(require_admin)):
    """
    [Admin Only] Create the approved class - a school meeting with a real Meet link and the
    students invited, or a tuition session.

    Idempotent: a request that already produced a class returns it rather than making a
    second one.
    """
    request = extra_service.require_request(request_id)
    return ExtraClassOut(**extra_service.present(extra_service.materialise(request, admin)))


@extra_router.post("/{request_id}/cancel", response_model=ExtraClassOut)
def cancel_extra_class(request_id: int, user: UserOut = Depends(require_teacher)):
    """
    Withdraw a request. The teacher who filed it, or an admin, may do this.

    Kept distinct from a rejection in the status: the timetable outcome is the same and the
    conversation is not.
    """
    request = extra_service.require_request(request_id)
    return ExtraClassOut(**extra_service.present(extra_service.cancel(request, user)))


# ---------------------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------------------

@calendar_router.get("", response_model=CalendarOut)
def my_calendar(
    from_date: Optional[date] = Query(None, description="Defaults to today."),
    to_date: Optional[date] = Query(None, description="Defaults to 7 days ahead."),
    kinds: Optional[str] = Query(
        None,
        description="Comma-separated: TIMETABLE, LIVE_CLASS, TUITION_CLASS, EXAM, HOMEWORK.",
    ),
    group_by_day: bool = Query(
        False, description="Also return events bucketed per day - the tile view's shape."
    ),
    user: UserOut = Depends(require_any_authenticated),
):
    """
    Everything on my calendar: timetable periods, live classes, tuition, exams and homework.

    One flat event list, so a tile view and a calendar view render from the same response -
    pass `group_by_day=true` to get the per-day buckets as well, with empty days included so
    a month grid lines up.

    Role decides what is gathered. A student sees their class's periods and their own
    tuition; a teacher sees what they teach plus anything in a class they lead; a parent sees
    the union of their children's, each event tagged with whose it is.

    Ranges are capped at 90 days - expanding a recurring timetable further is thousands of
    synthesised events, and the request that asks for it is nearly always a client bug.
    """
    start = from_date or date.today()
    end = to_date or (start + timedelta(days=7))

    wanted = None
    if kinds:
        wanted = {k.strip().upper() for k in kinds.split(",") if k.strip()}

    payload = calendar_service.for_user(user, start, end, wanted)

    days = None
    if group_by_day:
        days = [CalendarDay(**d) for d in calendar_service.group_by_day(payload)]

    return CalendarOut(
        from_date=payload["from_date"],
        to_date=payload["to_date"],
        events=[CalendarEvent(**e) for e in payload["events"]],
        count=payload["count"],
        counts_by_kind=payload["counts_by_kind"],
        days=days,
    )
