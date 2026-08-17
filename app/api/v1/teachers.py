from datetime import date, datetime
from typing import List, Optional
from fastapi import APIRouter, Depends, UploadFile, File, Form, HTTPException, status, Query

from app.api.v1.dependencies import require_teacher
from app.core import google_meet
from app.core.enums import DayOfWeek
from app.core.gcp_services import storage_service
from app.core.firebase import (
    firestore_attendance, firestore_topics, firestore_meetings,
    firestore_materials, firestore_grades,
    firestore_teacher_mappings, firestore_class_teacher_mappings, firestore_users,
    hydrate_attendance, hydrate_topic, hydrate_live_meeting,
    hydrate_study_material, hydrate_exam_grade, hydrate_teacher_mapping,
    hydrate_class_teacher_mapping,
    firestore_classes, firestore_subjects,
    require_document, assert_owner, prefetch_references, prefetch_academic
)
from app.services import timetable as timetable_service
from app.services.content import (
    active_students_in_class as _active_students_in_class,
    schedule_meeting, store_material,
)
from app.services.permissions import visible_records
from app.schemas.academic import ClassTeacherMappingOut, TeacherMappingOut
from app.schemas.timetable import ScheduledPeriod, TimetableEntryOut
from app.schemas.user import UserOut
from app.schemas.attendance import BatchAttendanceCreate, AttendanceUpdate, AttendanceOut
from app.schemas.topic import TopicCreate, TopicUpdate, TopicOut
from app.schemas.meeting import LiveMeetingCreate, LiveMeetingUpdate, LiveMeetingOut
from app.schemas.material import StudyMaterialUpdate, StudyMaterialOut
from app.schemas.grade import GradeEntryCreate, GradeEntryUpdate, ExamGradeOut

router = APIRouter(prefix="/teachers", tags=["Teachers Module"])


@router.get("/my-classes", response_model=List[TeacherMappingOut])
def get_teacher_assigned_classes(
    current_user: UserOut = Depends(require_teacher)
):
    """
    [Teacher Only] Retrieve assigned classes and subjects for logged in teacher.
    """
    mappings = firestore_teacher_mappings.query_documents("teacher_id", "==", current_user.id)
    prefetch_references(
        mappings,
        ("teacher_id", firestore_users),
        ("subject_id", firestore_subjects),
        ("class_id", firestore_classes),
    )
    return [TeacherMappingOut(**hydrate_teacher_mapping(m)) for m in mappings]


@router.get("/my-led-classes", response_model=List[ClassTeacherMappingOut])
def get_classes_led_as_class_teacher(
    current_user: UserOut = Depends(require_teacher)
):
    """
    [Teacher Only] The classes this teacher is the class teacher of.

    Distinct from `/my-classes`, which lists the subject-and-class periods they teach. These
    are the classes they answer for as a whole: within them, the attendance, topics, grades,
    meetings and materials listed by the endpoints below include records filed by the *other*
    teachers who take the class, and those records can be corrected here too.

    An empty list means the teacher leads no class, which is the normal case for a subject
    teacher and the reason the class-teacher screens should stay hidden for them.
    """
    mappings = firestore_class_teacher_mappings.query_documents("teacher_id", "==", current_user.id)
    prefetch_references(
        mappings,
        ("teacher_id", firestore_users),
        ("class_id", firestore_classes),
    )
    return [ClassTeacherMappingOut(**hydrate_class_teacher_mapping(m)) for m in mappings]


@router.get("/classes/{class_id}/students", response_model=List[UserOut])
def get_students_in_class(
    class_id: int,
    current_user: UserOut = Depends(require_teacher)
):
    """
    [Teacher Only] Retrieve list of enrolled students in a class.
    """
    return [UserOut(**student) for student in _active_students_in_class(class_id)]


@router.get("/timetable", response_model=List[TimetableEntryOut])
def get_teacher_timetable(
    day_of_week: Optional[DayOfWeek] = Query(None),
    class_id: Optional[int] = Query(None),
    current_user: UserOut = Depends(require_teacher)
):
    """
    [Teacher Only] The periods this teacher is scheduled to take, for the whole week.
    Ordered by weekday then start time, the way a printed timetable reads.
    """
    entries = timetable_service.list_entries(
        teacher_id=current_user.id, class_id=class_id, day_of_week=day_of_week
    )
    return [TimetableEntryOut(**item) for item in timetable_service.hydrate_many(entries)]


