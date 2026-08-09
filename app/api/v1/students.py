from datetime import date
from typing import List, Optional
from fastapi import APIRouter, Depends, Query

from app.api.v1.dependencies import require_student
from app.core.enums import DayOfWeek
from app.services import timetable as timetable_service
from app.core.firebase import (
    firestore_student_enrollments, firestore_teacher_mappings, firestore_attendance,
    firestore_topics, firestore_meetings, firestore_materials, firestore_grades,
    firestore_users, firestore_classes, firestore_subjects,
    hydrate_teacher_mapping, hydrate_attendance, hydrate_topic, hydrate_live_meeting,
    hydrate_study_material, hydrate_exam_grade,
    prefetch_references, prefetch_academic
)
from app.schemas.academic import TeacherMappingOut
from app.schemas.attendance import AttendanceOut
from app.schemas.topic import TopicOut
from app.schemas.meeting import LiveMeetingOut
from app.schemas.material import StudyMaterialOut
from app.schemas.grade import ExamGradeOut
from app.schemas.timetable import ScheduledPeriod, TimetableEntryOut
from app.schemas.user import UserOut

router = APIRouter(prefix="/students", tags=["Students Module"])


@router.get("/my-classes", response_model=List[TeacherMappingOut])
def get_student_enrolled_classes(
    current_user: UserOut = Depends(require_student)
):
    """
    [Student Only] View student's enrolled class details, subjects, and assigned teachers.
    """
    enrollment = next(
        (e for e in firestore_student_enrollments.query_documents("student_id", "==", current_user.id)),
        None
    )
    if not enrollment:
        return []

    mappings = firestore_teacher_mappings.query_documents("class_id", "==", enrollment.get("class_id"))
    prefetch_references(
        mappings,
        ("teacher_id", firestore_users),
        ("subject_id", firestore_subjects),
        ("class_id", firestore_classes),
    )
    return [TeacherMappingOut(**hydrate_teacher_mapping(m)) for m in mappings]


@router.get("/timetable", response_model=List[TimetableEntryOut])
def get_student_timetable(
    day_of_week: Optional[DayOfWeek] = Query(None),
    current_user: UserOut = Depends(require_student)
):
    """
    [Student Only] The student's weekly timetable, across every class they are enrolled in.
    Ordered by weekday then start time.
    """
    entries = timetable_service.entries_for_student(current_user.id, day_of_week=day_of_week)
    return [TimetableEntryOut(**item) for item in timetable_service.hydrate_many(entries)]


@router.get("/timetable/day", response_model=List[ScheduledPeriod])
def get_student_day_schedule(
    on_date: Optional[date] = Query(None, description="Defaults to today in the school timezone"),
    current_user: UserOut = Depends(require_student)
):
    """
    [Student Only] Today's classes (or another date's), with absolute start and end times.
    `is_current` marks the period in progress; `starts_in_minutes` counts down to the next.
    """
    target = on_date or timetable_service.now_local().date()
    entries = timetable_service.entries_for_student(current_user.id)
    return [ScheduledPeriod(**p) for p in timetable_service.resolve_on_date(entries, target)]


@router.get("/timetable/upcoming", response_model=List[ScheduledPeriod])
def get_student_upcoming_periods(
    days_ahead: int = Query(7, ge=0, le=31),
    limit: int = Query(10, ge=1, le=50),
    current_user: UserOut = Depends(require_student)
):
    """
    [Student Only] The next classes coming up, looking forward across days.
    Handles the case where the next lesson in a subject is not until next week.
    """
    entries = timetable_service.entries_for_student(current_user.id)
    return [
        ScheduledPeriod(**p)
        for p in timetable_service.upcoming_periods(entries, days_ahead=days_ahead, limit=limit)
    ]


@router.get("/attendance", response_model=List[AttendanceOut])
def get_student_attendance(
    subject_id: Optional[int] = Query(None),
    current_user: UserOut = Depends(require_student)
):
    """
    [Student Only] View personal attendance history per subject.
    """
    records = firestore_attendance.query_documents("student_id", "==", current_user.id)
    if subject_id is not None:
        records = [r for r in records if r.get("subject_id") == subject_id]
    records.sort(key=lambda r: r.get("date"), reverse=True)
    prefetch_academic(records)
    return [AttendanceOut(**hydrate_attendance(record)) for record in records]


@router.get("/topics", response_model=List[TopicOut])
def get_student_topics_covered(
    subject_id: Optional[int] = Query(None),
    current_user: UserOut = Depends(require_student)
):
    """
    [Student Only] View syllabus topics/portions covered by teachers for student's class.
    """
    enrollment = next(
        (e for e in firestore_student_enrollments.query_documents("student_id", "==", current_user.id)),
        None
    )
    if not enrollment:
        return []

    topics = firestore_topics.query_documents("class_id", "==", enrollment.get("class_id"))
    if subject_id is not None:
        topics = [t for t in topics if t.get("subject_id") == subject_id]
    topics.sort(key=lambda t: t.get("date_covered"), reverse=True)
    prefetch_academic(topics)
    return [TopicOut(**hydrate_topic(topic)) for topic in topics]


@router.get("/meetings", response_model=List[LiveMeetingOut])
def get_student_live_meetings(
    subject_id: Optional[int] = Query(None),
    current_user: UserOut = Depends(require_student)
):
    """
    [Student Only] View live meeting links (e.g. Google Meet) and recorded lecture URLs for enrolled class.
    """
    enrollment = next(
        (e for e in firestore_student_enrollments.query_documents("student_id", "==", current_user.id)),
        None
    )
    if not enrollment:
        return []

    meetings = firestore_meetings.query_documents("class_id", "==", enrollment.get("class_id"))
    if subject_id is not None:
        meetings = [m for m in meetings if m.get("subject_id") == subject_id]
    meetings.sort(key=lambda m: m.get("scheduled_time"), reverse=True)
    prefetch_academic(meetings)
    return [LiveMeetingOut(**hydrate_live_meeting(meeting)) for meeting in meetings]


@router.get("/materials", response_model=List[StudyMaterialOut])
def get_student_study_materials(
    subject_id: Optional[int] = Query(None),
    current_user: UserOut = Depends(require_student)
):
    """
    [Student Only] Access & download uploaded study notes, textbooks, and resources.
    """
    enrollment = next(
        (e for e in firestore_student_enrollments.query_documents("student_id", "==", current_user.id)),
        None
    )
    if not enrollment:
        return []

    materials = firestore_materials.query_documents("class_id", "==", enrollment.get("class_id"))
    if subject_id is not None:
        materials = [m for m in materials if m.get("subject_id") == subject_id]
    materials.sort(key=lambda m: m.get("uploaded_at"), reverse=True)
    prefetch_academic(materials)
    return [StudyMaterialOut(**hydrate_study_material(material)) for material in materials]


@router.get("/grades", response_model=List[ExamGradeOut])
def get_student_exam_grades(
    subject_id: Optional[int] = Query(None),
    current_user: UserOut = Depends(require_student)
):
    """
    [Student Only] View personal evaluation marks, exam grades, and teacher remarks.
    """
    grades = firestore_grades.query_documents("student_id", "==", current_user.id)
    if subject_id is not None:
        grades = [g for g in grades if g.get("subject_id") == subject_id]
    grades.sort(key=lambda g: g.get("created_at"), reverse=True)
    prefetch_academic(grades)
    return [ExamGradeOut(**hydrate_exam_grade(grade)) for grade in grades]
