from typing import List
from pydantic import BaseModel


class OverallStats(BaseModel):
    total_students: int
    total_teachers: int
    total_classes: int
    total_subjects: int
    total_materials_uploaded: int
    total_attendance_logs: int


class TeacherActivityReport(BaseModel):
    teacher_id: int
    teacher_name: str
    assigned_classes_count: int
    topics_covered_count: int
    materials_uploaded_count: int
    attendance_marked_count: int


class StudentPerformanceReport(BaseModel):
    student_id: int
    student_name: str
    class_name: str
    attendance_percentage: float
    average_grade_percentage: float
    total_exams_taken: int


class SystemMonitoringReport(BaseModel):
    overall_stats: OverallStats
    teacher_activity: List[TeacherActivityReport]
    student_performance: List[StudentPerformanceReport]
