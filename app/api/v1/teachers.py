from datetime import datetime
from typing import List, Optional
from fastapi import APIRouter, Depends, UploadFile, File, Form, HTTPException, status, Query

from app.api.v1.dependencies import require_teacher
from app.core.gcp_services import storage_service
from app.core.firebase import (
    firestore_attendance, firestore_topics, firestore_meetings,
    firestore_materials, firestore_grades, firestore_student_enrollments,
    firestore_teacher_mappings, firestore_users,
    hydrate_attendance, hydrate_topic, hydrate_live_meeting,
    hydrate_study_material, hydrate_exam_grade, hydrate_teacher_mapping
)
from app.schemas.academic import TeacherMappingOut
from app.schemas.user import UserOut
from app.schemas.attendance import BatchAttendanceCreate, AttendanceOut
from app.schemas.topic import TopicCreate, TopicOut
from app.schemas.meeting import LiveMeetingCreate, LiveMeetingOut
from app.schemas.material import StudyMaterialOut
from app.schemas.grade import GradeEntryCreate, ExamGradeOut

router = APIRouter(prefix="/teachers", tags=["Teachers Module"])


@router.get("/my-classes", response_model=List[TeacherMappingOut])
def get_teacher_assigned_classes(
    current_user: UserOut = Depends(require_teacher)
):
    """
    [Teacher Only] Retrieve assigned classes and subjects for logged in teacher.
    """
    mappings = firestore_teacher_mappings.query_documents("teacher_id", "==", current_user.id)
    return [TeacherMappingOut(**hydrate_teacher_mapping(m)) for m in mappings]


@router.get("/classes/{class_id}/students", response_model=List[UserOut])
def get_students_in_class(
    class_id: int,
    current_user: UserOut = Depends(require_teacher)
):
    """
    [Teacher Only] Retrieve list of enrolled students in a class.
    """
    enrollments = firestore_student_enrollments.query_documents("class_id", "==", class_id)
    students = []
    for enrollment in enrollments:
        student = firestore_users.get_document(str(enrollment.get("student_id")))
        if student and student.get("is_active", False):
            students.append(UserOut(**student))
    return students


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
    [Teacher Only] View attendance history logged by teacher.
    """
    records = firestore_attendance.query_documents("teacher_id", "==", current_user.id)
    if class_id is not None:
        records = [r for r in records if r.get("class_id") == class_id]
    if subject_id is not None:
        records = [r for r in records if r.get("subject_id") == subject_id]
    return [AttendanceOut(**hydrate_attendance(record)) for record in records]


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
    """
    topics = firestore_topics.query_documents("teacher_id", "==", current_user.id)
    if class_id is not None:
        topics = [t for t in topics if t.get("class_id") == class_id]
    if subject_id is not None:
        topics = [t for t in topics if t.get("subject_id") == subject_id]
    topics.sort(key=lambda t: t.get("date_covered"), reverse=True)
    return [TopicOut(**hydrate_topic(topic)) for topic in topics]


@router.post("/meetings", response_model=LiveMeetingOut, status_code=status.HTTP_201_CREATED)
def create_live_meeting(
    meeting_in: LiveMeetingCreate,
    current_user: UserOut = Depends(require_teacher)
):
    """
    [Teacher Only - Independent] Schedule a live meeting link (e.g. Google Meet) or recording link for students.
    Syncs to Firebase Cloud Firestore collection.
    """
    meeting_id = firestore_meetings.get_next_numeric_id()
    meeting_data = {
        "class_id": meeting_in.class_id,
        "subject_id": meeting_in.subject_id,
        "teacher_id": current_user.id,
        "title": meeting_in.title,
        "meeting_link": meeting_in.meeting_link,
        "recording_url": meeting_in.recording_url,
        "scheduled_time": meeting_in.scheduled_time.isoformat(),
        "status": meeting_in.status,
        "created_at": datetime.utcnow().isoformat()
    }
    firestore_meetings.add_document(str(meeting_id), meeting_data)
    meeting_data["id"] = meeting_id
    return LiveMeetingOut(**hydrate_live_meeting(meeting_data))


@router.get("/meetings", response_model=List[LiveMeetingOut])
def list_meetings(
    class_id: Optional[int] = Query(None),
    subject_id: Optional[int] = Query(None),
    current_user: UserOut = Depends(require_teacher)
):
    """
    [Teacher Only] List scheduled meetings and recording links created by this teacher.
    """
    meetings = firestore_meetings.query_documents("teacher_id", "==", current_user.id)
    if class_id is not None:
        meetings = [m for m in meetings if m.get("class_id") == class_id]
    if subject_id is not None:
        meetings = [m for m in meetings if m.get("subject_id") == subject_id]
    meetings.sort(key=lambda m: m.get("scheduled_time"), reverse=True)
    return [LiveMeetingOut(**hydrate_live_meeting(meeting)) for meeting in meetings]


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
    Saves file to Google Cloud Storage / Google Drive or local storage and syncs entry to Firebase.
    """
    file_url = await storage_service.save_file(file=file, folder=f"class_{class_id}/notes")
    material_id = firestore_materials.get_next_numeric_id()
    material_data = {
        "class_id": class_id,
        "subject_id": subject_id,
        "teacher_id": current_user.id,
        "title": title,
        "material_type": material_type,
        "file_url": file_url,
        "uploaded_at": datetime.utcnow().isoformat()
    }
    firestore_materials.add_document(str(material_id), material_data)
    material_data["id"] = material_id
    return StudyMaterialOut(**hydrate_study_material(material_data))


@router.get("/materials", response_model=List[StudyMaterialOut])
def list_uploaded_materials(
    class_id: Optional[int] = Query(None),
    subject_id: Optional[int] = Query(None),
    current_user: UserOut = Depends(require_teacher)
):
    """
    [Teacher Only] View study materials uploaded by this teacher.
    """
    materials = firestore_materials.query_documents("teacher_id", "==", current_user.id)
    if class_id is not None:
        materials = [m for m in materials if m.get("class_id") == class_id]
    if subject_id is not None:
        materials = [m for m in materials if m.get("subject_id") == subject_id]
    return [StudyMaterialOut(**hydrate_study_material(material)) for material in materials]


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
    """
    grades = firestore_grades.query_documents("teacher_id", "==", current_user.id)
    if class_id is not None:
        grades = [g for g in grades if g.get("class_id") == class_id]
    if subject_id is not None:
        grades = [g for g in grades if g.get("subject_id") == subject_id]
    return [ExamGradeOut(**hydrate_exam_grade(grade)) for grade in grades]
