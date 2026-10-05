from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field
from app.schemas.user import UserOut


class ClassRoomCreate(BaseModel):
    name: str
    code: str
    description: str | None = None


class ClassRoomUpdate(BaseModel):
    """Partial update payload for a class; omitted fields are left unchanged."""
    name: str | None = None
    code: str | None = None
    description: str | None = None


class ClassRoomOut(BaseModel):
    id: int
    name: str
    code: str
    description: str | None = None

    # The class's standing live-class room - one link the whole class shares, joined inside
    # the LMS (/call/class/<id>), which every subject teacher joins at their period. All
    # optional: a class created before rooms existed, or one whose room has not been set up,
    # simply carries none. `room_status` is NONE, CREATED (an ACS room the LMS made), MANUAL
    # (a pasted link) or FAILED (creation was attempted; `room_error` says why).
    # `room_provider` is AZURE_ACS or MANUAL; GOOGLE_MEET marks a room from before the move
    # to Azure, which is replaced by an ACS room on next need.
    room_link: str | None = None
    room_provider: str | None = None
    room_status: str | None = None
    room_error: str | None = None
    room_owner_id: int | None = None
    room_owner_email: str | None = None
    room_auto_record: bool | None = None
    room_recording_status: str | None = None
    # The Google Meet era's guest list and access setting. An ACS room admits exactly the
    # people the LMS lets in at the moment they join, so there is no list to keep and these
    # stay empty on rooms made since the move to Azure.
    room_guest_emails: list[str] | None = None
    room_guest_error: str | None = None
    room_access_type: str | None = None
    room_access_error: str | None = None
    room_created_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class ClassRoomSetup(BaseModel):
    """
    Body of `POST /admin/classes/{id}/room`.

    Leave everything blank to create the class's live-class room in the LMS (an Azure
    Communication Services room, joined at /call/class/<id>). Its recordings are filed into
    Blob storage under the class. `manual_link` skips the LMS room entirely and records a
    link the school already has (Zoom, Teams); nothing outside the LMS can be recorded.
    """
    owner_id: int | None = Field(
        None, description="Accepted for older clients and ignored: an LMS room has no owner."
    )
    auto_record: bool = Field(
        True, description="Record each teacher's period in the room, starting when they join."
    )
    manual_link: str | None = Field(None, max_length=2048, description="Use this link as-is.")
    replace: bool = Field(
        False, description="Replace an existing room. The old room is deleted."
    )


class SubjectCreate(BaseModel):
    name: str
    code: str
    description: str | None = None


class SubjectUpdate(BaseModel):
    """Partial update payload for a subject; omitted fields are left unchanged."""
    name: str | None = None
    code: str | None = None
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


class ClassTeacherMappingCreate(BaseModel):
    teacher_id: int
    class_id: int


class ClassTeacherMappingOut(BaseModel):
    id: int
    teacher: UserOut
    class_room: ClassRoomOut
    assigned_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class StudentEnrollmentCreate(BaseModel):
    student_id: int
    class_id: int


class StudentEnrollmentOut(BaseModel):
    id: int
    student: UserOut
    class_room: ClassRoomOut

    model_config = ConfigDict(from_attributes=True)
