from datetime import date, datetime
from typing import Callable, List, Optional
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status, Query

from app.api.v1.dependencies import require_admin
from app.core import acs
from app.core import mailer
from app.core.concurrency import run_parallel
from app.core.config import settings
from app.core.enums import (
    UserRole, AttendanceStatus, DayOfWeek, Program,
    TEACHING_ROLE_VALUES, TEACHING_OR_ADMIN_VALUES, normalize_programs,
)
from app.core.storage import storage_service
from app.core.jobs import job_registry, report_cache
from app.core import identity
from app.core.firebase import (
    firestore_users, firestore_classes, firestore_subjects,
    firestore_teacher_mappings, firestore_class_teacher_mappings,
    firestore_student_enrollments,
    firestore_attendance, firestore_topics, firestore_meetings,
    firestore_materials, firestore_grades, firestore_timetable, firestore_reminder_log,
    firestore_recording_log, firestore_exams, firestore_exam_submissions,
    firestore_report_cards, firestore_parent_links,
    firestore_syllabus_chapters, firestore_question_bank, firestore_lab_assignments,
    hydrate_teacher_mapping, hydrate_class_teacher_mapping, hydrate_student_enrollment,
    hydrate_live_meeting, hydrate_study_material, hydrate_timetable_entry,
    require_document, delete_with_dependencies, prefetch_references, prefetch_academic
)
from app.services import accounts as account_service
from app.services import admissions as admission_service
from app.services import class_rooms as class_room_service
from app.services import families as family_service
from app.services import timetable as timetable_service
from app.services.content import (
    is_legacy_meet_link, material_update_fields, regenerate_meeting_link, resolve_teacher,
    schedule_meeting, store_material, store_material_link,
)
from app.services.recordings import (
    get_scheduler as get_recording_scheduler, harvest_meeting, sweep as run_recording_sweep,
)
from app.services.reminders import get_scheduler, sweep as run_reminder_sweep
from app.schemas.meeting import AdminLiveMeetingCreate, LiveMeetingUpdate, LiveMeetingOut
from app.schemas.material import AdminStudyMaterialLinkCreate, StudyMaterialUpdate, StudyMaterialOut
from app.schemas.timetable import (
    ScheduledPeriod, TimetableBulkCreate, TimetableBulkResult,
    TimetableEntryCreate, TimetableEntryOut, TimetableEntryUpdate,
)
from app.schemas.user import (
    UserCreate, UserUpdate, UserOut, UserDeleted,
    GenerateCredentialsRequest, CredentialsIssued
)
from app.schemas.academic import (
    ClassRoomCreate, ClassRoomUpdate, ClassRoomOut, ClassRoomSetup,
    SubjectCreate, SubjectUpdate, SubjectOut,
    TeacherMappingCreate, TeacherMappingOut,
    ClassTeacherMappingCreate, ClassTeacherMappingOut,
    StudentEnrollmentCreate, StudentEnrollmentOut
)
from app.schemas.reports import (
    SystemMonitoringReport, OverallStats,
    TeacherActivityReport, StudentPerformanceReport
)
from app.schemas.presence import UserPresenceOut
from app.schemas.workflow import ClassRoomAttendanceOut, ClassRoomEventOut, LiveClassBoardRow
from app.services import presence

# The profile fields a sibling match is made from. An edit touching any of them re-runs
# the household placement for a student not yet in one.
GUARDIAN_FIELDS = ("guardian_name", "guardian_phone", "guardian_email")

router = APIRouter(prefix="/admin", tags=["Admin Module"])


# Account creation and the profile helpers live in `app.services.accounts`, because the
# tuition module creates its own students and teachers and both products must do it
# identically. Aliased here under the names the rest of this router already uses.
_email_is_taken = account_service.email_is_taken
_assert_identifiers_free = account_service.assert_identifiers_free
_profile_fields = account_service.profile_fields


@router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_user(user_in: UserCreate, admin: UserOut = Depends(require_admin)):
    """
    [Admin Only] Create a new system user (Student, Teacher, or Admin).

    The password, when given, is stored as a hash in `user_credentials` (app/core/identity.py);
    the user signs in with it at `POST /auth/login`.

    Omit `email` to have one generated as firstname.lastname@<USER_EMAIL_DOMAIN>.
    Omit `password` to create the account without a credential, then issue one with
    `POST /admin/users/{user_id}/generate-credentials` to mail it to the user.

    Every profile field (phone, address, guardian and admission detail for students,
    employee and qualification detail for teachers) is optional, so the minimal
    `{full_name, role}` payload still works. `admission_number` and `employee_id` are
    rejected if already held by another user, and are generated for you when the school's
    identifier mode is AUTO and you leave them blank - see
    `PUT /admin/settings/LMS` to switch between AUTO and MANUAL or change the prefix.

    A student with no `academic_year_id` is admitted into the current session year, so the
    usual case needs no extra field. Nothing is defaulted when no year has been created yet.
    """
    admission_service.assert_admission_fields(
        user_in.academic_year_id, user_in.admission_category_id
    )
    account_service.issue_school_identifier(user_in, user_in.role)

    if user_in.role == UserRole.STUDENT and user_in.academic_year_id is None:
        current = admission_service.current_year(Program.LMS.value)
        if current:
            user_in.academic_year_id = int(current["id"])

    account = account_service.create_account(user_in)
    if user_in.role == UserRole.STUDENT:
        # A new student whose guardian is already on file joins that household now, rather
        # than waiting for the office to remember the sibling discount at invoice time.
        family_service.auto_place(account["id"], admin.id)
    return UserOut(**account)


@router.get("/users/presence", response_model=List[UserPresenceOut])
def list_user_presence(_: UserOut = Depends(require_admin)):
    """
    [Admin Only] Who is online: every user's last sign of life, online first.

    A user is online when the LMS heard from them in the last three minutes - any request,
    or the once-a-minute heartbeat an open tab sends. Nobody announces leaving, so offline
    is silence: a closed laptop shows as offline within about three minutes. Users with no
    row have not signed in since tracking began. Poll this; it is one small collection.
    """
    return [UserPresenceOut(**row) for row in presence.snapshot()]


@router.get("/users", response_model=List[UserOut])
def list_users(
    role: Optional[UserRole] = Query(None, description="Filter by role: STUDENT, TEACHER, ADMIN, PARENT"),
    academic_year_id: Optional[int] = Query(
        None, description="Students admitted into this session year."
    ),
    admission_category_id: Optional[int] = Query(None),
    class_id: Optional[int] = Query(
        None, description="Students enrolled in this class."
    ),
    search: Optional[str] = Query(
        None, description="Match on name, email or admission number."
    ),
    _: UserOut = Depends(require_admin)
):
    """
    [Admin Only] List users, with optional filters.

    `class_id` resolves through the enrollment rows rather than the profile, because class
    membership lives there - a student's class is a record of enrollment, not a field on the
    person.
    """
    if role:
        users = firestore_users.query_documents("role", "==", role.value)
    else:
        users = firestore_users.list_all()

    if academic_year_id is not None:
        users = [u for u in users
                 if str(u.get("academic_year_id")) == str(academic_year_id)]
    if admission_category_id is not None:
        users = [u for u in users
                 if str(u.get("admission_category_id")) == str(admission_category_id)]

    if class_id is not None:
        enrolled = {
            int(e["student_id"]) for e in firestore_student_enrollments.list_all()
            if int(e.get("class_id", -1)) == int(class_id) and e.get("student_id") is not None
        }
        users = [u for u in users if int(u.get("id", -1)) in enrolled]

    if search:
        needle = search.strip().lower()
        users = [
            u for u in users
            if needle in " ".join(
                str(u.get(f) or "") for f in ("full_name", "email", "admission_number")
            ).lower()
        ]

    users.sort(key=lambda u: str(u.get("full_name") or ""))
    return [UserOut(**u) for u in users]


