from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field
from app.schemas.academic import ClassRoomOut, SubjectOut
from app.schemas.user import UserOut


class StudyMaterialUpdate(BaseModel):
    """
    Partial update for a study material's metadata; omitted fields are left unchanged.
    Replacing the underlying file means deleting the material and re-uploading.
    """
    title: str | None = None
    material_type: str | None = None
    description: str | None = Field(None, max_length=2000)
    # Only for a shared link: point it somewhere else. Ignored on an uploaded file.
    external_url: str | None = Field(None, max_length=2048)


class StudyMaterialLinkCreate(BaseModel):
    """A web page or YouTube video shared into the library instead of an uploaded file."""
    class_id: int
    subject_id: int
    title: str = Field(..., min_length=1, max_length=200)
    external_url: str = Field(..., max_length=2048)
    # Defaults to VIDEO for a YouTube address and LINK for anything else.
    material_type: str | None = None
    description: str | None = Field(None, max_length=2000)


class AdminStudyMaterialLinkCreate(StudyMaterialLinkCreate):
    # Attribute the link to this teacher; defaults to the acting admin.
    teacher_id: int | None = None


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
    # Set on a class recording published into the library (material_type RECORDING): the
    # recording it came from. Deleting such a material unpublishes it and keeps the video.
    recording_key: str | None = None

    # Set on a shared link. `file_url` carries the same address so older screens still open
    # it; `link_kind` is YOUTUBE (with `youtube_id`, playable in place) or WEB.
    external_url: str | None = None
    link_kind: str | None = None
    youtube_id: str | None = None
    description: str | None = None

    model_config = ConfigDict(from_attributes=True)