@router.get("/timetable/day", response_model=List[ScheduledPeriod])
def get_teacher_day_schedule(
    on_date: Optional[date] = Query(None, description="Defaults to today in the school timezone"),
    current_user: UserOut = Depends(require_teacher)
):
    """
    [Teacher Only] This teacher's periods for one date, with absolute start and end times.
    `is_current` marks the period happening right now; `starts_in_minutes` counts down.
    """
    target = on_date or timetable_service.now_local().date()
    entries = timetable_service.list_entries(teacher_id=current_user.id)
    return [ScheduledPeriod(**p) for p in timetable_service.resolve_on_date(entries, target)]


@router.get("/timetable/upcoming", response_model=List[ScheduledPeriod])
def get_teacher_upcoming_periods(
    days_ahead: int = Query(7, ge=0, le=31),
    limit: int = Query(10, ge=1, le=50),
    current_user: UserOut = Depends(require_teacher)
):
    """
    [Teacher Only] The next periods this teacher takes, looking forward across days.
    Drives a "what's next" panel without the client scanning the weekly grid itself.
    """
    entries = timetable_service.list_entries(teacher_id=current_user.id)
    return [
        ScheduledPeriod(**p)
        for p in timetable_service.upcoming_periods(entries, days_ahead=days_ahead, limit=limit)
    ]


@router.post("/attendance", response_model=List[AttendanceOut], status_code=status.HTTP_201_CREATED)
def mark_attendance(
    batch_in: BatchAttendanceCreate,
    current_user: UserOut = Depends(require_teacher)
):
    """
    [Teacher Only - Independent] Mark attendance for students in a class and subject for a date.
    Syncs records to Firebase Cloud Firestore database.
    """
    created_records = []
    for item in batch_in.attendance_list:
        existing = next(
            (
                record for record in firestore_attendance.query_documents("student_id", "==", item.student_id)
                if record.get("class_id") == batch_in.class_id
                and record.get("subject_id") == batch_in.subject_id
                and record.get("date") == batch_in.date.isoformat()
            ),
            None
        )

        created_at = existing.get("created_at") if existing else datetime.utcnow().isoformat()
        record_data = {
            "student_id": item.student_id,
            "class_id": batch_in.class_id,
            "subject_id": batch_in.subject_id,
            "teacher_id": current_user.id,
            "date": batch_in.date.isoformat(),
            "status": item.status.value,
            "remarks": item.remarks,
            "created_at": created_at
        }

        if existing:
            firestore_attendance.add_document(str(existing["id"]), record_data)
            record_data["id"] = existing["id"]
        else:
            new_id = firestore_attendance.get_next_numeric_id()
            firestore_attendance.add_document(str(new_id), record_data)
            record_data["id"] = new_id

        created_records.append(AttendanceOut(**hydrate_attendance(record_data)))

    return created_records


@router.get("/attendance", response_model=List[AttendanceOut])
def get_teacher_attendance_records(
    class_id: Optional[int] = Query(None),
    subject_id: Optional[int] = Query(None),
    current_user: UserOut = Depends(require_teacher)
):
    """
    [Teacher Only] View attendance history logged by this teacher.

    A class teacher additionally sees every attendance record marked against the classes they
    lead, whichever teacher marked it - that whole-class view is the point of the role. Pass
    `class_id` to narrow to one class; a class they neither lead nor marked returns only
    their own records within it.
    """
    records = visible_records(firestore_attendance, current_user, class_id)
    if subject_id is not None:
        records = [r for r in records if r.get("subject_id") == subject_id]
    prefetch_academic(records)
    return [AttendanceOut(**hydrate_attendance(record)) for record in records]


@router.put("/attendance/{record_id}", response_model=AttendanceOut)
def update_attendance_record(
    record_id: int,
    update_in: AttendanceUpdate,
    current_user: UserOut = Depends(require_teacher)
):
    """
    [Teacher Only] Correct a previously marked attendance record.

    A subject teacher may only edit records they marked themselves. The class teacher of the
    class may correct any of them, whoever marked it, and admins may edit any record at all.
    """
    record = require_document(firestore_attendance, record_id, "Attendance record")
    assert_owner(record, current_user, "attendance records")

    updated = update_in.model_dump(exclude_unset=True, exclude_none=True)
    if not updated:
        raise HTTPException(status_code=400, detail="No fields provided to update")

    if "status" in updated:
        updated["status"] = updated["status"].value
    if "date" in updated:
        updated["date"] = updated["date"].isoformat()

    firestore_attendance.add_document(str(record_id), updated)
    return AttendanceOut(**hydrate_attendance(firestore_attendance.get_document(str(record_id))))