@router.put("/users/{user_id}", response_model=UserOut)
def update_user(
    user_id: int,
    user_in: UserUpdate,
    admin: UserOut = Depends(require_admin)
):
    """
    [Admin Only] Update user details, profile fields, role, or active status.

    Partial: only the fields present in the body change. Sending `null` is not a way to
    clear a value - a form that defaults its untouched inputs to null would otherwise erase
    data the editor never looked at.
    """
    user = require_document(firestore_users, user_id, "User")

    admission_service.assert_admission_fields(
        user_in.academic_year_id, user_in.admission_category_id
    )

    updated = _profile_fields(user_in)

    if user_in.full_name is not None:
        updated["full_name"] = user_in.full_name
    if user_in.email is not None:
        updated["email"] = user_in.email.strip().lower()
    if user_in.is_active is not None:
        updated["is_active"] = user_in.is_active
    if user_in.role is not None:
        updated["role"] = user_in.role.value
    if user_in.programs is not None:
        # Replaces the list outright rather than merging. Product access is the kind of thing
        # an admin needs to be able to take away, and a merge would make revoking impossible.
        updated["programs"] = normalize_programs([p.value for p in user_in.programs])

    if not updated:
        raise HTTPException(status_code=400, detail="No fields provided to update")

    if "email" in updated and updated["email"] != user.get("email"):
        clash = firestore_users.get_document_by_field("email", updated["email"])
        if clash and int(clash["id"]) != user_id:
            raise HTTPException(status_code=400, detail="User with this email already exists")

    _assert_identifiers_free(updated, exclude_user_id=user_id)

    updated["updated_at"] = datetime.utcnow().isoformat()
    firestore_users.add_document(str(user_id), updated)

    # A guardian detail that now matches another student's makes them siblings. Only a
    # student not yet in a household is placed; an existing membership is never moved by an
    # edit, because that is a decision the office made and an edit is not a request to undo it.
    if (updated.get("role", user.get("role")) == UserRole.STUDENT.value
            and any(key in updated for key in GUARDIAN_FIELDS)):
        family_service.auto_place(user_id, admin.id)

    # A deactivated account is signed out everywhere at once, not when its token runs out.
    # Roles need nothing here: they are read from the profile on every request.
    if user_in.is_active is False and user.get("is_active", False):
        identity.revoke_sessions(user_id)

    return UserOut(**firestore_users.get_document(str(user_id)))


# Records that point at a user. Deleting the user permanently has to deal with each of
# these, or the system is left hydrating references to a profile that no longer exists.
def _user_dependencies(user_id: int) -> list[tuple[str, object, str]]:
    return [
        ("student enrollment(s)", firestore_student_enrollments, "student_id"),
        ("teacher mapping(s)", firestore_teacher_mappings, "teacher_id"),
        ("class teacher assignment(s)", firestore_class_teacher_mappings, "teacher_id"),
        ("timetable entry(s)", firestore_timetable, "teacher_id"),
        ("attendance record(s) as student", firestore_attendance, "student_id"),
        ("attendance record(s) as teacher", firestore_attendance, "teacher_id"),
        ("topic(s)", firestore_topics, "teacher_id"),
        ("meeting(s)", firestore_meetings, "teacher_id"),
        ("study material(s)", firestore_materials, "teacher_id"),
        ("exam grade(s) as student", firestore_grades, "student_id"),
        ("exam grade(s) as teacher", firestore_grades, "teacher_id"),
        ("exam(s) set", firestore_exams, "teacher_id"),
        ("exam submission(s)", firestore_exam_submissions, "student_id"),
        ("report card(s)", firestore_report_cards, "student_id"),
        # Both directions of a family link. A parent deleted without these keeps granting
        # access from a row nobody can see, and a student deleted without them leaves their
        # parent staring at a child that no longer resolves.
        ("parent link(s) as parent", firestore_parent_links, "parent_id"),
        ("parent link(s) as child", firestore_parent_links, "student_id"),
    ]


@router.delete("/users/{user_id}", response_model=UserDeleted)
def delete_user(
    user_id: int,
    permanent: bool = Query(
        False,
        description="Erase the user entirely instead of deactivating. Irreversible.",
    ),
    force: bool = Query(
        False,
        description="With permanent=true, also delete every record referencing the user",
    ),
    current_user: UserOut = Depends(require_admin),
):
    """
    [Admin Only] Remove a user account.

    **Default (soft delete)** deactivates: the profile stays, attendance, topic and grade
    history keep resolving to a name, and the account can no longer sign in: every session it
    holds is revoked at once. `POST /admin/users/{id}/reactivate` reverses it.

    **`?permanent=true`** erases the profile and its stored password. It refuses with
    `409` while anything still references the user, listing what and how many — add
    `&force=true` to delete those records too. This is irreversible and will leave gaps in
    historical reports, which is exactly why it is neither the default nor silent.

    An admin cannot permanently delete their own account, and the last remaining active
    admin cannot be removed by either mode; locking everyone out of the system is never the
    intended outcome of a delete.
    """
    user = require_document(firestore_users, user_id, "User")

    if permanent and user_id == current_user.id:
        raise HTTPException(
            status_code=400,
            detail="You cannot permanently delete the account you are signed in with.",
        )

    if user.get("role") == UserRole.ADMIN.value and user.get("is_active", False):
        other_admins = [
            u for u in firestore_users.query_documents("role", "==", UserRole.ADMIN.value)
            if int(u["id"]) != user_id and u.get("is_active", False)
        ]
        if not other_admins:
            raise HTTPException(
                status_code=409,
                detail=(
                    "This is the only active administrator. Create or reactivate another "
                    "admin before removing this one, or nobody can administer the system."
                ),
            )

    if not permanent:
        firestore_users.add_document(str(user_id), {
            "is_active": False,
            "updated_at": datetime.utcnow().isoformat(),
        })
        identity.revoke_sessions(user_id)

        return UserDeleted(
            user_id=user_id,
            email=user.get("email"),
            full_name=user.get("full_name"),
            mode="SOFT",
            detail=(
                "User deactivated. History is preserved and every session is signed "
                "out. Use POST /admin/users/{id}/reactivate to restore access, or "
                "?permanent=true to erase the account."
            ),
        )

    # Permanent: find everything pointing at this user before touching anything.
    dependencies = _user_dependencies(user_id)
    found = run_parallel({
        label: (lambda s=service, f=field: s.query_documents(f, "==", user_id))
        for label, service, field in dependencies
    })

    referencing = {label: (found.get(label) or []) for label, _, _ in dependencies}
    blocking = {label: len(records) for label, records in referencing.items() if records}

    if blocking and not force:
        summary = ", ".join(f"{count} {label}" for label, count in blocking.items())
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cannot permanently delete this user because they are still referenced by: "
                f"{summary}. Retry with &force=true to delete those records too, or use the "
                "default soft delete to keep the history."
            ),
        )

    services_by_label = {label: service for label, service, _ in dependencies}
    deletions = {
        f"{label}:{record['id']}":
            (lambda s=services_by_label[label], r=record: s.delete_document(str(r["id"])))
        for label, records in referencing.items()
        for record in records
    }
    if deletions:
        run_parallel(deletions)

    had_password = identity.has_password(user_id)
    login_deleted = identity.delete_credentials(user_id) if had_password else True

    firestore_users.delete_document(str(user_id))

    detail = f"User {user.get('email') or user_id} permanently deleted."
    if blocking:
        detail += f" {sum(blocking.values())} referencing record(s) were removed."
    if not login_deleted:
        detail += " Their stored password could not be removed; the account no longer exists, so it cannot be used."

    return UserDeleted(
        user_id=user_id,
        email=user.get("email"),
        full_name=user.get("full_name"),
        mode="PERMANENT",
        login_deleted=login_deleted,
        deleted_records=blocking,
        detail=detail,
    )


@router.post("/users/{user_id}/reactivate", response_model=UserOut)
def reactivate_user(user_id: int, _: UserOut = Depends(require_admin)):
    """
    [Admin Only] Re-enable a previously deactivated user account.
    """
    require_document(firestore_users, user_id, "User")

    firestore_users.add_document(str(user_id), {"is_active": True})

    return UserOut(**firestore_users.get_document(str(user_id)))


@router.post("/users/{user_id}/generate-credentials", response_model=CredentialsIssued)
def generate_user_credentials(
    user_id: int,
    options: GenerateCredentialsRequest = GenerateCredentialsRequest(),
    _: UserOut = Depends(require_admin)
):
    """
    [Admin Only] Generate a password for an existing user and email it to them.

    Backs the "Generate user credentials" action: pick a user, press the button, and they
    receive their email and password. Safe to run again to reset a forgotten password.

    The generated password is returned once in the response so the admin can display or
    relay it; only its hash is stored, so it cannot be retrieved later.
    """
    user = require_document(firestore_users, user_id, "User")
    # Shared with the admission flow, which issues a new student's login the same way.
    return CredentialsIssued(**account_service.issue_credentials(
        user, send_email=options.send_email, revoke_sessions=options.revoke_sessions,
    ))


@router.post("/classes", response_model=ClassRoomOut, status_code=status.HTTP_201_CREATED)
def create_class_room(class_in: ClassRoomCreate, _: UserOut = Depends(require_admin)):
    """
    [Admin Only] Create a new Class/Section.
    """
    existing = firestore_classes.get_document_by_field("code", class_in.code)
    if existing:
        raise HTTPException(status_code=400, detail="Class with this code already exists")

    class_id = firestore_classes.get_next_numeric_id()
    class_data = {
        "name": class_in.name,
        "code": class_in.code,
        "description": class_in.description
    }
    firestore_classes.add_document(str(class_id), class_data)
    class_data["id"] = class_id
    return ClassRoomOut(**class_data)


