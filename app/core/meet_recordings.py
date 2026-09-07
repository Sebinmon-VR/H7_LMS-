"""
Automatic Google Meet recording, via the Meet REST API v2.

A Calendar event with a Meet conference does not record itself, and the Calendar API has no
field that would make it. Recording is a property of the *meeting space* behind the link, so
switching it on is a second call against a different API:

    Calendar events.insert  ->  hangoutLink  ->  meeting code
    Meet spaces.get         ->  the space's real resource name
    Meet spaces.patch       ->  config.artifactConfig.recordingConfig.autoRecordingGeneration = ON

From then on Meet starts recording by itself the moment the first participant joins, with no
teacher having to remember to press anything. When the conference ends Meet writes the video
into the *organiser's* Drive, and `list_recordings()` finds it again so it can be filed into
the school's Shared Drive (see app/services/recordings.py).

Setup required before this works:
  1. Enable the Google Meet API on the GCP project.
  2. Authorize the service account's numeric client ID for these scopes under Workspace
     domain-wide delegation (Security -> Access and data control -> API controls):
       https://www.googleapis.com/auth/meetings.space.settings   (arm auto-recording)
       https://www.googleapis.com/auth/meetings.space.readonly   (find the recordings after)
  3. Recording must be available on the Workspace edition and switched on for the
     organiser's OU. Business Starter and personal Gmail accounts cannot record at all, and
     no amount of API configuration changes that - `check_access()` reports it plainly.

Everything here fails soft, in the same style as google_meet.py: a school whose delegation
is half-configured still gets its meetings and its links, with a `recording_error` that says
which of the three steps above is missing.
"""

import logging
import re
from datetime import datetime

from app.core.config import settings
from app.core.google_meet import (
    GoogleMeetError,
    resolve_impersonation_email,
    service_account_identity,
)

logger = logging.getLogger("meet_recordings")

# Non-sensitive scope, and the only one that can configure a space Calendar created rather
# than one this app created itself - which is exactly our case.
MEET_SETTINGS_SCOPE = "https://www.googleapis.com/auth/meetings.space.settings"
# Reading conference records and their recordings. `meetings.space.created` is deliberately
# not the first choice: it only covers spaces created *by this app*, so it cannot see a
# Calendar-created one.
MEET_READONLY_SCOPE = "https://www.googleapis.com/auth/meetings.space.readonly"
MEET_CREATED_SCOPE = "https://www.googleapis.com/auth/meetings.space.created"

# Google rejects the entire token exchange with `unauthorized_client` when any single
# requested scope is missing from the delegation grant, so each purpose asks for the
# narrowest set that can do its job and widens only on failure.
SETTINGS_SCOPE_CANDIDATES = [
    [MEET_SETTINGS_SCOPE],
    [MEET_SETTINGS_SCOPE, MEET_CREATED_SCOPE],
]
READ_SCOPE_CANDIDATES = [
    [MEET_READONLY_SCOPE],
    [MEET_READONLY_SCOPE, MEET_CREATED_SCOPE],
    [MEET_CREATED_SCOPE],
]

# Whichever candidate last authenticated, per purpose, so the probe runs once per process.
_working_scopes: dict[str, list[str]] = {}

# https://meet.google.com/abc-defg-hij, with or without query string or trailing slash.
_MEETING_CODE = re.compile(r"meet\.google\.com/([a-z0-9-]+)", re.IGNORECASE)


def _credentials_path() -> str | None:
    """Returns whichever service account file is present, mirroring initialize_firebase()."""
    import os

    for path in (settings.FIREBASE_CREDENTIALS_PATH, settings.GOOGLE_APPLICATION_CREDENTIALS):
        if path and os.path.exists(path):
            return path
    return None


def meeting_code_from_link(meeting_link: str | None) -> str | None:
    """
    Extracts `abc-defg-hij` from a Meet URL.

    The code doubles as an alias for the space in the Meet API, which is what lets us
    configure a conference Calendar created without ever having seen its space ID.
    """
    if not meeting_link:
        return None
    match = _MEETING_CODE.search(meeting_link)
    if not match:
        return None
    return match.group(1).strip("/").lower() or None


