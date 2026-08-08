from datetime import datetime
from pydantic import BaseModel, ConfigDict
from app.schemas.academic import ClassRoomOut, SubjectOut
from app.schemas.user import UserOut


class StudyMaterialOut(BaseModel):
    id: int
    class_id: int
    class_room: ClassRoomOut | None = None
    subject_id: int
    subject: SubjectOut | None = None
    teacher_id: int
    teacher: UserOut | None = None
    title: str
    material_type: str
    file_url: str
    uploaded_at: datetime

    model_config = ConfigDict(from_attributes=True)