@router.get("/classes", response_model=List[ClassRoomOut])
def list_classes(_: UserOut = Depends(require_admin)):
    """
    [Admin Only] List all classes.
    """
    classes = firestore_classes.list_all()
    return [ClassRoomOut(**c) for c in classes]


@router.get("/live-classes", response_model=List[LiveClassBoardRow])
def live_class_board(admin: UserOut = Depends(require_admin)):
    """
    [Admin Only] Every class as the office sees it right now.

    The room, the current and next period, how many students have come in today, whether
    a teacher is in, and any scheduled session that is live. Classes in session sort first.
    Computed on request from the timetable, the sessions and the room log; poll it for a
    live board. `room_link` is included, so the admin can join or open a preview window.
    """
    return [LiveClassBoardRow(**row) for row in class_room_service.live_board(admin)]


@router.get("/live-classes/{class_id}/events", response_model=List[ClassRoomEventOut])
def live_class_events(
    class_id: int,
    today_only: bool = Query(True, description="Only today's log (school day)."),
    limit: int = Query(200, ge=1, le=1000),
    _: UserOut = Depends(require_admin),
):
    """
    [Admin Only] A class's room log, newest first: joins and leaves (reported by the call
    itself for an LMS room), periods opened and closed, room changes.
    """
    require_document(firestore_classes, class_id, "ClassRoom")
    since = None
    if today_only:
        from app.services import timetable as timetable_service

        since = class_room_service._school_day_start_utc(
            datetime.now(timetable_service.school_timezone())
        )
    return [
        ClassRoomEventOut(**{"id": e.get("id"), **e})
        for e in class_room_service.events_for_class(class_id, since=since, limit=limit)
    ]


@router.get("/live-classes/{class_id}/attendance", response_model=List[ClassRoomAttendanceOut])
def live_class_attendance(
    class_id: int,
    on_date: Optional[date] = Query(None, description="School day. Defaults to today."),
    sync: bool = Query(False, description="Rebuild from the room log now rather than waiting for the sweep."),
    _: UserOut = Depends(require_admin),
):
    """
    [Admin Only] Who was in the class's room: each person with their join and leave times
    and total minutes, for one school day.

    Built from the room log, which the in-LMS call writes on every connect and hang-up, and
    rebuilt by the maintenance sweep every few minutes.
    """
    from app.services import timetable as timetable_service

    class_room = require_document(firestore_classes, class_id, "ClassRoom")
    day = on_date or datetime.now(timetable_service.school_timezone()).date()
    if sync:
        rows, error = class_room_service.sync_room_attendance(class_room, on_date=day, force=True)
        if error and not rows:
            raise HTTPException(status_code=502, detail=error)
    else:
        rows = class_room_service.attendance_for_class(class_id, day)
    return [ClassRoomAttendanceOut(**{"id": r.get("id"), **r}) for r in rows]


@router.post("/classes/rooms/prepare")
def prepare_class_rooms(
    dry_run: bool = Query(False, description="Report what would be created without creating it."),
    _: UserOut = Depends(require_admin),
):
    """
    [Admin Only] Make today's class rooms ready now, rather than waiting for the sweep.

    The maintenance sweep does this on its own every few minutes: a class with lessons today
    gets its room shortly before the first one. This runs the same pass immediately and
    returns what it did - useful after switching the classroom model on, or to see why a
    room was not created.
    """
    return class_room_service.ensure_rooms_for_today(dry_run=dry_run)


@router.post("/classes/{class_id}/room", response_model=ClassRoomOut)
def setup_class_room(
    class_id: int,
    payload: ClassRoomSetup = ClassRoomSetup(),
    admin: UserOut = Depends(require_admin),
):
    """
    [Admin Only] Give a class its standing live-class room.

    The school runs one live-class room per class, inside the LMS: students join it and
    stay, and each subject teacher joins at their timetabled period. Leave the body empty to
    create it; `manual_link` records an outside link the school already has instead. An
    existing room is kept unless `replace` is set, which gives the class a fresh room.

    Rooms are also created on their own the first time a session is scheduled for a class,
    so this is for setting one up ahead of time, or repairing one.
    """
    class_room = require_document(firestore_classes, class_id, "ClassRoom")
    if payload.manual_link:
        updated = class_room_service.set_manual_room(class_room, payload.manual_link, admin)
    else:
        updated, error = class_room_service.create_room(
            class_room, admin, owner_id=payload.owner_id,
            auto_record=payload.auto_record, replace=payload.replace,
        )
        if error:
            raise HTTPException(status_code=502, detail=error)
    return ClassRoomOut(**updated)


@router.delete("/classes/{class_id}/room", response_model=ClassRoomOut)
def clear_class_room(class_id: int, admin: UserOut = Depends(require_admin)):
    """
    [Admin Only] Remove a class's room. Its live-class room is deleted, so the old link stops
    working. Sessions already scheduled keep the link they were given.
    """
    class_room = require_document(firestore_classes, class_id, "ClassRoom")
    return ClassRoomOut(**class_room_service.clear_room(class_room, admin))


@router.put("/classes/{class_id}", response_model=ClassRoomOut)
def update_class_room(
    class_id: int,
    class_in: ClassRoomUpdate,
    _: UserOut = Depends(require_admin)
):
    """
    [Admin Only] Update a Class/Section name, code, or description.
    """
    require_document(firestore_classes, class_id, "ClassRoom")

    updated = class_in.model_dump(exclude_unset=True, exclude_none=True)
    if not updated:
        raise HTTPException(status_code=400, detail="No fields provided to update")

    if "code" in updated:
        clash = firestore_classes.get_document_by_field("code", updated["code"])
        if clash and int(clash["id"]) != class_id:
            raise HTTPException(status_code=400, detail="Class with this code already exists")

    firestore_classes.add_document(str(class_id), updated)
    return ClassRoomOut(**firestore_classes.get_document(str(class_id)))


@router.delete("/classes/{class_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_class_room(
    class_id: int,
    force: bool = Query(False, description="Cascade delete every record referencing this class"),
    _: UserOut = Depends(require_admin)
):
    """
    [Admin Only] Delete a Class/Section.
    Refuses with 409 while enrollments, teacher mappings, or academic records still reference it,
    unless `force=true` is supplied to cascade the delete.

    Cascading also removes the class-teacher assignments, and any teacher left leading no
    class at all drops back to the plain `TEACHER` role.
    """
    # The room goes with the class. Best-effort, before the cascade.
    existing = firestore_classes.get_document(str(class_id))
    if existing:
        class_room_service.drop_calendar_event(existing)

    # Read before the cascade: afterwards there is no record of who used to lead this class,
    # and they would keep a CLASS_TEACHER role pointing at nothing.
    led_by = [
        m.get("teacher_id")
        for m in firestore_class_teacher_mappings.query_documents("class_id", "==", class_id)
    ]

    delete_with_dependencies(
        firestore_classes, class_id, "ClassRoom",
        dependencies=[
            ("student enrollment(s)", firestore_student_enrollments, "class_id"),
            ("teacher mapping(s)", firestore_teacher_mappings, "class_id"),
            ("class teacher assignment(s)", firestore_class_teacher_mappings, "class_id"),
            ("attendance record(s)", firestore_attendance, "class_id"),
            ("topic(s)", firestore_topics, "class_id"),
            ("meeting(s)", firestore_meetings, "class_id"),
            ("study material(s)", firestore_materials, "class_id"),
            ("exam grade(s)", firestore_grades, "class_id"),
            ("exam(s)", firestore_exams, "class_id"),
            ("exam submission(s)", firestore_exam_submissions, "class_id"),
            ("report card(s)", firestore_report_cards, "class_id"),
            ("syllabus chapter(s)", firestore_syllabus_chapters, "class_id"),
            ("arena question(s)", firestore_question_bank, "class_id"),
            ("lab assignment(s)", firestore_lab_assignments, "class_id"),
        ],
        force=force,
    )

    for teacher_id in led_by:
        sync_class_teacher_role(teacher_id)

    return None


@router.post("/subjects", response_model=SubjectOut, status_code=status.HTTP_201_CREATED)
def create_subject(subject_in: SubjectCreate, _: UserOut = Depends(require_admin)):
    """
    [Admin Only] Create a new Subject.
    """
    existing = firestore_subjects.get_document_by_field("code", subject_in.code)
    if existing:
        raise HTTPException(status_code=400, detail="Subject with this code already exists")

    subject_id = firestore_subjects.get_next_numeric_id()
    subject_data = {
        "name": subject_in.name,
        "code": subject_in.code,
        "description": subject_in.description
    }
    firestore_subjects.add_document(str(subject_id), subject_data)
    subject_data["id"] = subject_id
    return SubjectOut(**subject_data)


@router.get("/subjects", response_model=List[SubjectOut])
def list_subjects(_: UserOut = Depends(require_admin)):
    """
    [Admin Only] List all subjects.
    """
    subjects = firestore_subjects.list_all()
    return [SubjectOut(**s) for s in subjects]