def _describe_api_error(exc: Exception | None) -> str:
    """Turns a Meet API failure into a message naming the step that has to be fixed."""
    if exc is None:
        return "No error was reported, which usually means no credentials were tried."

    status = getattr(getattr(exc, "resp", None), "status", None)
    reason = ""
    try:
        details = getattr(exc, "error_details", None)
        if details:
            first = details[0] if isinstance(details, list) and details else details
            if isinstance(first, dict):
                reason = first.get("reason") or first.get("message") or ""
    except Exception:  # pragma: no cover - diagnostics must never raise
        reason = ""

    text = str(exc)
    identity = service_account_identity()
    client_id = identity.get("client_id") or "the service account's numeric Client ID"

    if "unauthorized_client" in text:
        return (
            f"Domain-wide delegation is not authorized for Client ID {client_id} on the Meet "
            "scopes. In the Workspace admin console (Security -> Access and data control -> "
            "API controls -> Manage Domain Wide Delegation), add that exact numeric Client "
            f"ID with {MEET_SETTINGS_SCOPE} and {MEET_READONLY_SCOPE}. An entry authorized "
            "for only some of them produces this same error, so check the scopes on an "
            f"existing entry. Changes take a few minutes to propagate. ({text})"
        )

    if "invalid_grant" in text:
        return (
            "Google would not issue a token for the meeting organiser. That user must exist "
            f"in '{settings.GOOGLE_WORKSPACE_DOMAIN or 'the domain'}', and the domain must "
            "be a real Google Workspace domain - Meet recording and domain-wide delegation "
            f"both require one. ({text})"
        )

    if reason in {"accessNotConfigured", "SERVICE_DISABLED"} or "accessNotConfigured" in text:
        project = settings.GCP_PROJECT_ID
        return (
            f"The Google Meet API is not enabled on GCP project '{project}'. Enable it at "
            f"https://console.developers.google.com/apis/api/meet.googleapis.com/overview?project={project} "
            f"and retry in a minute or two. ({text})"
        )

    hint = ""
    if status == 403:
        # The commonest real-world cause by far, and invisible from the API alone: the
        # edition simply has no recording feature to switch on.
        hint = (
            " Meet refused the request for this organiser. Recording is available only on "
            "Business Standard/Plus, Enterprise, Education Plus and the Teaching & Learning "
            "Upgrade, and must be enabled for the organiser's OU in Admin console -> Apps "
            "-> Google Workspace -> Google Meet -> Meet video settings -> Recording. "
            "Confirm the scopes are authorized for domain-wide delegation as well."
        )
    elif status == 404:
        hint = (
            " The meeting space was not found. A space belongs to whoever created the "
            "Calendar event, so confirm the LMS is acting as that same organiser."
        )
    elif status == 401:
        hint = " Credentials were rejected - the delegation may not have propagated yet."
    elif status == 429:
        hint = " The Meet API quota was exhausted; the next sweep will retry."

    label = f"HTTP {status}" if status else type(exc).__name__
    return f"{label}: {reason or exc}{hint}"


def _scope_candidates(purpose: str) -> list[list[str]]:
    """Scope sets to try for `purpose`, narrowest first, or the configured override."""
    configured = (settings.MEET_RECORDING_SCOPES or "").strip()
    if configured:
        scopes = [s.strip() for s in configured.replace(",", " ").split() if s.strip()]
        if scopes:
            return [scopes]

    candidates = SETTINGS_SCOPE_CANDIDATES if purpose == "settings" else READ_SCOPE_CANDIDATES

    known_good = _working_scopes.get(purpose)
    if known_good and known_good in candidates:
        # Known-good first, but the others are kept so a grant that changes underneath a
        # running process still recovers without a restart.
        return [known_good] + [c for c in candidates if c != known_good]
    return candidates


