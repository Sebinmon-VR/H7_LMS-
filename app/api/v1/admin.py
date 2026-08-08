from datetime import datetime
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status, Query

from app.api.v1.dependencies import require_admin
from app.core.security import hash_password
from app.core.enums import UserRole, AttendanceStatus
from app.core.firebase import (
    firestore_users, firestore_classes, firestore_subjects,
    firestore_teacher_mappings, firestore_student_enrollments,
    firestore_attendance, firestore_topics, firestore_meetings,
    firestore_materials, firestore_grades,
    hydrate_teacher_mapping, hydrate_student_enrollment
)
from app.schemas.user import UserCreate, UserUpdate, UserOut
from app.schemas.academic import (
    ClassRoomCreate, ClassRoomOut,
    SubjectCreate, SubjectOut,
    TeacherMappingCreate, TeacherMappingOut,
    StudentEnrollmentCreate, StudentEnrollmentOut
)
from app.schemas.reports import (
    SystemMonitoringReport, OverallStats,
    TeacherActivityReport, StudentPerformanceReport
)

router = APIRouter(prefix="/admin", tags=["Admin Module"])


@router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_user(user_in: UserCreate, _: UserOut = Depends(require_admin)):
    """
    [Admin Only] Create a new system user (Student, Teacher, or Admin) and sync to Firebase.
    """
    existing = firestore_users.get_document_by_field("email", user_in.email)
    if existing:
        raise HTTPException(status_code=400, detail="User with this email already exists")

    user_id = firestore_users.get_next_numeric_id()
    user_data = {
        "full_name": user_in.full_name,
        "email": user_in.email,
        "hashed_password": hash_password(user_in.password),
        "role": user_in.role.value,
        "is_active": True,
        "created_at": datetime.utcnow().isoformat()
    }
    firestore_users.add_document(str(user_id), user_data)
    user_data["id"] = user_id
    return UserOut(**user_data)


@router.get("/users", response_model=List[UserOut])
def list_users(
    role: Optional[UserRole] = Query(None, description="Filter by role: STUDENT, TEACHER, ADMIN"),
    _: UserOut = Depends(require_admin)
):
    """
    [Admin Only] List all users with optional role filter.
    """
    if role:
        users = firestore_users.query_documents("role", "==", role.value)
    else:
        users = firestore_users.list_all()
    return [UserOut(**u) for u in users]


@router.put("/users/{user_id}", response_model=UserOut)
def update_user(
    user_id: int,
    user_in: UserUpdate,
    _: UserOut = Depends(require_admin)
):
    """
    [Admin Only] Update user details or active status.
    """
    user = firestore_users.get_document(str(user_id))
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    updated = {}
    if user_in.full_name is not None:
        updated["full_name"] = user_in.full_name
    if user_in.email is not None:
        updated["email"] = user_in.email
    if user_in.is_active is not None:
        updated["is_active"] = user_in.is_active

    firestore_users.add_document(str(user_id), updated)
    user = firestore_users.get_document(str(user_id))
    return UserOut(**user)


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(user_id: int, _: UserOut = Depends(require_admin)):
    """
    [Admin Only] Deactivate a user account.
    """
    user = firestore_users.get_document(str(user_id))
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    firestore_users.add_document(str(user_id), {"is_active": False})
    return None


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


@router.post("/mappings/teacher-subject-class", response_model=TeacherMappingOut, status_code=status.HTTP_201_CREATED)
def map_teacher_to_class(
    mapping_in: TeacherMappingCreate,
    _: UserOut = Depends(require_admin)
):
    """
    [Admin Only] Assign a Teacher to teach a specific Subject in a Class.
    """
    teacher = firestore_users.get_document(str(mapping_in.teacher_id))
    if not teacher or UserRole(teacher.get("role")) != UserRole.TEACHER:
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
    return [TeacherMappingOut(**hydrate_teacher_mapping(m)) for m in mappings]


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
    return [StudentEnrollmentOut(**hydrate_student_enrollment(e)) for e in enrollments]


@router.get("/reports/monitoring", response_model=SystemMonitoringReport)
def get_system_monitoring_report(_: UserOut = Depends(require_admin)):
    """
    [Admin Only] Analytics and System Monitoring Report.
    Calculates overall system metrics, teacher activity tracking logs, and student performance statistics.
    """
    students = [u for u in firestore_users.query_documents("role", "==", UserRole.STUDENT.value) if u.get("is_active", False)]
    teachers = [u for u in firestore_users.query_documents("role", "==", UserRole.TEACHER.value) if u.get("is_active", False)]

    total_students = len(students)
    total_teachers = len(teachers)
    total_classes = len(firestore_classes.list_all())
    total_subjects = len(firestore_subjects.list_all())
    total_materials = len(firestore_materials.list_all())
    total_attendance = len(firestore_attendance.list_all())

    overall = OverallStats(
        total_students=total_students,
        total_teachers=total_teachers,
        total_classes=total_classes,
        total_subjects=total_subjects,
        total_materials_uploaded=total_materials,
        total_attendance_logs=total_attendance
    )

    teacher_reports = []
    for t in teachers:
        assigned_count = len(firestore_teacher_mappings.query_documents("teacher_id", "==", t["id"]))
        topics_count = len(firestore_topics.query_documents("teacher_id", "==", t["id"]))
        materials_count = len(firestore_materials.query_documents("teacher_id", "==", t["id"]))
        att_count = len(firestore_attendance.query_documents("teacher_id", "==", t["id"]))

        teacher_reports.append(TeacherActivityReport(
            teacher_id=t["id"],
            teacher_name=t["full_name"],
            assigned_classes_count=assigned_count,
            topics_covered_count=topics_count,
            materials_uploaded_count=materials_count,
            attendance_marked_count=att_count
        ))

    student_reports = []
    for s in students:
        enrollment = next(
            (e for e in firestore_student_enrollments.query_documents("student_id", "==", s["id"])),
            None
        )
        class_name = "Unassigned"
        if enrollment:
            class_doc = firestore_classes.get_document(str(enrollment.get("class_id")))
            class_name = class_doc.get("name") if class_doc else "Unassigned"

        attendance_records = firestore_attendance.query_documents("student_id", "==", s["id"])
        total_att = len(attendance_records)
        present_att = sum(1 for a in attendance_records if a.get("status") == AttendanceStatus.PRESENT.value)
        att_percentage = (present_att / total_att * 100.0) if total_att > 0 else 0.0

        grades = firestore_grades.query_documents("student_id", "==", s["id"])
        exam_count = len(grades)
        avg_grade = (
            sum((g.get("marks_obtained", 0.0) / g.get("max_marks", 1.0)) * 100.0 for g in grades) / exam_count
        ) if exam_count > 0 else 0.0

        student_reports.append(StudentPerformanceReport(
            student_id=s["id"],
            student_name=s["full_name"],
            class_name=class_name,
            attendance_percentage=round(att_percentage, 2),
            average_grade_percentage=round(avg_grade, 2),
            total_exams_taken=exam_count
        ))

    return SystemMonitoringReport(
        overall_stats=overall,
        teacher_activity=teacher_reports,
        student_performance=student_reports
    )