@router.put("/subjects/{subject_id}", response_model=SubjectOut)
def update_subject(
    subject_id: int,
    subject_in: SubjectUpdate,
    _: UserOut = Depends(require_admin)
):
    """
    [Admin Only] Update a Subject name, code, or description.
    """
    require_document(firestore_subjects, subject_id, "Subject")

    updated = subject_in.model_dump(exclude_unset=True, exclude_none=True)
    if not updated:
        raise HTTPException(status_code=400, detail="No fields provided to update")

    if "code" in updated:
        clash = firestore_subjects.get_document_by_field("code", updated["code"])
        if clash and int(clash["id"]) != subject_id:
            raise HTTPException(status_code=400, detail="Subject with this code already exists")

    firestore_subjects.add_document(str(subject_id), updated)
    return SubjectOut(**firestore_subjects.get_document(str(subject_id)))


@router.delete("/subjects/{subject_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_subject(
    subject_id: int,
    force: bool = Query(False, description="Cascade delete every record referencing this subject"),
    _: UserOut = Depends(require_admin)
):
    """
    [Admin Only] Delete a Subject.
    Refuses with 409 while teacher mappings or academic records still reference it,
    unless `force=true` is supplied to cascade the delete.
    """
    delete_with_dependencies(
        firestore_subjects, subject_id, "Subject",
        dependencies=[
            ("teacher mapping(s)", firestore_teacher_mappings, "subject_id"),
            ("attendance record(s)", firestore_attendance, "subject_id"),
            ("topic(s)", firestore_topics, "subject_id"),
            ("meeting(s)", firestore_meetings, "subject_id"),
            ("study material(s)", firestore_materials, "subject_id"),
            ("exam grade(s)", firestore_grades, "subject_id"),
            ("exam(s)", firestore_exams, "subject_id"),
            ("exam submission(s)", firestore_exam_submissions, "subject_id"),
            ("syllabus chapter(s)", firestore_syllabus_chapters, "subject_id"),
            ("arena question(s)", firestore_question_bank, "subject_id"),
        ],
        force=force,
    )
    return None


@router.post("/mappings/teacher-subject-class", response_model=TeacherMappingOut, status_code=status.HTTP_201_CREATED)
def map_teacher_to_class(
    mapping_in: TeacherMappingCreate,
    _: UserOut = Depends(require_admin)
):
    """
    [Admin Only] Assign a Teacher to teach a specific Subject in a Class.
    """
    teacher = firestore_users.get_document(str(mapping_in.teacher_id))
    # A class teacher still takes subjects, so both teaching roles are valid here. Testing
    # against UserRole.TEACHER alone would refuse to give a promoted teacher new periods.
    if not teacher or teacher.get("role") not in TEACHING_ROLE_VALUES:
        raise HTTPException(status_code=404, detail="Teacher not found or specified user is not a teacher")

    subject = firestore_subjects.get_document(str(mapping_in.subject_id))
    if not subject:
        raise HTTPException(status_code=404, detail="Subject not found")

    class_room = firestore_classes.get_document(str(mapping_in.class_id))
    if not class_room:
        raise HTTPException(status_code=404, detail="ClassRoom not found")

    duplicates = [m for m in firestore_teacher_mappings.query_documents("teacher_id", "==", mapping_in.teacher_id)
                  if m.get("subject_id") == mapping_in.subject_id and m.get("class_id") == mapping_in.class_id]
    if duplicates:
        return TeacherMappingOut(**hydrate_teacher_mapping(duplicates[0]))

    mapping_id = firestore_teacher_mappings.get_next_numeric_id()
    mapping_data = {
        "teacher_id": mapping_in.teacher_id,
        "subject_id": mapping_in.subject_id,
        "class_id": mapping_in.class_id
    }
    firestore_teacher_mappings.add_document(str(mapping_id), mapping_data)
    mapping_data["id"] = mapping_id

    return TeacherMappingOut(**hydrate_teacher_mapping(mapping_data))


@router.get("/mappings/teacher-subject-class", response_model=List[TeacherMappingOut])
def list_teacher_mappings(_: UserOut = Depends(require_admin)):
    """
    [Admin Only] View all Teacher <-> Subject <-> Class mappings.
    """
    mappings = firestore_teacher_mappings.list_all()
    prefetch_references(
        mappings,
        ("teacher_id", firestore_users),
        ("subject_id", firestore_subjects),
        ("class_id", firestore_classes),
    )
    return [TeacherMappingOut(**hydrate_teacher_mapping(m)) for m in mappings]