def _build_meet_service(impersonate_email: str | None, purpose: str):
    """
    Builds a Meet API client acting as `impersonate_email` via domain-wide delegation.

    Unlike Calendar, there is no service-account-only fallback: every Meet REST API method
    requires user authentication, so a deployment without delegation cannot use this at all.
    The token is fetched eagerly so an unauthorized scope surfaces here, where the next
    candidate can be tried, rather than as a confusing failure on the first real call.
    """
    try:
        from google.oauth2 import service_account
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
    except ModuleNotFoundError as exc:
        raise GoogleMeetError(
            f"Google API client libraries not installed ({exc}). "
            "Install google-api-python-client and google-auth."
        ) from exc

    cred_path = _credentials_path()
    if not cred_path:
        raise GoogleMeetError(
            "No service account credentials file found for Meet access. Set "
            "FIREBASE_CREDENTIALS_PATH or GOOGLE_APPLICATION_CREDENTIALS to a readable file."
        )

    if not impersonate_email:
        raise GoogleMeetError(
            "The Meet REST API only accepts user authentication, so a Workspace identity to "
            "impersonate is required. Set GOOGLE_IMPERSONATION_FALLBACK, or leave "
            "GOOGLE_CALENDAR_IMPERSONATION on so meetings are owned by their teacher."
        )

    last_error: Exception | None = None

    for scopes in _scope_candidates(purpose):
        try:
            credentials = service_account.Credentials.from_service_account_file(
                cred_path, scopes=scopes
            ).with_subject(impersonate_email)
            credentials.refresh(Request())
        except Exception as exc:
            last_error = exc
            logger.info(
                "Meet scope set %s was refused for '%s'; trying the next candidate.",
                scopes, impersonate_email,
            )
            continue

        if _working_scopes.get(purpose) != scopes:
            logger.info(
                "Meet API authenticated as '%s' for %s with scopes %s.",
                impersonate_email, purpose, scopes,
            )
            _working_scopes[purpose] = scopes

        try:
            return build("meet", "v2", credentials=credentials, cache_discovery=False)
        except Exception as exc:
            raise GoogleMeetError(
                f"Could not build the Meet client: {_describe_api_error(exc)}"
            ) from exc

    _working_scopes.pop(purpose, None)
    raise GoogleMeetError(
        f"Could not authenticate with the Meet API. {_describe_api_error(last_error)}"
    )


def is_enabled() -> bool:
    """True when automatic recording is switched on and credentials exist."""
    return bool(
        settings.ENABLE_MEET_AUTO_RECORDING
        and settings.ENABLE_GOOGLE_MEET
        and _credentials_path()
    )


def configuration_problems(for_collection: bool = False) -> list[str]:
    """
    Static configuration issues, without touching the network.

    `for_collection` drops the ENABLE_MEET_AUTO_RECORDING check: a school can leave automatic
    arming off and still want recordings a teacher started by hand to be filed into Drive.
    """
    problems: list[str] = []

    if not settings.ENABLE_MEET_AUTO_RECORDING and not for_collection:
        problems.append(
            "ENABLE_MEET_AUTO_RECORDING is False, so classes are not recorded automatically."
        )

    if not settings.ENABLE_GOOGLE_MEET:
        problems.append(
            "ENABLE_GOOGLE_MEET is False, so there are no Meet conferences to record."
        )

    if not _credentials_path():
        problems.append(
            "No service account credentials file found "
            f"(looked for '{settings.FIREBASE_CREDENTIALS_PATH}' and "
            f"'{settings.GOOGLE_APPLICATION_CREDENTIALS}')."
        )

    if not settings.GOOGLE_CALENDAR_IMPERSONATION and not settings.GOOGLE_IMPERSONATION_FALLBACK:
        problems.append(
            "The Meet REST API requires an impersonated Workspace user, but impersonation "
            "is off and GOOGLE_IMPERSONATION_FALLBACK is empty. Recording cannot be armed "
            "or collected without one."
        )

    return problems


