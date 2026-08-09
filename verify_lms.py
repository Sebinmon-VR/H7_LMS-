import json
import logging
import sys

from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("verify_lms")

client = TestClient(app)


def login(email: str, password: str) -> dict:
    """
    Signs in and returns auth headers.

    Login is delegated to Firebase Auth, so the dev-only password endpoint needs
    FIREBASE_WEB_API_KEY. Without it the endpoint answers 501 and this suite cannot run.
    """
    res = client.post("/api/v1/auth/login", json={"email": email, "password": password})

    if res.status_code == 501:
        print("\n❌ Password login is not configured on this server.")
        print("   Authentication now runs through Firebase Auth. To run this suite, set")
        print("   FIREBASE_WEB_API_KEY in .env (Firebase Console -> Project settings ->")
        print("   General -> Web API Key), then re-run.")
        sys.exit(1)

    assert res.status_code == 200, f"Login failed for {email}: {res.text}"
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


def run_verification():
    print("\n==========================================")
    print("🚀 LMS BACKEND SYSTEM VERIFICATION SUITE")
    print("==========================================\n")
    print(f"Auth provider: {settings.AUTH_PROVIDER} | Storage provider: {settings.resolved_storage_provider}")
    print(f"Google Meet auto-creation: {'enabled' if settings.ENABLE_GOOGLE_MEET else 'disabled'}\n")

    # 1. Health Check
    res = client.get("/")
    assert res.status_code == 200, f"Health check failed: {res.text}"
    print("✅ 1. Health Check Endpoint Passed:", res.json())

    # 2. Login as Admin
    admin_headers = login("admin@lms.com", "admin123")
    print("✅ 2. Admin Firebase Authentication Passed")

    # 3. Admin creates Class
    class_res = client.post("/api/v1/admin/classes", json={
        "name": "Computer Science 2026",
        "code": "CS-2026",
        "description": "Batch 2026 CS Dept"
    }, headers=admin_headers)
    assert class_res.status_code == 201
    class_id = class_res.json()["id"]
    print(f"✅ 3. Admin Class Creation Passed (Class ID: {class_id})")

    # 4. Admin creates Subject
    subject_res = client.post("/api/v1/admin/subjects", json={
        "name": "Data Structures & Algorithms",
        "code": "CS201",
        "description": "Arrays, Trees, Graphs, Complexity"
    }, headers=admin_headers)
    assert subject_res.status_code == 201
    subject_id = subject_res.json()["id"]
    print(f"✅ 4. Admin Subject Creation Passed (Subject ID: {subject_id})")

    # 5. Admin creates Teacher and Student
    teacher_res = client.post("/api/v1/admin/users", json={
        "full_name": "Dr. Grace Hopper",
        "email": "grace.hopper@lms.com",
        "password": "teacherpass123",
        "role": "TEACHER"
    }, headers=admin_headers)
    assert teacher_res.status_code == 201
    teacher_id = teacher_res.json()["id"]

    student_res = client.post("/api/v1/admin/users", json={
        "full_name": "Charlie Brown",
        "email": "charlie.brown@lms.com",
        "password": "studentpass123",
        "role": "STUDENT"
    }, headers=admin_headers)
    assert student_res.status_code == 201
    student_id = student_res.json()["id"]
    print(f"✅ 5. Admin User Accounts Created (Teacher ID: {teacher_id}, Student ID: {student_id})")

    # 6. Admin Maps Teacher to Class & Subject
    map_res = client.post("/api/v1/admin/mappings/teacher-subject-class", json={
        "teacher_id": teacher_id,
        "subject_id": subject_id,
        "class_id": class_id
    }, headers=admin_headers)
    assert map_res.status_code == 201
    mapping_id = map_res.json()["id"]
    print("✅ 6. Admin Teacher-Subject-Class Mapping Passed")

    # 7. Admin Enrolls Student
    enroll_res = client.post("/api/v1/admin/enrollments", json={
        "student_id": student_id,
        "class_id": class_id
    }, headers=admin_headers)
    assert enroll_res.status_code == 201
    enrollment_id = enroll_res.json()["id"]
    print("✅ 7. Admin Student Enrollment Passed")

    # 8. Teacher Login
    t_headers = login("grace.hopper@lms.com", "teacherpass123")

    # 8a. Attendance
    att_res = client.post("/api/v1/teachers/attendance", json={
        "class_id": class_id,
        "subject_id": subject_id,
        "date": "2026-08-08",
        "attendance_list": [
            {"student_id": student_id, "status": "PRESENT", "remarks": "Punctual"}
        ]
    }, headers=t_headers)
    assert att_res.status_code == 201
    attendance_id = att_res.json()[0]["id"]
    print("✅ 8a. Teacher Independent Attendance Entry Passed")

    # 8b. Topic Log
    topic_res = client.post("/api/v1/teachers/topics", json={
        "class_id": class_id,
        "subject_id": subject_id,
        "topic_title": "Binary Search Trees",
        "description": "Tree Traversal and Balancing",
        "completion_percentage": 100.0
    }, headers=t_headers)
    assert topic_res.status_code == 201
    topic_id = topic_res.json()["id"]
    print("✅ 8b. Teacher Independent Topic Progress Logging Passed")

    # 8c. Meeting Scheduler (manual link; auto_create_meet is exercised separately)
    meet_res = client.post("/api/v1/teachers/meetings", json={
        "class_id": class_id,
        "subject_id": subject_id,
        "title": "Live Lecture: Graph Algorithms",
        "meeting_link": "https://meet.google.com/abc-defg-hij",
        "recording_url": "https://drive.google.com/file/d/sample-id/view",
        "scheduled_time": "2026-08-10T10:00:00",
        "status": "SCHEDULED",
        "auto_create_meet": False
    }, headers=t_headers)
    assert meet_res.status_code == 201
    meeting_id = meet_res.json()["id"]
    print("✅ 8c. Teacher Independent Meeting & Recording Link Creation Passed")

    # 8d. Grade Entry
    grade_res = client.post("/api/v1/teachers/grades", json={
        "student_id": student_id,
        "class_id": class_id,
        "subject_id": subject_id,
        "exam_name": "Midterm Exam 1",
        "marks_obtained": 95.0,
        "max_marks": 100.0,
        "remarks": "Excellent work"
    }, headers=t_headers)
    assert grade_res.status_code == 201
    grade_id = grade_res.json()["id"]
    print("✅ 8d. Teacher Independent Exam Grade Submission Passed")

    # 9. Student Portal Access
    s_headers = login("charlie.brown@lms.com", "studentpass123")

    s_classes = client.get("/api/v1/students/my-classes", headers=s_headers)
    assert len(s_classes.json()) > 0
    print("✅ 9a. Student Enrolled Classes View Passed")

    s_att = client.get("/api/v1/students/attendance", headers=s_headers)
    assert len(s_att.json()) > 0
    print("✅ 9b. Student Personal Attendance View Passed")

    s_topics = client.get("/api/v1/students/topics", headers=s_headers)
    assert len(s_topics.json()) > 0
    print("✅ 9c. Student Covered Topics View Passed")

    s_meetings = client.get("/api/v1/students/meetings", headers=s_headers)
    assert len(s_meetings.json()) > 0
    print("✅ 9d. Student Live Meetings & Recordings View Passed")

    s_grades = client.get("/api/v1/students/grades", headers=s_headers)
    assert len(s_grades.json()) > 0
    print("✅ 9e. Student Grades Report Card View Passed")

    # 10. Admin Monitoring Report
    rep_res = client.get("/api/v1/admin/reports/monitoring", headers=admin_headers)
    assert rep_res.status_code == 200
    print("✅ 10. Admin System Monitoring & Activity Report Passed:")
    print(json.dumps(rep_res.json()["overall_stats"], indent=2))

    # 11. Update endpoints
    upd_class = client.put(f"/api/v1/admin/classes/{class_id}", json={
        "description": "Batch 2026 CS Dept - updated"
    }, headers=admin_headers)
    assert upd_class.status_code == 200, upd_class.text
    assert upd_class.json()["description"].endswith("updated")

    upd_grade = client.put(f"/api/v1/teachers/grades/{grade_id}", json={
        "marks_obtained": 97.5, "remarks": "Re-evaluated"
    }, headers=t_headers)
    assert upd_grade.status_code == 200, upd_grade.text
    assert upd_grade.json()["marks_obtained"] == 97.5

    over_max = client.put(f"/api/v1/teachers/grades/{grade_id}", json={
        "marks_obtained": 250.0
    }, headers=t_headers)
    assert over_max.status_code == 400, "Grade above max_marks should be rejected"
    print("✅ 11. Update Endpoints & Grade Validation Passed")

    # 12. Delete dependency guard: the class is still referenced
    blocked = client.delete(f"/api/v1/admin/classes/{class_id}", headers=admin_headers)
    assert blocked.status_code == 409, f"Expected 409 for a referenced class, got {blocked.status_code}"
    print(f"✅ 12. Delete Dependency Guard Passed: {blocked.json()['detail'][:90]}...")

    # 13. Ownership enforcement: a student may not delete a teacher's topic
    forbidden = client.delete(f"/api/v1/teachers/topics/{topic_id}", headers=s_headers)
    assert forbidden.status_code == 403, f"Expected 403 for a student, got {forbidden.status_code}"
    print("✅ 13. Role & Ownership Enforcement Passed")

    # 14. Teacher deletes their own records
    for label, path in [
        ("attendance", f"/api/v1/teachers/attendance/{attendance_id}"),
        ("topic", f"/api/v1/teachers/topics/{topic_id}"),
        ("meeting", f"/api/v1/teachers/meetings/{meeting_id}"),
        ("grade", f"/api/v1/teachers/grades/{grade_id}"),
    ]:
        res = client.delete(path, headers=t_headers)
        assert res.status_code == 204, f"Deleting {label} failed: {res.status_code} {res.text}"
    print("✅ 14. Teacher Record Deletions Passed")

    # 15. Admin deletes mapping and enrollment, then the now-unreferenced class
    assert client.delete(f"/api/v1/admin/mappings/teacher-subject-class/{mapping_id}",
                         headers=admin_headers).status_code == 204
    assert client.delete(f"/api/v1/admin/enrollments/{enrollment_id}",
                         headers=admin_headers).status_code == 204

    cleared = client.delete(f"/api/v1/admin/classes/{class_id}", headers=admin_headers)
    assert cleared.status_code == 204, f"Expected 204 once unreferenced, got {cleared.status_code}: {cleared.text}"
    assert client.delete(f"/api/v1/admin/subjects/{subject_id}", headers=admin_headers).status_code == 204
    print("✅ 15. Admin Cascade-Free Deletions Passed")

    # 16. Deactivate and reactivate a user
    assert client.delete(f"/api/v1/admin/users/{student_id}", headers=admin_headers).status_code == 204
    reactivated = client.post(f"/api/v1/admin/users/{student_id}/reactivate", headers=admin_headers)
    assert reactivated.status_code == 200 and reactivated.json()["is_active"] is True
    print("✅ 16. User Soft Delete & Reactivation Passed")

    # 17. Missing resources answer 404
    assert client.delete("/api/v1/admin/classes/99999999", headers=admin_headers).status_code == 404
    print("✅ 17. Not-Found Handling Passed")

    print("\n==========================================")
    print("🎉 LMS BACKEND FULLY VERIFIED AND WORKING PERFECTLY!")
    print("==========================================\n")


if __name__ == "__main__":
    run_verification()
