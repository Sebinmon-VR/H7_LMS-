from datetime import datetime
from pydantic import BaseModel, ConfigDict
from app.schemas.academic import ClassRoomOut, SubjectOut
from app.schemas.user import UserOut


class StudyMaterialUpdate(BaseModel):
    """
    Partial update for a study material's metadata; omitted fields are left unchanged.
    Replacing the underlying file means deleting the material and re-uploading.
    """
    title: str | None = None
    material_type: str | None = None


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
    storage_provider: str | None = None

    # Set when the configured provider could not be used and the file landed elsewhere,
    # so an upload that quietly fell back to local disk is visible in the response.
    storage_warning: str | None = None

    model_config = ConfigDict(from_attributes=True)
