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

    # The class's standing live-class room - one Google Meet link the whole class shares,
    # which every subject teacher joins at their period. All optional: a class created
    # before rooms existed, or one whose room has not been set up, simply carries none.
    # `room_status` is NONE, CREATED (a Meet room the LMS made), MANUAL (a pasted link) or
    # FAILED (creation was attempted; `room_error` says why).
    room_link: str | None = None
    room_provider: str | None = None
    room_status: str | None = None
    room_error: str | None = None
    room_owner_id: int | None = None
    room_owner_email: str | None = None
    room_auto_record: bool | None = None
    room_recording_status: str | None = None
    # Teachers on the room's Calendar guest list - the ones Meet lets in without asking.
    room_guest_emails: list[str] | None = None
    room_guest_error: str | None = None
    # Who may walk in without asking: OPEN (anyone with the link), TRUSTED or RESTRICTED.
    # None means Google has not accepted the setting; `room_access_error` says why.
    room_access_type: str | None = None
    room_access_error: str | None = None
    room_created_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class ClassRoomSetup(BaseModel):
    """
    Body of `POST /admin/classes/{id}/room`.

    Leave everything blank to create a Google Meet room owned by the school's Workspace
    identity. `owner_id` puts it on a particular teacher's calendar instead - the class
    teacher, usually - which is also whose Drive its recordings land in. `manual_link` skips
    Meet entirely and records a link the school already has (Zoom, Teams, an existing Meet).
    """
    owner_id: int | None = Field(None, description="Teacher whose calendar hosts the room.")
    auto_record: bool = Field(True, description="Arm the room to record itself.")
    manual_link: str | None = Field(None, max_length=2048, description="Use this link as-is.")
    replace: bool = Field(
        False, description="Replace an existing room. The old Calendar event is deleted."
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