@router.delete("/mappings/teacher-subject-class/{mapping_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_teacher_mapping(mapping_id: int, _: UserOut = Depends(require_admin)):
    """
    [Admin Only] Remove a Teacher <-> Subject <-> Class assignment.
    Academic records the teacher already logged are left intact.
    """
    require_document(firestore_teacher_mappings, mapping_id, "Teacher mapping")
    firestore_teacher_mappings.delete_document(str(mapping_id))
    return None


def sync_class_teacher_role(teacher_id: int | None) -> str | None:
    """
    Brings a teacher's role label back into line with their class-teacher mappings.

    The mappings are the authority for what a class teacher may do; the role exists only so
    the frontend can gate navigation straight from the profile (`/auth/me`, or the `role`
    claim in the session token when that is unreachable). Running this after
    every assign and unassign is what stops the two from drifting into the state
    `app.services.permissions` warns about - a role reading CLASS_TEACHER while no mapping
    says which class, or a teacher still leading 9-A whose token says plain TEACHER.

    Admins are deliberately left untouched. An admin already outranks a class teacher
    everywhere, so rewriting their role would cost them the rest of the system to grant
    authority they had anyway.

    Returns the role now held, or None when there was no non-admin user to sync.
    """
    if teacher_id is None:
        return None

    user = firestore_users.get_document(str(teacher_id))
    if not user or user.get("role") == UserRole.ADMIN.value:
        return None

    leads = bool(firestore_class_teacher_mappings.query_documents("teacher_id", "==", teacher_id))
    target = UserRole.CLASS_TEACHER if leads else UserRole.TEACHER

    if user.get("role") == target.value:
        return target.value

    firestore_users.add_document(str(teacher_id), {
        "role": target.value,
        "updated_at": datetime.utcnow().isoformat(),
    })

    return target.value


@router.post(
    "/mappings/class-teacher",
    response_model=ClassTeacherMappingOut,
    status_code=status.HTTP_201_CREATED,
)
def assign_class_teacher(
    mapping_in: ClassTeacherMappingCreate,
    _: UserOut = Depends(require_admin)
):
    """
    [Admin Only] Make a teacher the class teacher of a class.

    This is broader than a subject mapping and deliberately carries no `subject_id`: a class
    teacher answers for the class as a whole, so they may read and correct the attendance,
    topics, grades, meetings and study materials that **every** teacher files against that
    class, not only their own periods. Authority stays scoped to the classes assigned here —
    leading 9-A grants nothing over 10-B.

    The teacher's role is promoted to `CLASS_TEACHER`, which applies from their next request
    (roles are read from the profile, not the token), so the frontend can gate navigation
    from `/auth/me` without another call. Their existing subject mappings,
    periods and records are untouched; a class teacher is still an ordinary teacher elsewhere.

    A class may have several class teachers (joint or relief arrangements), and re-posting an
    assignment that already exists returns it unchanged instead of duplicating it.
    """
    teacher = firestore_users.get_document(str(mapping_in.teacher_id))
    if not teacher:
        raise HTTPException(status_code=404, detail="Teacher not found")

    if teacher.get("role") == UserRole.ADMIN.value:
        raise HTTPException(
            status_code=400,
            detail=(
                f"User {mapping_in.teacher_id} is an administrator and already has full access "
                "to every class, so assigning them as class teacher would change nothing."
            ),
        )

    if teacher.get("role") not in TEACHING_ROLE_VALUES:
        raise HTTPException(
            status_code=400,
            detail=f"User {mapping_in.teacher_id} is not a teacher and cannot lead a class.",
        )

    require_document(firestore_classes, mapping_in.class_id, "ClassRoom")

    duplicates = [
        m for m in firestore_class_teacher_mappings.query_documents(
            "teacher_id", "==", mapping_in.teacher_id
        )
        if m.get("class_id") == mapping_in.class_id
    ]
    if duplicates:
        return ClassTeacherMappingOut(**hydrate_class_teacher_mapping(duplicates[0]))

    mapping_id = firestore_class_teacher_mappings.get_next_numeric_id()
    mapping_data = {
        "teacher_id": mapping_in.teacher_id,
        "class_id": mapping_in.class_id,
        "assigned_at": datetime.utcnow().isoformat(),
    }
    firestore_class_teacher_mappings.add_document(str(mapping_id), mapping_data)
    mapping_data["id"] = mapping_id

    sync_class_teacher_role(mapping_in.teacher_id)
    return ClassTeacherMappingOut(**hydrate_class_teacher_mapping(mapping_data))


@router.get("/mappings/class-teacher", response_model=List[ClassTeacherMappingOut])
def list_class_teacher_mappings(
    class_id: Optional[int] = Query(None, description="Only the class teacher(s) leading this class"),
    teacher_id: Optional[int] = Query(None, description="Only the classes led by this teacher"),
    _: UserOut = Depends(require_admin)
):
    """
    [Admin Only] View which teacher is class teacher of which class.

    Filter by `class_id` to answer "who leads 9-A", or by `teacher_id` for "what does this
    teacher lead". Unfiltered, this is the full class-teacher chart.
    """
    if class_id is not None:
        mappings = firestore_class_teacher_mappings.query_documents("class_id", "==", class_id)
        if teacher_id is not None:
            mappings = [m for m in mappings if m.get("teacher_id") == teacher_id]
    elif teacher_id is not None:
        mappings = firestore_class_teacher_mappings.query_documents("teacher_id", "==", teacher_id)
    else:
        mappings = firestore_class_teacher_mappings.list_all()

    prefetch_references(
        mappings,
        ("teacher_id", firestore_users),
        ("class_id", firestore_classes),
    )
    return [ClassTeacherMappingOut(**hydrate_class_teacher_mapping(m)) for m in mappings]


@router.delete("/mappings/class-teacher/{mapping_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_class_teacher_mapping(mapping_id: int, _: UserOut = Depends(require_admin)):
    """
    [Admin Only] Remove a class teacher assignment.

    The teacher keeps their subject mappings, periods and everything they logged; they simply
    stop being answerable for the class, and lose the cross-teacher visibility that came with
    it. If this was their last class, their role drops back to `TEACHER`, which applies from
    their next request, so the frontend stops offering the class-teacher screens.
    """
    mapping = require_document(firestore_class_teacher_mappings, mapping_id, "Class teacher mapping")
    firestore_class_teacher_mappings.delete_document(str(mapping_id))
    sync_class_teacher_role(mapping.get("teacher_id"))
    return None


@router.post("/enrollments", response_model=StudentEnrollmentOut, status_code=status.HTTP_201_CREATED)
def enroll_student(
    enroll_in: StudentEnrollmentCreate,
    _: UserOut = Depends(require_admin)
):
    """
    [Admin Only] Enroll a Student into a Class.
    """
    student = firestore_users.get_document(str(enroll_in.student_id))
    if not student or UserRole(student.get("role")) != UserRole.STUDENT:
        raise HTTPException(status_code=404, detail="Student not found or specified user is not a student")

    class_room = firestore_classes.get_document(str(enroll_in.class_id))
    if not class_room:
        raise HTTPException(status_code=404, detail="ClassRoom not found")

    duplicates = [e for e in firestore_student_enrollments.query_documents("student_id", "==", enroll_in.student_id)
                  if e.get("class_id") == enroll_in.class_id]
    if duplicates:
        return StudentEnrollmentOut(**hydrate_student_enrollment(duplicates[0]))

    enrollment_id = firestore_student_enrollments.get_next_numeric_id()
    enrollment_data = {
        "student_id": enroll_in.student_id,
        "class_id": enroll_in.class_id,
        "enrolled_at": datetime.utcnow().isoformat()
    }
    firestore_student_enrollments.add_document(str(enrollment_id), enrollment_data)
    enrollment_data["id"] = enrollment_id
    return StudentEnrollmentOut(**hydrate_student_enrollment(enrollment_data))


@router.get("/enrollments", response_model=List[StudentEnrollmentOut])
def list_enrollments(_: UserOut = Depends(require_admin)):
    """
    [Admin Only] View all student class enrollments.
    """
    enrollments = firestore_student_enrollments.list_all()
    prefetch_references(
        enrollments,
        ("student_id", firestore_users),
        ("class_id", firestore_classes),
    )
    return [StudentEnrollmentOut(**hydrate_student_enrollment(e)) for e in enrollments]


@router.delete("/enrollments/{enrollment_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_enrollment(enrollment_id: int, _: UserOut = Depends(require_admin)):
    """
    [Admin Only] Un-enroll a student from a class.
    Attendance and grade history for the student is preserved.
    """
    require_document(firestore_student_enrollments, enrollment_id, "Enrollment")
    firestore_student_enrollments.delete_document(str(enrollment_id))
    return None


@router.post("/timetable", response_model=TimetableEntryOut, status_code=status.HTTP_201_CREATED)
def create_timetable_entry(
    entry_in: TimetableEntryCreate,
    allow_conflicts: bool = Query(
        False, description="Schedule the period even if it clashes with an existing one"
    ),
    _: UserOut = Depends(require_admin),
):
    """
    [Admin Only] Add one recurring period to the timetable.

    An entry recurs weekly on `day_of_week` between `effective_from` and `effective_to`, so
    a term's schedule is a few dozen rows rather than one per calendar day.

    Omit `teacher_id` to use whoever is already mapped to that subject and class. The
    request is refused with `409` if it double-books the class or the teacher, listing the
    clashing entries; `?allow_conflicts=true` overrides that.
    """
    entry = timetable_service.create_entry(entry_in, allow_conflicts=allow_conflicts)
    return TimetableEntryOut(**hydrate_timetable_entry(entry))


@router.post("/timetable/bulk", response_model=TimetableBulkResult, status_code=status.HTTP_201_CREATED)
def bulk_create_timetable(
    payload: TimetableBulkCreate,
    allow_conflicts: bool = Query(
        False, description="Insert entries even when they clash with an existing period"
    ),
    _: UserOut = Depends(require_admin),
):
    """
    [Admin Only] Upload a whole week or term in one request.

    Building a forty-period timetable one call at a time means forty round trips and forty
    chances to leave a class half-scheduled. Set `replace_existing` to wipe the current
    timetable for every class named in the payload before inserting, which makes re-uploading
    a corrected schedule idempotent instead of doubling it up.

    Rows that cannot be created are reported in `skipped` with the reason; the rest still
    go in, so one bad row does not cost you the other thirty-nine.
    """
    replaced = 0
    if payload.replace_existing:
        class_ids = {entry.class_id for entry in payload.entries}
        for class_id in class_ids:
            for existing in firestore_timetable.query_documents("class_id", "==", class_id):
                firestore_timetable.delete_document(str(existing["id"]))
                replaced += 1

    created: list[dict] = []
    skipped: list[dict] = []

    for index, entry_in in enumerate(payload.entries):
        try:
            created.append(timetable_service.create_entry(entry_in, allow_conflicts=allow_conflicts))
        except HTTPException as exc:
            skipped.append({"index": index, "reason": exc.detail})

    hydrated = timetable_service.hydrate_many(created)
    return TimetableBulkResult(
        created=[TimetableEntryOut(**item) for item in hydrated],
        replaced=replaced,
        skipped=skipped,
        detail=(
            f"{len(created)} period(s) created"
            + (f", {replaced} replaced" if replaced else "")
            + (f", {len(skipped)} skipped" if skipped else "")
            + "."
        ),
    )


@router.get("/timetable", response_model=List[TimetableEntryOut])
def list_timetable(
    class_id: Optional[int] = Query(None),
    teacher_id: Optional[int] = Query(None),
    subject_id: Optional[int] = Query(None),
    day_of_week: Optional[DayOfWeek] = Query(None),
    include_inactive: bool = Query(False),
    _: UserOut = Depends(require_admin),
):
    """
    [Admin Only] The timetable, filtered. Ordered the way a printed timetable reads:
    by weekday, then start time.
    """
    entries = timetable_service.list_entries(
        class_id=class_id, teacher_id=teacher_id, subject_id=subject_id,
        day_of_week=day_of_week, include_inactive=include_inactive,
    )
    return [TimetableEntryOut(**item) for item in timetable_service.hydrate_many(entries)]


@router.put("/timetable/{entry_id}", response_model=TimetableEntryOut)
def update_timetable_entry(
    entry_id: int,
    update_in: TimetableEntryUpdate,
    allow_conflicts: bool = Query(False, description="Save even if the change causes a clash"),
    _: UserOut = Depends(require_admin),
):
    """
    [Admin Only] Move, reassign, or deactivate a period.

    Clash detection re-runs against the merged result, ignoring the entry being edited, so
    nudging a period by five minutes is not reported as clashing with itself.
    """
    entry = require_document(firestore_timetable, entry_id, "Timetable entry")

    updated = update_in.model_dump(exclude_unset=True, exclude_none=True)
    if not updated:
        raise HTTPException(status_code=400, detail="No fields provided to update")

    if "day_of_week" in updated:
        updated["day_of_week"] = updated["day_of_week"].value
    for field in ("start_time", "end_time"):
        if field in updated:
            updated[field] = timetable_service.format_time(updated[field])
    for field in ("effective_from", "effective_to"):
        if field in updated:
            updated[field] = updated[field].isoformat()

    merged = {**entry, **updated}

    start = timetable_service.parse_time(merged["start_time"])
    end = timetable_service.parse_time(merged["end_time"])
    if end <= start:
        raise HTTPException(status_code=400, detail="end_time must be later than start_time")

    if "teacher_id" in updated and updated["teacher_id"] is not None:
        teacher = firestore_users.get_document(str(updated["teacher_id"]))
        if not teacher:
            raise HTTPException(status_code=404, detail=f"Teacher {updated['teacher_id']} not found")
        if teacher.get("role") not in TEACHING_OR_ADMIN_VALUES:
            raise HTTPException(
                status_code=400,
                detail=f"User {updated['teacher_id']} is not a teacher and cannot be assigned a period.",
            )

    if not allow_conflicts and merged.get("is_active", True):
        conflicts = timetable_service.find_conflicts(merged, exclude_id=entry_id)
        if conflicts:
            raise HTTPException(
                status_code=409,
                detail=(
                    "This change clashes with the existing timetable: " + "; ".join(conflicts)
                    + ". Fix the clash, or resend with ?allow_conflicts=true."
                ),
            )

    updated["updated_at"] = datetime.utcnow().isoformat()
    firestore_timetable.add_document(str(entry_id), updated)
    return TimetableEntryOut(
        **hydrate_timetable_entry(firestore_timetable.get_document(str(entry_id)))
    )


@router.delete("/timetable/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_timetable_entry(entry_id: int, _: UserOut = Depends(require_admin)):
    """
    [Admin Only] Remove a period from the timetable.
    To pause one temporarily instead, `PUT` it with `is_active: false`.
    """
    require_document(firestore_timetable, entry_id, "Timetable entry")
    firestore_timetable.delete_document(str(entry_id))
    return None


@router.get("/timetable/class/{class_id}/day", response_model=List[ScheduledPeriod])
def get_class_day_schedule(
    class_id: int,
    on_date: Optional[date] = Query(None, description="Defaults to today in the school timezone"),
    _: UserOut = Depends(require_admin),
):
    """
    [Admin Only] A class's periods resolved against one calendar date.

    Returns absolute `starts_at` / `ends_at` instants plus `starts_in_minutes` and
    `is_current`, so the client renders the day without redoing weekday or timezone maths.
    """
    require_document(firestore_classes, class_id, "ClassRoom")
    target = on_date or timetable_service.now_local().date()
    entries = timetable_service.list_entries(class_id=class_id)
    return [ScheduledPeriod(**period) for period in timetable_service.resolve_on_date(entries, target)]


@router.get("/reminders/status")
def get_reminder_status(_: UserOut = Depends(require_admin)):
    """
    [Admin Only] Whether class reminders are running, and what the last sweep did.

    `server_time_local` is worth checking first when reminders arrive at the wrong hour:
    it is the server's idea of the school's local time, and a wrong SCHOOL_TIMEZONE shows
    up here immediately.
    """
    return get_scheduler().status()


@router.get("/reminders/preview")
def preview_reminders(_: UserOut = Depends(require_admin)):
    """
    [Admin Only] What the next sweep would send, without sending anything.

    Use this after editing a timetable to confirm the right people are about to be emailed.
    Nothing is claimed or delivered, so it is safe to call repeatedly.
    """
    return run_reminder_sweep(dry_run=True)


@router.post("/reminders/run", status_code=status.HTTP_202_ACCEPTED)
def run_reminders_now(current_user: UserOut = Depends(require_admin)):
    """
    [Admin Only] Trigger a reminder sweep immediately instead of waiting for the next tick.

    Returns a job id to poll at `GET /admin/jobs/{job_id}`. Already-sent reminders are
    skipped, so running this by hand cannot double-email anyone.
    """
    job = job_registry.submit(
        name="reminder_sweep",
        func=lambda progress: run_reminder_sweep(progress=progress),
        requested_by=current_user.id,
    )
    return {**job.to_dict(), "poll_url": f"/api/v1/admin/jobs/{job.id}"}


@router.get("/reminders/log")
def list_reminder_log(
    limit: int = Query(50, ge=1, le=500),
    user_id: Optional[int] = Query(None),
    _: UserOut = Depends(require_admin),
):
    """
    [Admin Only] Recently sent class reminders, newest first.
    Answers "did this student actually get told?" without reading server logs.
    """
    records = (
        firestore_reminder_log.query_documents("user_id", "==", user_id)
        if user_id is not None else firestore_reminder_log.list_all()
    )
    records.sort(key=lambda r: r.get("claimed_at") or "", reverse=True)
    return records[:limit]


@router.get("/recordings/status")
def get_recording_status(_: UserOut = Depends(require_admin)):
    """
    [Admin Only] Whether class recordings are being collected, and what the last sweep did.

    Open this first when a finished class has no video: `problems` names any missing piece
    of the recording setup, and `last_result.details` shows what the sweep decided about each
    recording it looked at.
    """
    return get_recording_scheduler().status()


@router.get("/recordings/pending")
def preview_recordings(_: UserOut = Depends(require_admin)):
    """
    [Admin Only] What the next sweep would do, without copying or changing anything.
    """
    return run_recording_sweep(dry_run=True)


@router.post("/recordings/run", status_code=status.HTTP_202_ACCEPTED)
def run_recordings_now(current_user: UserOut = Depends(require_admin)):
    """
    [Admin Only] Collect finished recordings now instead of waiting for the next tick.

    Returns a job id to poll at `GET /admin/jobs/{job_id}`. Recordings already filed are
    skipped by their copy claim, so running this by hand cannot duplicate a video.
    """
    job = job_registry.submit(
        name="recording_sweep",
        func=lambda progress: run_recording_sweep(progress=progress),
        requested_by=current_user.id,
    )
    return {**job.to_dict(), "poll_url": f"/api/v1/admin/jobs/{job.id}"}


@router.post("/meetings/{meeting_id}/recording/sync", response_model=LiveMeetingOut)
def admin_sync_meeting_recording(
    meeting_id: int,
    _: UserOut = Depends(require_admin),
):
    """
    [Admin Only] File one session's announced recording immediately.

    For the session someone is asking about right now, rather than waiting up to
    RECORDING_SCAN_INTERVAL_SECONDS for the sweep to reach it. A recording ACS has not
    finished writing simply leaves the session as WAITING.
    """
    meeting = require_document(firestore_meetings, meeting_id, "Meeting")

    harvest_meeting(meeting)
    return LiveMeetingOut(**hydrate_live_meeting(firestore_meetings.get_document(str(meeting_id))))


@router.get("/recordings/log")
def list_recording_log(
    limit: int = Query(50, ge=1, le=500),
    meeting_id: Optional[int] = Query(None),
    _: UserOut = Depends(require_admin),
):
    """
    [Admin Only] Recently filed class recordings, newest first.
    Answers "where did that video go?" - and, for a failure, why it did not go there.
    """
    records = (
        firestore_recording_log.query_documents("meeting_id", "==", meeting_id)
        if meeting_id is not None else firestore_recording_log.list_all()
    )
    records.sort(key=lambda r: r.get("claimed_at") or "", reverse=True)
    return records[:limit]


@router.post("/meetings", response_model=LiveMeetingOut, status_code=status.HTTP_201_CREATED)
def admin_create_meeting(
    meeting_in: AdminLiveMeetingCreate,
    current_user: UserOut = Depends(require_admin)
):
    """
    [Admin Only] Schedule a live session, optionally on behalf of a teacher.

    Set `teacher_id` to file the session under that teacher; omit it to schedule under the
    acting admin.

    The meeting is saved even if it cannot be given a room - inspect `meet_status` and
    `meet_error` on the response, and `GET /admin/integrations` for the underlying cause.
    """
    if meeting_in.teacher_id is not None:
        teacher = resolve_teacher(meeting_in.teacher_id)
        teacher_id = meeting_in.teacher_id
        teacher_email = teacher.get("email", "")
    else:
        teacher_id = current_user.id
        teacher_email = current_user.email

    meeting_data = schedule_meeting(meeting_in, teacher_id=teacher_id, teacher_email=teacher_email)
    return LiveMeetingOut(**hydrate_live_meeting(meeting_data))


@router.get("/meetings", response_model=List[LiveMeetingOut])
def admin_list_meetings(
    class_id: Optional[int] = Query(None),
    subject_id: Optional[int] = Query(None),
    teacher_id: Optional[int] = Query(None),
    _: UserOut = Depends(require_admin)
):
    """
    [Admin Only] Every scheduled meeting across all teachers, newest first.
    The teacher endpoint only ever shows a teacher their own sessions; this is the
    system-wide view.
    """
    meetings = (
        firestore_meetings.query_documents("teacher_id", "==", teacher_id)
        if teacher_id is not None else firestore_meetings.list_all()
    )
    if class_id is not None:
        meetings = [m for m in meetings if m.get("class_id") == class_id]
    if subject_id is not None:
        meetings = [m for m in meetings if m.get("subject_id") == subject_id]

    meetings.sort(key=lambda m: m.get("scheduled_time") or "", reverse=True)
    prefetch_academic(meetings)
    return [LiveMeetingOut(**hydrate_live_meeting(m)) for m in meetings]


@router.put("/meetings/{meeting_id}", response_model=LiveMeetingOut)
def admin_update_meeting(
    meeting_id: int,
    update_in: LiveMeetingUpdate,
    _: UserOut = Depends(require_admin)
):
    """
    [Admin Only] Reschedule or edit any teacher's meeting. The join link is unchanged; the
    class's room follows the new time on its own.
    """
    require_document(firestore_meetings, meeting_id, "Meeting")

    updated = update_in.model_dump(exclude_unset=True, exclude_none=True)
    if not updated:
        raise HTTPException(status_code=400, detail="No fields provided to update")

    if "scheduled_time" in updated:
        updated["scheduled_time"] = updated["scheduled_time"].isoformat()

    firestore_meetings.add_document(str(meeting_id), updated)
    return LiveMeetingOut(**hydrate_live_meeting(firestore_meetings.get_document(str(meeting_id))))


@router.post("/meetings/{meeting_id}/regenerate-link", response_model=LiveMeetingOut)
def admin_regenerate_meeting_link(
    meeting_id: int,
    admin: UserOut = Depends(require_admin)
):
    """
    [Admin Only] Give a session a working live-class room.

    For a session saved without one (live classes were not configured when it was
    scheduled), and for one still carrying a Google Meet link from before the move to Azure -
    those links stopped working with the Workspace. Under the classroom model the session is
    pointed at its class's shared room, creating it if need be.
    """
    meeting = require_document(firestore_meetings, meeting_id, "Meeting")

    link = meeting.get("meeting_link") or ""
    if link and not is_legacy_meet_link(link) and meeting.get("meet_status") == "MANUAL":
        raise HTTPException(
            status_code=400,
            detail="This meeting has a link entered by hand. Clear it first, or edit it directly.",
        )

    updates = regenerate_meeting_link(meeting)
    if updates.get("meet_status") == "FAILED":
        raise HTTPException(status_code=502, detail=updates.get("meet_error") or "No room could be given.")
    return LiveMeetingOut(**hydrate_live_meeting(firestore_meetings.get_document(str(meeting_id))))


@router.delete("/meetings/{meeting_id}", status_code=status.HTTP_204_NO_CONTENT)
def admin_delete_meeting(meeting_id: int, _: UserOut = Depends(require_admin)):
    """
    [Admin Only] Cancel any teacher's meeting. Its live-class room goes with it.
    """
    meeting = require_document(firestore_meetings, meeting_id, "Meeting")
    acs.delete_room(meeting.get("acs_room_id"))

    firestore_meetings.delete_document(str(meeting_id))
    return None


@router.post("/materials", response_model=StudyMaterialOut, status_code=status.HTTP_201_CREATED)
async def admin_upload_material(
    class_id: int = Form(...),
    subject_id: int = Form(...),
    title: str = Form(...),
    material_type: str = Form("NOTES", description="NOTES, BOOK, ASSIGNMENT, SYLLABUS"),
    teacher_id: Optional[int] = Form(None, description="Attribute the upload to this teacher; defaults to the acting admin"),
    file: UploadFile = File(...),
    current_user: UserOut = Depends(require_admin)
):
    """
    [Admin Only] Upload notes, books, or study materials, optionally on behalf of a teacher.

    Uses the same storage backend as the teacher upload (Azure Blob Storage, or local disk).
    If `storage_provider` on the response reads LOCAL while Blob storage is configured,
    `storage_warning` names the failure - `GET /admin/integrations` diagnoses it.
    """
    if teacher_id is not None:
        resolve_teacher(teacher_id)
    else:
        teacher_id = current_user.id

    material_data = await store_material(
        file=file,
        class_id=class_id,
        subject_id=subject_id,
        title=title,
        material_type=material_type,
        teacher_id=teacher_id,
    )
    return StudyMaterialOut(**hydrate_study_material(material_data))


@router.post("/materials/links", response_model=StudyMaterialOut, status_code=status.HTTP_201_CREATED)
def admin_share_study_link(
    link_in: AdminStudyMaterialLinkCreate,
    current_user: UserOut = Depends(require_admin)
):
    """
    [Admin Only] Share a web page or YouTube video into a class's library, optionally on
    behalf of a teacher. YouTube addresses play in place for students.
    """
    teacher_id = link_in.teacher_id
    if teacher_id is not None:
        resolve_teacher(teacher_id)
    else:
        teacher_id = current_user.id

    material_data = store_material_link(link_in, teacher_id=teacher_id)
    return StudyMaterialOut(**hydrate_study_material(material_data))


@router.get("/materials", response_model=List[StudyMaterialOut])
def admin_list_materials(
    class_id: Optional[int] = Query(None),
    subject_id: Optional[int] = Query(None),
    teacher_id: Optional[int] = Query(None),
    _: UserOut = Depends(require_admin)
):
    """
    [Admin Only] Every study material in the system, newest first.
    """
    materials = (
        firestore_materials.query_documents("teacher_id", "==", teacher_id)
        if teacher_id is not None else firestore_materials.list_all()
    )
    if class_id is not None:
        materials = [m for m in materials if m.get("class_id") == class_id]
    if subject_id is not None:
        materials = [m for m in materials if m.get("subject_id") == subject_id]

    materials.sort(key=lambda m: m.get("uploaded_at") or "", reverse=True)
    prefetch_academic(materials)
    return [StudyMaterialOut(**hydrate_study_material(m)) for m in materials]


@router.put("/materials/{material_id}", response_model=StudyMaterialOut)
def admin_update_material(
    material_id: int,
    update_in: StudyMaterialUpdate,
    _: UserOut = Depends(require_admin)
):
    """
    [Admin Only] Rename any study material or change its type.
    To replace the file itself, delete the material and upload again.
    """
    material = require_document(firestore_materials, material_id, "Study material")

    updated = material_update_fields(material, update_in)
    if not updated:
        raise HTTPException(status_code=400, detail="No fields provided to update")

    firestore_materials.add_document(str(material_id), updated)
    return StudyMaterialOut(**hydrate_study_material(firestore_materials.get_document(str(material_id))))


@router.delete("/materials/{material_id}", status_code=status.HTTP_204_NO_CONTENT)
def admin_delete_material(
    material_id: int,
    keep_file: bool = Query(False, description="Delete the LMS record but leave the stored file in place"),
    _: UserOut = Depends(require_admin)
):
    """
    [Admin Only] Delete any uploaded study material.
    The underlying file in cloud storage is removed too unless `keep_file=true`.
    """
    material = require_document(firestore_materials, material_id, "Study material")

    # A published class recording is unpublished; its video stays with the recordings.
    if material.get("recording_key"):
        from app.services import recordings

        recordings.material_removed(material)
        firestore_materials.delete_document(str(material_id))
        return None

    # A shared link has no stored file to remove.
    if not keep_file and not material.get("external_url"):
        storage_service.delete_file(
            material.get("file_url", ""),
            provider=material.get("storage_provider"),
        )

    firestore_materials.delete_document(str(material_id))
    return None


@router.get("/integrations")
def get_integration_status(
    probe: bool = Query(True, description="Perform live reachability checks; set False for a settings-only view"),
    _: UserOut = Depends(require_admin)
):
    """
    [Admin Only] Health and configuration of the Azure integrations.

    This is the endpoint to open when uploads land on local disk, a class cannot be joined,
    or email does not arrive. Each section reports `ok` (when probed) plus a `detail` naming
    the exact misconfiguration. No secrets are returned.
    """
    storage = storage_service.check_access() if probe else storage_service.describe()
    live = acs.check_access() if probe else acs.describe_configuration()
    recording_problems = acs.recording_problems()

    return {
        "storage": storage,
        "storage_config_conflict": settings.storage_config_conflict,
        "live_classes": {**acs.describe_configuration(), **live},
        # Reported separately: classes can run perfectly while the recording webhook is not
        # yet subscribed.
        "recording": {
            "enabled": settings.ENABLE_AUTO_RECORDING,
            "ok": not recording_problems,
            "problems": recording_problems,
            "webhook_path": f"{settings.API_V1_STR}/calls/events?key=<ACS_EVENTS_KEY>",
            "detail": (
                "Recordings are started with each teacher's call and filed into Blob storage."
                if not recording_problems else recording_problems[0]
            ),
        },
        "email": {**mailer.describe(), "ok": mailer.is_configured()},
        "probed": probe,
    }


@router.post("/integrations/storage/test-upload")
async def test_storage_upload(
    cleanup: bool = Query(True, description="Delete the probe file after the round trip"),
    _: UserOut = Depends(require_admin)
):
    """
    [Admin Only] End-to-end storage probe: upload a tiny file, report where it landed, and
    delete it again.

    `GET /admin/integrations` checks reachability; this proves a real write actually works,
    which is the difference between "storage is visible" and "storage accepts our uploads".
    """
    import io as _io
    from starlette.datastructures import UploadFile as _UploadFile

    probe_bytes = b"H7 LMS storage connectivity probe.\n"
    probe = _UploadFile(
        filename="h7-lms-storage-probe.txt",
        file=_io.BytesIO(probe_bytes),
        headers={"content-type": "text/plain"},
    )

    try:
        stored = await storage_service.save_file_detailed(file=probe, folder="_diagnostics")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Storage probe failed: {exc}") from exc

    deleted = None
    if cleanup:
        deleted = storage_service.delete_file(stored["url"], provider=stored["provider"])

    reached_cloud = stored["provider"] == stored["requested_provider"]
    return {
        "ok": reached_cloud,
        "requested_provider": stored["requested_provider"],
        "actual_provider": stored["provider"],
        "url": stored["url"],
        "warning": stored.get("warning"),
        "cleaned_up": deleted,
        "detail": (
            f"Upload reached {stored['provider']} as configured."
            if reached_cloud else
            f"Upload was requested on {stored['requested_provider']} but landed on "
            f"{stored['provider']}. {stored.get('warning') or ''}".strip()
        ),
    }


def build_monitoring_report(progress: Callable[[int, str], None] | None = None) -> SystemMonitoringReport:
    """
    Computes the full system monitoring report.

    The original implementation issued one database query at a time, so cost scaled as
    (4 + 4 x teachers + 3 x students) sequential network waits - over twenty seconds on a
    small dataset. This version reads each collection once, in parallel, and derives every
    per-teacher and per-student figure by grouping those results in memory. Query count is
    now constant regardless of how many teachers or students exist.

    `progress` is an optional callback used when running as a background job.
    """
    def report(percent: int, message: str) -> None:
        if progress:
            progress(percent, message)

    report(5, "Loading collections")

    # One read per collection, all concurrently. Everything below is computed from these.
    data = run_parallel({
        "users": firestore_users.list_all,
        "classes": firestore_classes.list_all,
        "subjects": firestore_subjects.list_all,
        "materials": firestore_materials.list_all,
        "attendance": firestore_attendance.list_all,
        "grades": firestore_grades.list_all,
        "topics": firestore_topics.list_all,
        "mappings": firestore_teacher_mappings.list_all,
        "enrollments": firestore_student_enrollments.list_all,
    })

    users = data.get("users") or []
    classes = data.get("classes") or []
    materials = data.get("materials") or []
    attendance = data.get("attendance") or []
    grades = data.get("grades") or []
    topics = data.get("topics") or []
    mappings = data.get("mappings") or []
    enrollments = data.get("enrollments") or []

    report(45, "Indexing records")

    students = [u for u in users if u.get("role") == UserRole.STUDENT.value and u.get("is_active", False)]
    # Both teaching roles, or a teacher would drop out of the activity report the moment they
    # were made a class teacher - exactly the person an admin most wants to see on it.
    teachers = [u for u in users if u.get("role") in TEACHING_ROLE_VALUES and u.get("is_active", False)]

    def group_by(records: list[dict], field: str) -> dict:
        buckets: dict = {}
        for record in records:
            buckets.setdefault(record.get(field), []).append(record)
        return buckets

    mappings_by_teacher = group_by(mappings, "teacher_id")
    topics_by_teacher = group_by(topics, "teacher_id")
    materials_by_teacher = group_by(materials, "teacher_id")
    attendance_by_teacher = group_by(attendance, "teacher_id")
    attendance_by_student = group_by(attendance, "student_id")
    grades_by_student = group_by(grades, "student_id")
    enrollments_by_student = group_by(enrollments, "student_id")
    class_names = {c["id"]: c.get("name") for c in classes}

    overall = OverallStats(
        total_students=len(students),
        total_teachers=len(teachers),
        total_classes=len(classes),
        total_subjects=len(data.get("subjects") or []),
        total_materials_uploaded=len(materials),
        total_attendance_logs=len(attendance),
    )

    report(65, "Summarising teacher activity")

    teacher_reports = [
        TeacherActivityReport(
            teacher_id=t["id"],
            teacher_name=t["full_name"],
            assigned_classes_count=len(mappings_by_teacher.get(t["id"], [])),
            topics_covered_count=len(topics_by_teacher.get(t["id"], [])),
            materials_uploaded_count=len(materials_by_teacher.get(t["id"], [])),
            attendance_marked_count=len(attendance_by_teacher.get(t["id"], [])),
        )
        for t in teachers
    ]

    report(85, "Summarising student performance")

    student_reports = []
    for s in students:
        enrollment = next(iter(enrollments_by_student.get(s["id"], [])), None)
        class_name = "Unassigned"
        if enrollment:
            class_name = class_names.get(enrollment.get("class_id")) or "Unassigned"

        records = attendance_by_student.get(s["id"], [])
        present = sum(1 for a in records if a.get("status") == AttendanceStatus.PRESENT.value)
        att_percentage = (present / len(records) * 100.0) if records else 0.0

        student_grades = grades_by_student.get(s["id"], [])
        # Rows mirrored from a letter-graded exam carry a grade and no marks. They are real
        # results but there is no number in them to average, so they are counted as exams
        # taken and left out of the percentage rather than treated as a zero.
        scored = [
            g for g in student_grades
            if g.get("marks_obtained") is not None and g.get("max_marks")
        ]
        avg_grade = 0.0
        if scored:
            avg_grade = sum(
                (g["marks_obtained"] / g["max_marks"]) * 100.0 for g in scored
            ) / len(scored)

        student_reports.append(StudentPerformanceReport(
            student_id=s["id"],
            student_name=s["full_name"],
            class_name=class_name,
            attendance_percentage=round(att_percentage, 2),
            average_grade_percentage=round(avg_grade, 2),
            total_exams_taken=len(student_grades),
        ))

    report(100, "Report ready")

    return SystemMonitoringReport(
        overall_stats=overall,
        teacher_activity=teacher_reports,
        student_performance=student_reports,
    )


@router.get("/reports/monitoring", response_model=SystemMonitoringReport)
def get_system_monitoring_report(
    refresh: bool = Query(False, description="Bypass the cache and recompute now"),
    _: UserOut = Depends(require_admin)
):
    """
    [Admin Only] Analytics and System Monitoring Report.

    Served from a short-lived cache so repeat views are instant. Pass `refresh=true` to
    recompute synchronously, or use `POST /admin/reports/monitoring/refresh` to rebuild it
    in the background and poll for progress.
    """
    if not refresh:
        cached = report_cache.get("monitoring", settings.MONITORING_REPORT_TTL_SECONDS)
        if cached is not None:
            return cached

    result = build_monitoring_report()
    report_cache.set("monitoring", result)
    return result


@router.post("/reports/monitoring/refresh", status_code=status.HTTP_202_ACCEPTED)
def refresh_monitoring_report(current_user: UserOut = Depends(require_admin)):
    """
    [Admin Only] Rebuild the monitoring report in the background.

    Returns immediately with a job id. Poll `GET /admin/jobs/{job_id}` for progress, then
    re-read `GET /admin/reports/monitoring` to get the refreshed figures.

    Requesting a refresh while one is already running returns that same job rather than
    starting a duplicate.
    """
    def task(progress: Callable[[int, str], None]):
        result = build_monitoring_report(progress=progress)
        report_cache.set("monitoring", result)
        return {"generated": True}

    job = job_registry.submit(
        name="monitoring_report",
        func=task,
        requested_by=current_user.id,
    )
    return {
        **job.to_dict(),
        "poll_url": f"/api/v1/admin/jobs/{job.id}",
    }


@router.get("/jobs/{job_id}")
def get_job_status(job_id: str, _: UserOut = Depends(require_admin)):
    """
    [Admin Only] Progress and outcome of a background job.

    `status` is one of QUEUED, RUNNING, SUCCEEDED, FAILED. `percent` and `message` describe
    how far along it is, which is what a progress bar or notification should display.
    """
    job = job_registry.get(job_id)
    if job is None:
        raise HTTPException(
            status_code=404,
            detail="Job not found. It may have expired, or the server restarted since it was started.",
        )
    return job.to_dict()


@router.get("/jobs")
def list_jobs(
    limit: int = Query(20, ge=1, le=100),
    _: UserOut = Depends(require_admin)
):
    """
    [Admin Only] Recent background jobs, newest first.
    Useful for driving a notifications panel.
    """
    return [job.to_dict() for job in job_registry.list_jobs(limit=limit)]
