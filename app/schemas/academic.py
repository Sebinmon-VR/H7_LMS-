from pydantic import BaseModel, ConfigDict
from app.schemas.user import UserOut


class ClassRoomCreate(BaseModel):
    name: str
    code: str
    description: str | None = None


class ClassRoomOut(BaseModel):
    id: int
    name: str
    code: str
    description: str | None = None

    model_config = ConfigDict(from_attributes=True)


class SubjectCreate(BaseModel):
    name: str
    code: str
    description: str | None = None


class SubjectOut(BaseModel):
    id: int
    name: str
    code: str
    description: str | None = None

    model_config = ConfigDict(from_attributes=True)


class TeacherMappingCreate(BaseModel):
    teacher_id: int
    subject_id: int
    class_id: int


class TeacherMappingOut(BaseModel):
    id: int
    teacher: UserOut
    subject: SubjectOut
    class_room: ClassRoomOut

    model_config = ConfigDict(from_attributes=True)


class StudentEnrollmentCreate(BaseModel):
    student_id: int
    class_id: int


class StudentEnrollmentOut(BaseModel):
    id: int
    student: UserOut
    class_room: ClassRoomOut

    model_config = ConfigDict(from_attributes=True)