@router.delete("/attendance/{record_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_attendance_record(
    record_id: int,
    current_user: UserOut = Depends(require_teacher)
):
    """
    [Teacher Only] Delete an attendance record marked in error.
    """
    record = require_document(firestore_attendance, record_id, "Attendance record")
    assert_owner(record, current_user, "attendance records")

    firestore_attendance.delete_document(str(record_id))
    return None


@router.post("/topics", response_model=TopicOut, status_code=status.HTTP_201_CREATED)
def log_topic_covered(
    topic_in: TopicCreate,
    current_user: UserOut = Depends(require_teacher)
):
    """
    [Teacher Only - Independent] Log covered portion/topic for a class and subject.
    Syncs to Firebase Cloud Firestore collection.
    """
    topic_id = firestore_topics.get_next_numeric_id()
    topic_data = {
        "class_id": topic_in.class_id,
        "subject_id": topic_in.subject_id,
        "teacher_id": current_user.id,
        "topic_title": topic_in.topic_title,
        "description": topic_in.description,
        "date_covered": (topic_in.date_covered or datetime.utcnow().date()).isoformat(),
        "completion_percentage": topic_in.completion_percentage,
        "created_at": datetime.utcnow().isoformat()
    }
    firestore_topics.add_document(str(topic_id), topic_data)
    topic_data["id"] = topic_id
    return TopicOut(**hydrate_topic(topic_data))


@router.get("/topics", response_model=List[TopicOut])
def list_topics_covered(
    class_id: Optional[int] = Query(None),
    subject_id: Optional[int] = Query(None),
    current_user: UserOut = Depends(require_teacher)
):
    """
    [Teacher Only] Retrieve list of topics logged by this teacher.

    A class teacher also sees the syllabus progress logged by every other teacher in the
    classes they lead, which is what makes "how far has 9-A actually got?" answerable.
    """
    topics = visible_records(firestore_topics, current_user, class_id)
    if subject_id is not None:
        topics = [t for t in topics if t.get("subject_id") == subject_id]
    topics.sort(key=lambda t: t.get("date_covered"), reverse=True)
    prefetch_academic(topics)
    return [TopicOut(**hydrate_topic(topic)) for topic in topics]


@router.put("/topics/{topic_id}", response_model=TopicOut)
def update_topic_covered(
    topic_id: int,
    update_in: TopicUpdate,
    current_user: UserOut = Depends(require_teacher)
):
    """
    [Teacher Only] Edit a logged syllabus topic.
    """
    topic = require_document(firestore_topics, topic_id, "Topic")
    assert_owner(topic, current_user, "topics")

    updated = update_in.model_dump(exclude_unset=True, exclude_none=True)
    if not updated:
        raise HTTPException(status_code=400, detail="No fields provided to update")

    if "date_covered" in updated:
        updated["date_covered"] = updated["date_covered"].isoformat()

    firestore_topics.add_document(str(topic_id), updated)
    return TopicOut(**hydrate_topic(firestore_topics.get_document(str(topic_id))))


@router.delete("/topics/{topic_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_topic_covered(
    topic_id: int,
    current_user: UserOut = Depends(require_teacher)
):
    """
    [Teacher Only] Remove a logged syllabus topic.
    """
    topic = require_document(firestore_topics, topic_id, "Topic")
    assert_owner(topic, current_user, "topics")

    firestore_topics.delete_document(str(topic_id))
    return None


@router.post("/meetings", response_model=LiveMeetingOut, status_code=status.HTTP_201_CREATED)
def create_live_meeting(
    meeting_in: LiveMeetingCreate,
    current_user: UserOut = Depends(require_teacher)
):
    """
    [Teacher Only - Independent] Schedule a live class session.

    When `auto_create_meet` is set and no manual `meeting_link` is supplied, a real Google
    Calendar event with an attached Meet link is created on the teacher's calendar, and
    enrolled students are invited. If Meet generation is unavailable the meeting is still
    saved so the schedule is never lost, and `meet_status`/`meet_error` on the response say
    exactly why no link came back.
    """
    meeting_data = schedule_meeting(
        meeting_in,
        teacher_id=current_user.id,
        teacher_email=current_user.email,
    )
    return LiveMeetingOut(**hydrate_live_meeting(meeting_data))


