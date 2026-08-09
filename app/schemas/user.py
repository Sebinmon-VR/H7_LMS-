from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.enums import Gender, UserRole


class UserProfileFields(BaseModel):
    """
    The optional profile detail every user may carry, shared by create and update.

    Nothing here is required. A school onboarding a hundred students has partial data for
    most of them, and refusing the record until every field is filled just pushes the gap
    into a spreadsheet somewhere. Fields are grouped by who they apply to, but none are
    enforced per-role: a teacher with a guardian contact is odd, not invalid, and the
    frontend decides which sections to render from `role`.
    """

    # Contact and personal detail - applies to every role.
    phone: str | None = Field(None, max_length=32, description="Primary contact number")
    alternate_phone: str | None = Field(None, max_length=32)
    date_of_birth: date | None = None
    gender: Gender | None = None
    photo_url: str | None = Field(None, max_length=2048, description="Profile picture URL")

    # Address.
    address_line1: str | None = Field(None, max_length=200)
    address_line2: str | None = Field(None, max_length=200)
    city: str | None = Field(None, max_length=100)
    state: str | None = Field(None, max_length=100)
    postal_code: str | None = Field(None, max_length=20)
    country: str | None = Field(None, max_length=100)

    # Student detail.
    admission_number: str | None = Field(None, max_length=50, description="Unique per school")
    roll_number: str | None = Field(None, max_length=50, description="Unique within a class")
    admission_date: date | None = None
    blood_group: str | None = Field(None, max_length=8, description="e.g. O+, AB-")
    guardian_name: str | None = Field(None, max_length=150)
    guardian_phone: str | None = Field(None, max_length=32)
    guardian_email: str | None = Field(None, max_length=320)
    guardian_relation: str | None = Field(None, max_length=50, description="e.g. Father, Mother")

    # Teacher and staff detail.
    employee_id: str | None = Field(None, max_length=50, description="Unique per school")
    designation: str | None = Field(None, max_length=100)
    qualification: str | None = Field(None, max_length=200)
    specialization: str | None = Field(None, max_length=200)
    date_of_joining: date | None = None
    experience_years: float | None = Field(None, ge=0, le=80)

    # Free-form and preferences.
    notes: str | None = Field(None, max_length=2000, description="Internal admin notes")
    # Class reminder emails are opt-out rather than opt-in: a school enrolling a student
    # intends for them to be told when class starts.
    reminder_opt_in: bool | None = Field(
        None, description="Receive timetable reminder emails. Defaults to true on creation."
    )

    @field_validator(
        "phone", "alternate_phone", "guardian_phone",
        "admission_number", "roll_number", "employee_id",
        mode="before",
    )
    @classmethod
    def _strip_identifier(cls, value):
        """
        Trims whitespace and turns an empty string into None.

        A form that submits "" for every untouched field would otherwise write empty strings
        into Firestore, which then defeat the uniqueness checks on admission and employee
        numbers - two blank ids would read as a genuine collision.
        """
        if isinstance(value, str):
            cleaned = value.strip()
            return cleaned or None
        return value

    @field_validator("guardian_email", mode="before")
    @classmethod
    def _normalize_guardian_email(cls, value):
        if isinstance(value, str):
            cleaned = value.strip().lower()
            return cleaned or None
        return value


class UserCreate(UserProfileFields):
    """
    Validation schema for registering or creating a new user (Student, Teacher, or Admin).

    `email` may be omitted, in which case it is generated as firstname.lastname@<domain>
    from `full_name` using USER_EMAIL_DOMAIN, with a numeric suffix on collision.

    `password` is forwarded to Firebase Auth and never stored by this backend. Omit it to
    provision an account without a password; the admin then issues one with
    `POST /admin/users/{user_id}/generate-credentials`, which mails it to the user.

    Every field inherited from UserProfileFields is optional, so the original
    `{full_name, role}` payload still creates a user unchanged.
    """
    full_name: str = Field(..., min_length=1, max_length=150)
    email: str | None = None
    password: str | None = None
    role: UserRole = UserRole.STUDENT

    @field_validator("full_name", mode="before")
    @classmethod
    def _strip_name(cls, value):
        return value.strip() if isinstance(value, str) else value


class GenerateCredentialsRequest(BaseModel):
    """
    Options for issuing login credentials to an existing user.

    `send_email` off is the escape hatch for a user whose mailbox does not exist yet: the
    password comes back in the response for the admin to hand over directly.
    """
    send_email: bool = True
    revoke_sessions: bool = True


class CredentialsIssued(BaseModel):
    """
    Result of issuing credentials.

    `password` is returned so the admin UI can display it once; it is not stored anywhere
    by this backend and cannot be retrieved again.
    """
    user_id: int
    full_name: str
    email: str
    role: UserRole
    password: str
    email_sent: bool
    detail: str


class UserUpdate(UserProfileFields):
    """
    Partial update for a user profile; omitted fields are left unchanged.

    Sending an explicit `null` is not a way to clear a field - omitted and null are treated
    alike, because a partial form submission that defaults missing inputs to null would
    otherwise wipe data the editor never saw.
    """
    full_name: str | None = Field(None, min_length=1, max_length=150)
    email: str | None = None
    is_active: bool | None = None
    role: UserRole | None = Field(
        None, description="Changing this re-issues the user's Firebase role claims"
    )


class UserOut(BaseModel):
    """
    Response schema returning non-sensitive user metadata.

    Profile fields are all nullable, so a client written against the previous shape keeps
    working: it simply ignores the additions.
    """
    id: int
    full_name: str
    email: str
    role: UserRole
    is_active: bool
    created_at: datetime
    firebase_uid: str | None = None

    phone: str | None = None
    alternate_phone: str | None = None
    date_of_birth: date | None = None
    gender: Gender | None = None
    photo_url: str | None = None

    address_line1: str | None = None
    address_line2: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    country: str | None = None

    admission_number: str | None = None
    roll_number: str | None = None
    admission_date: date | None = None
    blood_group: str | None = None
    guardian_name: str | None = None
    guardian_phone: str | None = None
    guardian_email: str | None = None
    guardian_relation: str | None = None

    employee_id: str | None = None
    designation: str | None = None
    qualification: str | None = None
    specialization: str | None = None
    date_of_joining: date | None = None
    experience_years: float | None = None

    notes: str | None = None
    reminder_opt_in: bool | None = None
    updated_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class UserDeleted(BaseModel):
    """
    Outcome of a permanent user delete.

    Returned instead of a bare 204 because a hard delete removes rows across several
    collections and the admin needs to see what actually went; `firebase_auth_deleted`
    being false means an orphaned login may still exist and needs manual cleanup.
    """
    user_id: int
    email: str | None = None
    full_name: str | None = None
    mode: str = Field(..., description="SOFT or PERMANENT")
    firebase_auth_deleted: bool = False
    deleted_records: dict[str, int] = Field(
        default_factory=dict, description="Collection label -> number of records removed"
    )
    detail: str