def describe_configuration() -> dict:
    """Non-secret snapshot of how automatic recording is wired up."""
    return {
        "enabled": bool(settings.ENABLE_MEET_AUTO_RECORDING),
        "transfer_mode": settings.recording_transfer_mode,
        "destination_folder": settings.RECORDING_DRIVE_FOLDER_NAME,
        "share_with_students": settings.RECORDING_SHARE_WITH_STUDENTS,
        "harvest_delay_minutes": settings.RECORDING_HARVEST_DELAY_MINUTES,
        "scan_interval_seconds": settings.RECORDING_SCAN_INTERVAL_SECONDS,
        "give_up_after_hours": settings.RECORDING_MAX_AGE_HOURS,
        "credentials_file": _credentials_path(),
        "service_account": service_account_identity(),
        "required_scopes": [MEET_SETTINGS_SCOPE, MEET_READONLY_SCOPE],
        "granted_scopes": dict(_working_scopes) or None,
        "problems": configuration_problems(),
    }


def check_access(teacher_email: str | None = None) -> dict:
    """
    Live probe: authenticate for both purposes and list one conference record.

    Listing conference records is the cheapest call that exercises the read path without
    creating or modifying anything, so this is safe to call at any time. The settings path
    is exercised only as far as the token exchange, since the only way to prove it further
    would be to reconfigure a real meeting space.
    """
    result = describe_configuration()

    if result["problems"]:
        return {**result, "ok": False, "detail": " ".join(result["problems"])}

    acting_as = None
    try:
        acting_as = resolve_impersonation_email(teacher_email) or (
            settings.GOOGLE_IMPERSONATION_FALLBACK or ""
        ).strip()
        _build_meet_service(acting_as, "settings")
    except GoogleMeetError as exc:
        return {**result, "ok": False, "detail": f"Auto-recording cannot be armed. {exc}"}

    try:
        service = _build_meet_service(acting_as, "read")
        service.conferenceRecords().list(pageSize=1).execute()
    except GoogleMeetError as exc:
        return {**result, "ok": False, "detail": f"Recordings cannot be collected. {exc}"}
    except Exception as exc:
        return {
            **result,
            "ok": False,
            "detail": f"Could not list conference records. {_describe_api_error(exc)}",
        }

    return {
        **result,
        "ok": True,
        "acting_as": acting_as,
        "granted_scopes": dict(_working_scopes),
        "detail": (
            f"The Meet API is reachable as {acting_as}; auto-recording can be armed and "
            "finished recordings can be collected. Whether Meet actually records depends "
            "on the Workspace edition and the Meet recording policy for that user."
        ),
    }


def enable_auto_recording(teacher_email: str, meeting_link: str) -> dict:
    """
    Switches auto-recording on for the space behind `meeting_link`.

    Always returns a dict:
      {"ok", "space_name", "meeting_code", "error"}

    `space_name` is the space's stable resource name (`spaces/xxxx`). It is worth persisting:
    the meeting code is an alias, while the resource name is the key that finds this exact
    conference's recordings later.
    """
    failure = {"ok": False, "space_name": None, "meeting_code": None, "error": None}

    code = meeting_code_from_link(meeting_link)
    if not code:
        return {
            **failure,
            "error": (
                f"'{meeting_link}' is not a Google Meet link, so there is no meeting space "
                "to configure. Auto-recording applies only to generated Meet conferences."
            ),
        }

    problems = configuration_problems()
    if problems:
        return {**failure, "meeting_code": code, "error": " ".join(problems)}

    try:
        acting_as = resolve_impersonation_email(teacher_email)
        service = _build_meet_service(acting_as, "settings")
    except GoogleMeetError as exc:
        return {**failure, "meeting_code": code, "error": str(exc)}

    try:
        space = service.spaces().get(name=f"spaces/{code}").execute()
    except Exception as exc:
        return {
            **failure,
            "meeting_code": code,
            "error": f"Could not read meeting space '{code}'. {_describe_api_error(exc)}",
        }

    space_name = space.get("name")
    if not space_name:
        return {
            **failure,
            "meeting_code": code,
            "error": f"Meet returned no resource name for space '{code}'.",
        }

    try:
        service.spaces().patch(
            name=space_name,
            # Naming the exact field matters: an omitted mask means "replace every field
            # present in the body", which would silently clear the moderation and access
            # settings an administrator may have configured on the space.
            updateMask="config.artifactConfig.recordingConfig.autoRecordingGeneration",
            body={
                "config": {
                    "artifactConfig": {
                        "recordingConfig": {"autoRecordingGeneration": "ON"}
                    }
                }
            },
        ).execute()
    except Exception as exc:
        return {
            **failure,
            "space_name": space_name,
            "meeting_code": code,
            "error": f"Could not turn on auto-recording. {_describe_api_error(exc)}",
        }

    logger.info("Auto-recording armed on space %s (%s) as %s.", space_name, code, acting_as)
    return {"ok": True, "space_name": space_name, "meeting_code": code, "error": None}