@router.get("/meetings", response_model=List[LiveMeetingOut])
def list_meetings(
    class_id: Optional[int] = Query(None),
    subject_id: Optional[int] = Query(None),
    current_user: UserOut = Depends(require_teacher)
):
    """
    [Teacher Only] List scheduled meetings and recording links created by this teacher.

    A class teacher also sees the sessions other teachers scheduled for the classes they lead,
    so clashes in a class's day are visible to the person answerable for it.
    """
    meetings = visible_records(firestore_meetings, current_user, class_id)
    if subject_id is not None:
        meetings = [m for m in meetings if m.get("subject_id") == subject_id]
    meetings.sort(key=lambda m: m.get("scheduled_time"), reverse=True)
    prefetch_academic(meetings)
    return [LiveMeetingOut(**hydrate_live_meeting(meeting)) for meeting in meetings]


@router.put("/meetings/{meeting_id}", response_model=LiveMeetingOut)
def update_live_meeting(
    meeting_id: int,
    update_in: LiveMeetingUpdate,
    current_user: UserOut = Depends(require_teacher)
):
    """
    [Teacher Only] Reschedule or edit a meeting.
    Title and time changes propagate to the backing Google Calendar event, so invited
    students see the update on their own calendars.
    """
    meeting = require_document(firestore_meetings, meeting_id, "Meeting")
    assert_owner(meeting, current_user, "meetings")

    updated = update_in.model_dump(exclude_unset=True, exclude_none=True)
    if not updated:
        raise HTTPException(status_code=400, detail="No fields provided to update")

    if "scheduled_time" in updated:
        updated["scheduled_time"] = updated["scheduled_time"].isoformat()

    google_event_id = meeting.get("google_event_id")
    if google_event_id and ("title" in updated or "scheduled_time" in updated):
        google_meet.update_meeting(
            teacher_email=current_user.email,
            event_id=google_event_id,
            calendar_id=meeting.get("google_calendar_id"),
            title=update_in.title,
            scheduled_time=update_in.scheduled_time,
            duration_minutes=update_in.duration_minutes or meeting.get("duration_minutes") or 60,
        )

    firestore_meetings.add_document(str(meeting_id), updated)
    return LiveMeetingOut(**hydrate_live_meeting(firestore_meetings.get_document(str(meeting_id))))


@router.delete("/meetings/{meeting_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_live_meeting(
    meeting_id: int,
    current_user: UserOut = Depends(require_teacher)
):
    """
    [Teacher Only] Cancel a meeting.
    The backing Google Calendar event is deleted too, so attendees are notified.
    """
    meeting = require_document(firestore_meetings, meeting_id, "Meeting")
    assert_owner(meeting, current_user, "meetings")

    google_event_id = meeting.get("google_event_id")
    if google_event_id:
        google_meet.delete_meeting(
            teacher_email=current_user.email,
            event_id=google_event_id,
            calendar_id=meeting.get("google_calendar_id"),
        )

    firestore_meetings.delete_document(str(meeting_id))
    return None


@router.post("/materials", response_model=StudyMaterialOut, status_code=status.HTTP_201_CREATED)
async def upload_study_material(
    class_id: int = Form(...),
    subject_id: int = Form(...),
    title: str = Form(...),
    material_type: str = Form("NOTES", description="NOTES, BOOK, ASSIGNMENT, SYLLABUS"),
    file: UploadFile = File(...),
    current_user: UserOut = Depends(require_teacher)
):
    """
    [Teacher Only - Independent] Upload notes, books, or study materials.

    Saves the file to the configured backend (Cloud Storage, Workspace Shared Drive, or
    local disk), records which backend was used, and syncs the entry to Firebase. Check
    `storage_provider` on the response: if it reads LOCAL when Drive or GCS was configured,
    `storage_warning` explains what went wrong with the cloud upload.
    """
    material_data = await store_material(
        file=file,
        class_id=class_id,
        subject_id=subject_id,
        title=title,
        material_type=material_type,
        teacher_id=current_user.id,
    )
    return StudyMaterialOut(**hydrate_study_material(material_data))


@router.get("/materials", response_model=List[StudyMaterialOut])
def list_uploaded_materials(
    class_id: Optional[int] = Query(None),
    subject_id: Optional[int] = Query(None),
    current_user: UserOut = Depends(require_teacher)
):
    """
    [Teacher Only] View study materials uploaded by this teacher.

    A class teacher also sees everything issued to the classes they lead, whoever uploaded it.
    """
    materials = visible_records(firestore_materials, current_user, class_id)
    if subject_id is not None:
        materials = [m for m in materials if m.get("subject_id") == subject_id]
    prefetch_academic(materials)
    return [StudyMaterialOut(**hydrate_study_material(material)) for material in materials]


