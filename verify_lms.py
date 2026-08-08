import json
import logging
from fastapi.testclient import TestClient
from app.main import app

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("verify_lms")

client = TestClient(app)

def run_verification():
    print("\n==========================================")
    print("🚀 LMS BACKEND SYSTEM VERIFICATION SUITE")
    print("==========================================\n")

    # 1. Health Check
    res = client.get("/")
    assert res.status_code == 200, f"Health check failed: {res.text}"
    print("✅ 1. Health Check Endpoint Passed:", res.json())

    # 2. Login as Admin
    login_res = client.post("/api/v1/auth/login", json={
        "email": "admin@lms.com",
        "password": "admin123"
    })
    assert login_res.status_code == 200, f"Admin login failed: {login_res.text}"
    admin_token = login_res.json()["access_token"]
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    print("✅ 2. Admin JWT Authentication Passed")

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
    print("✅ 6. Admin Teacher-Subject-Class Mapping Passed")

    # 7. Admin Enrolls Student
    enroll_res = client.post("/api/v1/admin/enrollments", json={
        "student_id": student_id,
        "class_id": class_id
    }, headers=admin_headers)
    assert enroll_res.status_code == 201
    print("✅ 7. Admin Student Enrollment Passed")

    # 8. Teacher Login
    t_login = client.post("/api/v1/auth/login", json={
        "email": "grace.hopper@lms.com",
        "password": "teacherpass123"
    })
    assert t_login.status_code == 200
    t_headers = {"Authorization": f"Bearer {t_login.json()['access_token']}"}

    # 9. Teacher Independent Operations
    # 9a. Attendance
    att_res = client.post("/api/v1/teachers/attendance", json={
        "class_id": class_id,
        "subject_id": subject_id,
        "date": "2026-08-08",
        "attendance_list": [
            {"student_id": student_id, "status": "PRESENT", "remarks": "Punctual"}
        ]
    }, headers=t_headers)
    assert att_res.status_code == 201
    print("✅ 8a. Teacher Independent Attendance Entry Passed")

    # 9b. Topic Log
    topic_res = client.post("/api/v1/teachers/topics", json={
        "class_id": class_id,
        "subject_id": subject_id,
        "topic_title": "Binary Search Trees",
        "description": "Tree Traversal and Balancing",
        "completion_percentage": 100.0
    }, headers=t_headers)
    assert topic_res.status_code == 201
    print("✅ 8b. Teacher Independent Topic Progress Logging Passed")

    # 9c. Meeting Scheduler
    meet_res = client.post("/api/v1/teachers/meetings", json={
        "class_id": class_id,
        "subject_id": subject_id,
        "title": "Live Lecture: Graph Algorithms",
        "meeting_link": "https://meet.google.com/abc-defg-hij",
        "recording_url": "https://drive.google.com/file/d/sample-id/view",
        "scheduled_time": "2026-08-10T10:00:00",
        "status": "SCHEDULED"
    }, headers=t_headers)
    assert meet_res.status_code == 201
    print("✅ 8c. Teacher Independent Meeting & Recording Link Creation Passed")

    # 9d. Grade Entry
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
    print("✅ 8d. Teacher Independent Exam Grade Submission Passed")

    # 10. Student Portal Access
    s_login = client.post("/api/v1/auth/login", json={
        "email": "charlie.brown@lms.com",
        "password": "studentpass123"
    })
    assert s_login.status_code == 200
    s_headers = {"Authorization": f"Bearer {s_login.json()['access_token']}"}

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

    # 11. Admin Monitoring Report
    rep_res = client.get("/api/v1/admin/reports/monitoring", headers=admin_headers)
    assert rep_res.status_code == 200
    print("✅ 10. Admin System Monitoring & Activity Report Passed:")
    print(json.dumps(rep_res.json()["overall_stats"], indent=2))

    print("\n==========================================")
    print("🎉 LMS BACKEND FULLY VERIFIED AND WORKING PERFECTLY!")
    print("==========================================\n")

if __name__ == "__main__":
    run_verification()
