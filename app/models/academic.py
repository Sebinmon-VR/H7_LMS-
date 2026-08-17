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
class ClassTeacherMapping:
    """
    Mapping naming the teacher responsible for a class as a whole.

    Deliberately has no `subject_id`: a class teacher's remit is the class, not one period of
    it. That is the difference from TeacherSubjectClassMapping, and it is what lets a class
    teacher see and correct records filed by the other teachers who take the same class.
    """
    teacher_id: int
    class_id: int
    assigned_at: datetime = field(default_factory=datetime.utcnow)
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
