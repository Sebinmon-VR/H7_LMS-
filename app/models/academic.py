from dataclasses import dataclass, field
from datetime import datetime


@dataclass(slots=True)
class ClassRoom:
    """
    Class room entity representing a school section or class.
    """
    name: str
    code: str
    description: str | None = None
    id: int | None = None


@dataclass(slots=True)
class Subject:
    """
    Subject entity representing a course subject.
    """
    name: str
    code: str
    description: str | None = None
    id: int | None = None


@dataclass(slots=True)
class TeacherSubjectClassMapping:
    """
    Mapping linking a teacher to a specific subject and class.
    """
    teacher_id: int
    subject_id: int
    class_id: int
    id: int | None = None


@dataclass(slots=True)
class StudentEnrollment:
    """
    Enrollment mapping students to the class they belong to.
    """
    student_id: int
    class_id: int
    enrolled_at: datetime = field(default_factory=datetime.utcnow)
    id: int | None = None