def _recording_record(recording: dict, conference: dict) -> dict:
    """Flattens a Meet recording plus its conference into the shape callers persist."""
    drive = recording.get("driveDestination") or {}
    return {
        "recording_name": recording.get("name"),
        "conference_record": conference.get("name"),
        "state": recording.get("state"),
        "drive_file_id": drive.get("file"),
        "export_uri": drive.get("exportUri"),
        "start_time": recording.get("startTime"),
        "end_time": recording.get("endTime"),
        "conference_start_time": conference.get("startTime"),
        "conference_end_time": conference.get("endTime"),
    }


def list_recordings(
    teacher_email: str,
    space_name: str | None = None,
    meeting_code: str | None = None,
) -> dict:
    """
    Finds every recording Meet has produced for one meeting space.

    Always returns {"ok", "recordings", "conferences", "error"}. `recordings` carries only
    the ones with a Drive file behind them: Meet publishes a recording resource while the
    video is still being processed, and one without `driveDestination.file` is nothing a
    later sweep cannot pick up once it is finished.
    """
    failure = {"ok": False, "recordings": [], "conferences": 0, "error": None}

    if not space_name and not meeting_code:
        return {**failure, "error": "Neither a space name nor a meeting code was supplied."}

    problems = configuration_problems(for_collection=True)
    if problems:
        return {**failure, "error": " ".join(problems)}

    try:
        acting_as = resolve_impersonation_email(teacher_email)
        service = _build_meet_service(acting_as, "read")
    except GoogleMeetError as exc:
        return {**failure, "error": str(exc)}

    if space_name:
        query = f'space.name = "{space_name}"'
    else:
        query = f'space.meeting_code = "{meeting_code}"'

    try:
        response = service.conferenceRecords().list(filter=query, pageSize=25).execute()
    except Exception as exc:
        return {
            **failure,
            "error": f"Could not list conferences for {query}. {_describe_api_error(exc)}",
        }

    conferences = response.get("conferenceRecords", [])
    found: list[dict] = []
    error = None

    for conference in conferences:
        try:
            listed = service.conferenceRecords().recordings().list(
                parent=conference["name"], pageSize=10
            ).execute()
        except Exception as exc:
            # One unreadable conference must not hide the recordings of the others.
            error = (
                f"Could not list recordings for {conference.get('name')}. "
                f"{_describe_api_error(exc)}"
            )
            logger.warning(error)
            continue

        for recording in listed.get("recordings", []):
            record = _recording_record(recording, conference)
            if record["drive_file_id"]:
                found.append(record)
            else:
                logger.info(
                    "Recording %s is not ready yet (state %s).",
                    record["recording_name"], record["state"],
                )

    return {
        "ok": True,
        "recordings": found,
        "conferences": len(conferences),
        "error": error,
    }


def recording_filename(title: str, when: datetime | None = None) -> str:
    """
    A stable, sortable name for the stored video: '2026-09-02 1000 Physics - Optics.mp4'.

    Meet's own name is 'Physics (2026-09-02 10:00 GMT+4)', which sorts by subject rather than
    by date and says nothing about which LMS session it belongs to.
    """
    stamp = (when or datetime.utcnow()).strftime("%Y-%m-%d %H%M")
    safe = re.sub(r'[\\/:*?"<>|]+', " ", title or "Class recording").strip()
    return f"{stamp} {safe or 'Class recording'}.mp4"
