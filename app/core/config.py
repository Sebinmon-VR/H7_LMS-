import base64
import binascii
import json
import logging
import os
import tempfile
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger("config")


class Settings(BaseSettings):
    """
    Centralized Settings Manager powered by Pydantic's BaseSettings.
    Reads, parses, and validates settings from environment variables and `.env` file.
    """
    PROJECT_NAME: str = "LMS Backend API"
    API_V1_STR: str = "/api/v1"
    DEBUG: bool = True

    # Security & JWT Authentication settings
    SECRET_KEY: str = "lms_super_secret_development_key_change_in_production_32bytes"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24  # 24 hours duration

    # Authentication provider settings
    AUTH_PROVIDER: str = "FIREBASE"          # FIREBASE | LEGACY_JWT
    ALLOW_LEGACY_JWT_LOGIN: bool = True      # Transition flag; disable once all clients send Firebase ID tokens
    FIREBASE_WEB_API_KEY: str = ""           # Only used by the dev-only password login helper endpoints

    # Browser origins allowed to call the API.
    # The frontend sends `Authorization: Bearer <token>`, which makes every request
    # preflighted, so an origin missing from here fails at the OPTIONS before any handler
    # runs. Exact origins are comma-separated and scheme-qualified ("https://app.example.com").
    # The regex is the fallback that keeps Static Web Apps preview environments working,
    # since each pull request gets its own generated hostname.
    CORS_ALLOW_ORIGINS: str = ""
    CORS_ALLOW_ORIGIN_REGEX: str = (
        r"https://([a-z0-9-]+\.)*azurestaticapps\.net"
        r"|http://localhost(:\d+)?"
        r"|http://127\.0\.0\.1(:\d+)?"
    )

    @property
    def cors_origins(self) -> list[str]:
        """
        Exact allowed origins.

        A trailing slash is stripped because browsers send the origin without one, and
        "https://app.example.com/" would silently never match.
        """
        raw = (self.CORS_ALLOW_ORIGINS or "").replace(",", " ").split()
        return [origin.strip().rstrip("/") for origin in raw if origin.strip()]

    # Bootstrap administrator.
    # There is no public registration endpoint, so the system needs exactly one account to
    # exist before anyone can sign in and create the rest. This account is created at
    # startup if it is absent, and left untouched if it already exists.
    BOOTSTRAP_ADMIN_EMAIL: str = "admin@h7ed.com"
    BOOTSTRAP_ADMIN_PASSWORD: str = "admin@h7ed"
    BOOTSTRAP_ADMIN_NAME: str = "System Administrator"
    SEED_DEMO_DATA: bool = False             # Demo classes/subjects/students; off by default

    # Database Configuration Settings
    DATABASE_PROVIDER: str = "FIREBASE_FIRESTORE"  # Primary database provider
    DATABASE_URL: str = "sqlite:///./lms.db"       # Secondary local relational fallback

    # Performance tuning
    # Firestore round trips dominate response time from outside the database's region
    # (~850ms each), so reference data is cached and independent queries run concurrently.
    REFERENCE_CACHE_TTL_SECONDS: float = 60.0
    QUERY_CONCURRENCY: int = 8
    MONITORING_REPORT_TTL_SECONDS: float = 300.0

    # Google Cloud & Firebase Integration Settings
    GCP_PROJECT_ID: str = "lms-demo-project"
    GCP_BUCKET_NAME: str = "lms-study-materials-bucket"
    GOOGLE_APPLICATION_CREDENTIALS: str = "./service_account.json"
    FIREBASE_CREDENTIALS_PATH: str = "./firebase_credentials.json"
    # The service account key as a *value* rather than a file, for hosts where there is no
    # credentials file to point at. The deployment artifact is built from a repository
    # checkout and the key files are gitignored, so on App Service the paths above resolve
    # to nothing and every Firestore read silently returns empty. Set this application
    # setting to the contents of firebase_credentials.json (raw JSON on one line, or the
    # same bytes base64-encoded) and it is written to a private temp file at startup that
    # the two path settings then point at.
    FIREBASE_CREDENTIALS_JSON: str = ""
    USE_FIREBASE_DB: bool = True
    USE_LOCAL_STORAGE: bool = False
    LOCAL_STORAGE_DIR: str = "./uploads"

    # Google Workspace integration settings (requires domain-wide delegation)
    GOOGLE_WORKSPACE_DOMAIN: str = ""
    GOOGLE_IMPERSONATION_FALLBACK: str = ""  # Workspace user impersonated when a teacher email is unusable
    ENABLE_GOOGLE_MEET: bool = True
    GOOGLE_CALENDAR_ID: str = "primary"
    # Calendar rejects a naive dateTime unless a timeZone accompanies it, so every event
    # carries this IANA zone. Times that already include a UTC offset keep their own.
    GOOGLE_CALENDAR_TIMEZONE: str = "Asia/Dubai"
    # Turn off on a non-Workspace project: the service account then writes to a calendar
    # shared directly with it instead of impersonating a user (which requires delegation).
    GOOGLE_CALENDAR_IMPERSONATION: bool = True
    # Comma-separated OAuth scopes to request for Calendar. Leave blank to let the client
    # probe the usual candidates and settle on whichever set delegation actually grants:
    # Google fails the whole token exchange if *any* requested scope is unauthorized, so
    # asking for more than was authorized breaks a working setup.
    GOOGLE_CALENDAR_SCOPES: str = ""
    # Invite enrolled students as Calendar attendees. Non-Workspace projects cannot invite
    # external guests, so this can be turned off without losing the Meet link itself.
    GOOGLE_MEET_INVITE_ATTENDEES: bool = True

    # Automatic class recording (Google Meet REST API v2).
    #
    # Every generated Meet conference is configured to record itself, so a teacher never has
    # to remember to press record. Requires the Meet API enabled on the GCP project, the
    # meetings.space.settings and meetings.space.readonly scopes authorized for the service
    # account under domain-wide delegation, and a Workspace edition that can record at all
    # (Business Standard/Plus, Enterprise, Education Plus, Teaching & Learning Upgrade).
    ENABLE_MEET_AUTO_RECORDING: bool = True
    # Comma-separated override for the Meet OAuth scopes, in the same spirit as
    # GOOGLE_CALENDAR_SCOPES: leave blank to let the client settle on whichever set the
    # delegation grant actually authorizes.
    MEET_RECORDING_SCOPES: str = ""
    # Meet writes the video into the meeting organiser's own Drive. What the LMS then does
    # with it: MOVE it into the Shared Drive (organisation-owned, no duplicate storage),
    # COPY it there (the teacher keeps the original), or LINK to it where it lies.
    RECORDING_TRANSFER_MODE: str = "MOVE"
    # Folder under the Drive root that holds the per-class recording folders.
    RECORDING_DRIVE_FOLDER_NAME: str = "Class Recordings"
    # Grant each enrolled student read access to their class's recording.
    RECORDING_SHARE_WITH_STUDENTS: bool = True
    # How long after a session's scheduled end to start looking for its recording. Meet needs
    # a few minutes to finish writing the file, and asking sooner just burns quota.
    RECORDING_HARVEST_DELAY_MINUTES: int = 5
    # How often the background sweep looks for finished sessions whose recording has landed.
    RECORDING_SCAN_INTERVAL_SECONDS: float = 300.0
    # A session with no recording this long after it ended is marked UNAVAILABLE and stops
    # being polled - a class nobody attended produces no recording, and retrying it forever
    # would cost a Meet API call per sweep for the life of the deployment.
    RECORDING_MAX_AGE_HOURS: int = 48

    # Storage backend selection
    STORAGE_PROVIDER: str = "GCS"            # GCS | DRIVE | LOCAL
    GOOGLE_DRIVE_SHARED_DRIVE_ID: str = ""
    # Alternative to a Shared Drive: the ID of an ordinary Drive folder shared with the
    # service account (or owned by GOOGLE_IMPERSONATION_FALLBACK when impersonating).
    GOOGLE_DRIVE_FOLDER_ID: str = ""
    GOOGLE_DRIVE_ROOT_FOLDER_NAME: str = "H7 LMS Materials"
    # Impersonate GOOGLE_IMPERSONATION_FALLBACK for Drive calls. Required when uploading
    # into a My Drive folder (a bare service account has no personal Drive quota).
    GOOGLE_DRIVE_IMPERSONATION: bool = True
    # Grant "anyone with the link can view" on uploads so students can open a material
    # without a Workspace account. Turn off for domain-only distribution.
    GOOGLE_DRIVE_LINK_SHARING: bool = True
    # When True an upload that cannot reach the configured cloud provider returns 502
    # instead of quietly landing on the API server's local disk.
    STORAGE_STRICT: bool = False

    # Timetable and class reminders.
    # Timetable periods are wall-clock facts ("assembly is at 08:00"), so they are stored as
    # local times and resolved against this zone when the reminder sweep decides what is
    # about to start. Falls back to the calendar zone so a deployment sets it once.
    SCHOOL_TIMEZONE: str = ""
    ENABLE_CLASS_REMINDERS: bool = True
    # How far ahead of a period students are emailed. A list sends several nudges, e.g.
    # "60,15" for a day-planning reminder and a get-to-class one.
    CLASS_REMINDER_MINUTES_BEFORE: str = "15"
    # How often the background sweep looks for periods entering a reminder window. Must be
    # smaller than the tightest reminder offset or a window can be stepped over entirely.
    REMINDER_SCAN_INTERVAL_SECONDS: float = 120.0
    # Also email the teacher taking the period, not just the enrolled students.
    REMIND_TEACHERS: bool = True
    # Reminders for a period more than this many minutes in the past are never sent, so a
    # server that was down all morning does not flood everyone on restart.
    REMINDER_MAX_LATENESS_MINUTES: int = 10

    @property
    def resolved_school_timezone(self) -> str:
        """IANA zone timetable periods are interpreted in."""
        return (self.SCHOOL_TIMEZONE or self.GOOGLE_CALENDAR_TIMEZONE or "UTC").strip()

    @property
    def reminder_offsets(self) -> list[int]:
        """
        Minutes-before values to send reminders at, largest first and de-duplicated.

        Parsed leniently from a comma or space separated string: an unreadable entry is
        dropped rather than taking the scheduler down at startup.
        """
        raw = (self.CLASS_REMINDER_MINUTES_BEFORE or "").replace(",", " ").split()
        offsets = set()
        for token in raw:
            try:
                value = int(token)
            except ValueError:
                continue
            if value >= 0:
                offsets.add(value)
        return sorted(offsets, reverse=True) or [15]

    @property
    def recording_transfer_mode(self) -> str:
        """
        What to do with a finished recording: MOVE, COPY or LINK.

        An unrecognised value falls back to MOVE rather than raising. A typo here would
        otherwise take down the whole recording sweep, and the safe reading of "the school
        wants recordings in its Drive" is to put them there.
        """
        mode = (self.RECORDING_TRANSFER_MODE or "").strip().upper()
        return mode if mode in {"MOVE", "COPY", "LINK"} else "MOVE"

    # Credential provisioning.
    # Admins create users by name; the login email is derived as firstname.lastname@domain
    # and the password is generated. Falls back to the Workspace domain when unset.
    USER_EMAIL_DOMAIN: str = ""
    GENERATED_PASSWORD_LENGTH: int = 12
    LMS_LOGIN_URL: str = ""                  # Included in the credentials email when set

    # Outbound email (Gmail / Google Workspace SMTP).
    # SMTP_PASSWORD must be a Google App Password, not the account password.
    ENABLE_EMAIL_NOTIFICATIONS: bool = True
    SMTP_HOST: str = "smtp.gmail.com"
    SMTP_PORT: int = 587                     # 587 = STARTTLS, 465 = implicit TLS
    SMTP_USE_TLS: bool = True
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM: str = ""                      # Defaults to SMTP_USER
    SMTP_FROM_NAME: str = ""                 # Defaults to PROJECT_NAME
    SMTP_TIMEOUT_SECONDS: float = 20.0

    @property
    def resolved_user_email_domain(self) -> str:
        """
        Domain used for generated login emails. Prefers the dedicated setting so LMS
        accounts can live on a different domain than the Workspace integration.
        """
        return (self.USER_EMAIL_DOMAIN or self.GOOGLE_WORKSPACE_DOMAIN or "").strip().lstrip("@")

    @property
    def resolved_storage_provider(self) -> str:
        """
        Effective storage provider.

        An explicitly configured STORAGE_PROVIDER wins. The legacy USE_LOCAL_STORAGE flag
        still forces LOCAL for deployments that only ever set that variable, but it no
        longer silently overrides an explicit STORAGE_PROVIDER=DRIVE - a combination that
        looks like "Drive is configured" while every upload quietly lands on local disk,
        and is the single most confusing way for cloud uploads to appear broken.
        """
        explicit = "STORAGE_PROVIDER" in self.model_fields_set
        if self.USE_LOCAL_STORAGE and not explicit:
            return "LOCAL"
        return (self.STORAGE_PROVIDER or "GCS").upper()

    @property
    def storage_config_conflict(self) -> str | None:
        """Describes a contradictory storage configuration, or None when it is coherent."""
        if self.USE_LOCAL_STORAGE and "STORAGE_PROVIDER" in self.model_fields_set:
            if self.STORAGE_PROVIDER.upper() != "LOCAL":
                return (
                    f"USE_LOCAL_STORAGE=True contradicts STORAGE_PROVIDER="
                    f"{self.STORAGE_PROVIDER.upper()}; STORAGE_PROVIDER wins. Set "
                    "USE_LOCAL_STORAGE=False to remove the ambiguity, or "
                    'STORAGE_PROVIDER="LOCAL" to keep files on the API server.'
                )
        return None

    # ---------------------------------------------------------------------------------
    # Online tuition
    #
    # The tuition product is one-to-one: a slot belongs to one student and one teacher, and
    # is billed by the class rather than by the term. These are *deployment defaults*; the
    # values an administrator actually operates on live in the `app_settings` Firestore
    # document and are edited from the admin API, because a school that wants a 45-minute
    # class instead of 60 should not need a redeploy. See `app.services.tuition.settings`.
    # ---------------------------------------------------------------------------------
    ENABLE_TUITION_MODULE: bool = True
    # The zone tuition slot times are written and read in, and the default every user is
    # shown times in until their own is known. Deliberately its own setting rather than
    # inheriting SCHOOL_TIMEZONE: the tuition programme runs on Indian time whatever zone the
    # school itself keeps, and the two products should be able to disagree.
    TUITION_TIMEZONE: str = "Asia/Kolkata"
    # Default length of one one-to-one class, in minutes.
    TUITION_DEFAULT_SESSION_MINUTES: int = 60
    # Minutes before a class that participants are reminded, comma-separated like the LMS
    # equivalent. Kept separate because a one-to-one class wants a short, sharp nudge where
    # a school timetable wants a morning summary.
    TUITION_REMINDER_MINUTES_BEFORE: str = "10"
    ENABLE_TUITION_REMINDERS: bool = True
    # How long after a teacher's late arrival the class may still be extended. Without a cap,
    # a teacher joining two hours late would owe the student a class ending after midnight,
    # colliding with everything scheduled behind it.
    TUITION_MAX_TEACHER_LATE_EXTENSION_MINUTES: int = 30
    # Whether a class opens on schedule by itself, or waits for the teacher to start it.
    #
    # False (default) - the teacher presses start, and that moment is when the class begins.
    #                   A student cannot be late for a class nobody has started.
    # True            - the class opens at its scheduled time whether or not the teacher has
    #                   arrived, and lateness is measured from the timetable.
    #
    # Either way both join times are recorded separately, and a late *teacher* still earns the
    # student extra time. See `app.services.tuition.sessions.class_started_at`.
    TUITION_AUTO_START_CLASS: bool = False
    # A teacher who has not joined this many minutes after the start is treated as a no-show,
    # which makes the class non-billable. Distinct from the extension cap: one decides
    # whether the class still happens, the other how long it may run.
    TUITION_TEACHER_NO_SHOW_MINUTES: int = 15
    # Minutes after the scheduled start beyond which a student's arrival counts as LATE.
    TUITION_STUDENT_LATE_GRACE_MINUTES: int = 5
    # Minimum gap between two consecutive classes for the same person, so a teacher is not
    # scheduled to finish one class and start another in the same minute.
    TUITION_MIN_GAP_MINUTES: int = 0
    # How far ahead the scheduler will materialize concrete sessions from recurring slots.
    TUITION_SESSION_HORIZON_DAYS: int = 30
    # Whether a student may upload to the shared library without a teacher approving it.
    TUITION_STUDENT_UPLOADS_NEED_APPROVAL: bool = True
    # Default per-class fee used when an enrollment carries no fee plan of its own.
    TUITION_DEFAULT_SESSION_FEE: float = 0.0
    TUITION_CURRENCY: str = "INR"
    # Prefixes for the identifiers issued to tuition accounts when none is supplied, as
    # PREFIX-YEAR-0001. Every tuition student carries a unique admission number; leaving an
    # administrator to invent one per student is how blanks and duplicates get into the data.
    TUITION_ADMISSION_PREFIX: str = "TUI"
    TUITION_EMPLOYEE_PREFIX: str = "TUT"

    @property
    def resolved_tuition_timezone(self) -> str:
        """
        IANA zone the tuition programme runs in.

        Falls back through the school's zone rather than straight to UTC, so a deployment
        that has only ever configured one timezone keeps behaving sensibly.
        """
        return (self.TUITION_TIMEZONE or self.resolved_school_timezone or "UTC").strip()

    @property
    def tuition_reminder_offsets(self) -> list[int]:
        """
        Minutes-before values for tuition class reminders, largest first.

        Parsed with the same leniency as `reminder_offsets`, and for the same reason: a typo
        in an environment variable must not stop the reminder sweep from starting.
        """
        raw = (self.TUITION_REMINDER_MINUTES_BEFORE or "").replace(",", " ").split()
        offsets = set()
        for token in raw:
            try:
                value = int(token)
            except ValueError:
                continue
            if value >= 0:
                offsets.add(value)
        return sorted(offsets, reverse=True) or [10]

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )


def _decode_credentials_blob(raw: str) -> dict:
    """
    Parses FIREBASE_CREDENTIALS_JSON, which may arrive as raw JSON or base64-encoded JSON.

    Both forms are accepted because a service account key pasted straight into a portal
    field often picks up line breaks on the way, and base64 is the form that survives every
    settings UI, secret store, and shell that sits between the key and this process.
    """
    text = raw.strip().strip('"').strip("'")
    if not text:
        raise ValueError("value is empty")

    if not text.lstrip().startswith("{"):
        try:
            text = base64.b64decode(text, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError) as exc:
            raise ValueError(
                "value is neither JSON (it does not start with '{') nor valid base64"
            ) from exc

    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("decoded value is not a JSON object")

    missing = [key for key in ("client_email", "private_key", "project_id") if not parsed.get(key)]
    if missing:
        raise ValueError(f"service account JSON is missing {', '.join(missing)}")

    return parsed


def _materialize_service_account(config: Settings) -> None:
    """
    Writes FIREBASE_CREDENTIALS_JSON to a file and repoints the credential path settings at it.

    Every consumer in this codebase — the Firebase Admin SDK, the GCS client, Drive, and
    Calendar — loads credentials from a path, so materializing once here keeps all of them
    working unchanged instead of teaching each one a second way to authenticate. Runs before
    anything imports those clients because they all import this module first.

    A malformed value is logged and ignored rather than raised: a key that cannot be parsed
    should degrade the deployment to the same state it is in today, not stop the API from
    booting and take down the health endpoints that would explain why.
    """
    if not config.FIREBASE_CREDENTIALS_JSON:
        return

    # An existing key file on disk is the developer's local setup and outranks the env var.
    if os.path.exists(config.FIREBASE_CREDENTIALS_PATH):
        logger.info(
            "FIREBASE_CREDENTIALS_JSON ignored: %s already exists on disk.",
            config.FIREBASE_CREDENTIALS_PATH,
        )
        return

    try:
        service_account = _decode_credentials_blob(config.FIREBASE_CREDENTIALS_JSON)
    except (ValueError, json.JSONDecodeError) as exc:
        logger.error(
            "FIREBASE_CREDENTIALS_JSON could not be read (%s). Firestore will be unavailable "
            "and every collection will read as empty.",
            exc,
        )
        return

    target = Path(tempfile.gettempdir()) / "firebase_credentials.json"
    try:
        # 0600, and created before the key is written: the file lives in a world-readable
        # temp directory, so it must never be readable in the window between the two.
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(service_account, handle)
    except OSError as exc:
        logger.error(
            "Could not write the service account key to %s (%s). Firestore will be unavailable.",
            target, exc,
        )
        return

    path = str(target)
    config.FIREBASE_CREDENTIALS_PATH = path
    if not os.path.exists(config.GOOGLE_APPLICATION_CREDENTIALS):
        config.GOOGLE_APPLICATION_CREDENTIALS = path
    # Google's own libraries fall back to this variable when handed no explicit credentials.
    # It is overwritten, not defaulted: the sample configuration ships a placeholder path,
    # so a deployment that copied it has this variable set to a file that does not exist,
    # and leaving that in place sends every ADC lookup to a dead path.
    if not os.path.exists(os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")):
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = path

    # A project ID that disagrees with the key points the client at a project the key cannot
    # read, which fails exactly like a missing key. The key is the authoritative one.
    key_project = service_account["project_id"]
    if "GCP_PROJECT_ID" not in config.model_fields_set:
        config.GCP_PROJECT_ID = key_project
    elif config.GCP_PROJECT_ID != key_project:
        logger.warning(
            "GCP_PROJECT_ID is '%s' but the service account key belongs to '%s'. "
            "Firestore reads will be empty unless this is intentional.",
            config.GCP_PROJECT_ID, key_project,
        )

    logger.info(
        "Service account loaded from FIREBASE_CREDENTIALS_JSON (%s, project %s).",
        service_account["client_email"], key_project,
    )


# Single shared settings instance across the application
settings = Settings()
_materialize_service_account(settings)