@router.put("/materials/{material_id}", response_model=StudyMaterialOut)
def update_study_material(
    material_id: int,
    update_in: StudyMaterialUpdate,
    current_user: UserOut = Depends(require_teacher)
):
    """
    [Teacher Only] Rename a study material or change its type.
    To replace the file itself, delete the material and upload again.
    """
    material = require_document(firestore_materials, material_id, "Study material")
    assert_owner(material, current_user, "study materials")

    updated = update_in.model_dump(exclude_unset=True, exclude_none=True)
    if not updated:
        raise HTTPException(status_code=400, detail="No fields provided to update")

    firestore_materials.add_document(str(material_id), updated)
    return StudyMaterialOut(**hydrate_study_material(firestore_materials.get_document(str(material_id))))


@router.delete("/materials/{material_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_study_material(
    material_id: int,
    keep_file: bool = Query(False, description="Delete the LMS record but leave the stored file in place"),
    current_user: UserOut = Depends(require_teacher)
):
    """
    [Teacher Only] Delete an uploaded study material.
    The underlying file in Cloud Storage or Drive is removed too unless `keep_file=true`.
    """
    material = require_document(firestore_materials, material_id, "Study material")
    assert_owner(material, current_user, "study materials")

    if not keep_file:
        storage_service.delete_file(
            material.get("file_url", ""),
            provider=material.get("storage_provider"),
        )

    firestore_materials.delete_document(str(material_id))
    return None


@router.post("/grades", response_model=ExamGradeOut, status_code=status.HTTP_201_CREATED)
def submit_exam_grade(
    grade_in: GradeEntryCreate,
    current_user: UserOut = Depends(require_teacher)
):
    """
    [Teacher Only - Independent] Enter subject exam/evaluation mark for a student.
    Syncs entry to Firebase Cloud Firestore collection.
    """
    grade_id = firestore_grades.get_next_numeric_id()
    grade_data = {
        "student_id": grade_in.student_id,
        "class_id": grade_in.class_id,
        "subject_id": grade_in.subject_id,
        "teacher_id": current_user.id,
        "exam_name": grade_in.exam_name,
        "marks_obtained": grade_in.marks_obtained,
        "max_marks": grade_in.max_marks,
        "remarks": grade_in.remarks,
        "created_at": datetime.utcnow().isoformat()
    }
    firestore_grades.add_document(str(grade_id), grade_data)
    grade_data["id"] = grade_id
    return ExamGradeOut(**hydrate_exam_grade(grade_data))


@router.get("/grades", response_model=List[ExamGradeOut])
def list_submitted_grades(
    class_id: Optional[int] = Query(None),
    subject_id: Optional[int] = Query(None),
    current_user: UserOut = Depends(require_teacher)
):
    """
    [Teacher Only] View exam grades entered by this teacher.

    A class teacher also sees marks entered by every other teacher for the classes they lead,
    which is what lets them compile a whole-class result rather than one subject's column.
    """
    grades = visible_records(firestore_grades, current_user, class_id)
    if subject_id is not None:
        grades = [g for g in grades if g.get("subject_id") == subject_id]
    prefetch_academic(grades)
    return [ExamGradeOut(**hydrate_exam_grade(grade)) for grade in grades]


@router.put("/grades/{grade_id}", response_model=ExamGradeOut)
def update_exam_grade(
    grade_id: int,
    update_in: GradeEntryUpdate,
    current_user: UserOut = Depends(require_teacher)
):
    """
    [Teacher Only] Correct a previously entered exam mark or remark.
    """
    grade = require_document(firestore_grades, grade_id, "Exam grade")
    assert_owner(grade, current_user, "exam grades")

    updated = update_in.model_dump(exclude_unset=True, exclude_none=True)
    if not updated:
        raise HTTPException(status_code=400, detail="No fields provided to update")

    marks = updated.get("marks_obtained", grade.get("marks_obtained"))
    max_marks = updated.get("max_marks", grade.get("max_marks"))
    if max_marks is not None and max_marks <= 0:
        raise HTTPException(status_code=400, detail="max_marks must be greater than zero")
    if marks is not None and max_marks is not None and marks > max_marks:
        raise HTTPException(status_code=400, detail="marks_obtained cannot exceed max_marks")

    firestore_grades.add_document(str(grade_id), updated)
    return ExamGradeOut(**hydrate_exam_grade(firestore_grades.get_document(str(grade_id))))


@router.delete("/grades/{grade_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_exam_grade(
    grade_id: int,
    current_user: UserOut = Depends(require_teacher)
):
    """
    [Teacher Only] Delete an exam grade entered in error.
    """
    grade = require_document(firestore_grades, grade_id, "Exam grade")
    assert_owner(grade, current_user, "exam grades")

    firestore_grades.delete_document(str(grade_id))
    return None
