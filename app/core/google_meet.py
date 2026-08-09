"""
Google Meet integration via the Google Calendar API.

Meet links cannot be minted directly; they are produced as a side effect of inserting a
Calendar event with a conferencing create-request. On a Workspace domain the service account
impersonates the teacher through domain-wide delegation, so the event is genuinely owned by
that teacher, appears on their calendar, and invited students receive real invitations.

Setup required before this works:
  1. Enable the Google Calendar API on the GCP project.
  2. Authorize the service account's numeric client ID for
     https://www.googleapis.com/auth/calendar and .../auth/calendar.events in the Workspace
     admin console under Security -> API controls -> Domain-wide delegation.
  3. Set GOOGLE_WORKSPACE_DOMAIN and GOOGLE_IMPERSONATION_FALLBACK.

Without Workspace, set GOOGLE_CALENDAR_IMPERSONATION=False and share a calendar with the
service account's email directly, then put that calendar's ID in GOOGLE_CALENDAR_ID.

Every function fails soft - a misconfigured delegation degrades the meeting to a plain
record rather than taking the endpoint down - but it now returns *why* it failed, so the
caller can surface that instead of silently persisting a meeting with no link.
"""

import logging
import uuid
from datetime import datetime, timedelta

from app.core.config import settings

logger = logging.getLogger("google_meet")

CALENDAR_EVENTS_SCOPE = "https://www.googleapis.com/auth/calendar.events"
CALENDAR_FULL_SCOPE = "https://www.googleapis.com/auth/calendar"

# Inserting an event with a Meet conference needs only calendar.events, so that is tried
# first. Google rejects the *entire* token exchange with `unauthorized_client` if any single
# requested scope is missing from the delegation grant, so asking for extra scopes "just in
# case" is actively harmful - hence a narrowest-first probe rather than a fixed wide set.
SCOPE_CANDIDATES = [
    [CALENDAR_EVENTS_SCOPE],
    [CALENDAR_FULL_SCOPE],
    [CALENDAR_FULL_SCOPE, CALENDAR_EVENTS_SCOPE],
]

# The candidate that last authenticated, so the probe runs once per process rather than on
# every request. Reset whenever the working set stops working.
_working_scopes: list[str] | None = None


class GoogleMeetError(Exception):
    """Raised internally when a Calendar operation cannot be completed."""


def _credentials_path() -> str | None:
    """Returns whichever service account file is present, mirroring initialize_firebase()."""
    import os

    for path in (settings.FIREBASE_CREDENTIALS_PATH, settings.GOOGLE_APPLICATION_CREDENTIALS):
        if path and os.path.exists(path):
            return path
    return None


def _describe_api_error(exc: Exception | None) -> str:
    """Extracts the actionable reason from a googleapiclient HttpError or auth failure."""
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

    subject = (settings.GOOGLE_IMPERSONATION_FALLBACK or "").strip() or "the teacher"
    identity = service_account_identity()
    client_id = identity.get("client_id") or "the service account's numeric Client ID"

    hint = ""
    if "unauthorized_client" in text:
        # The subject resolved fine; the delegation grant itself is missing or narrower than
        # what was asked for. Every candidate scope set has already been tried by this point.
        return (
            f"Domain-wide delegation is not authorized for Client ID {client_id}. In the "
            "Workspace admin console (admin.google.com -> Security -> Access and data "
            "control -> API controls -> Manage Domain Wide Delegation), add that exact "
            f"numeric Client ID - not the email {identity.get('client_email') or ''} - with "
            f"the scope {CALENDAR_EVENTS_SCOPE}. An existing entry authorized for a "
            "different scope also produces this error, so check the scopes on the entry if "
            f"one is already there. Changes take a few minutes to propagate. ({text})"
        )

    if "invalid_grant" in text:
        # The token exchange failed on the subject, not the scopes.
        return (
            f"Google would not issue a token for '{subject}'. That user must exist in "
            f"'{settings.GOOGLE_WORKSPACE_DOMAIN or 'the domain'}' and the domain must be a "
            "real Google Workspace domain - domain-wide delegation does not work with "
            "personal Gmail accounts. Set GOOGLE_IMPERSONATION_FALLBACK to a live Workspace "
            f"user. ({text})"
        )

    if reason in {"accessNotConfigured", "SERVICE_DISABLED"} or "accessNotConfigured" in text:
        # Distinct from a permissions problem: the API is switched off project-wide, so no
        # amount of delegation or calendar sharing will help until it is enabled.
        project = settings.GCP_PROJECT_ID
        return (
            "The Google Calendar API is not enabled on GCP project "
            f"'{project}'. Enable it at "
            f"https://console.developers.google.com/apis/api/calendar-json.googleapis.com/overview?project={project} "
            f"and retry in a minute or two. ({text})"
        )

    if reason in {"notFound", "notACalendarUser"} or status == 404:
        hint = (
            f" Calendar '{settings.GOOGLE_CALENDAR_ID}' was not found for the acting identity. "
            "Check GOOGLE_CALENDAR_ID, and that the impersonated user exists in the domain."
        )
    elif reason in {"forbiddenForServiceAccounts", "forbidden"} or status == 403:
        hint = (
            " Access was refused. Authorize the service account's client ID for the calendar "
            "scopes under Workspace domain-wide delegation, or share the target calendar "
            "with the service account and set GOOGLE_CALENDAR_IMPERSONATION=False."
        )
    elif status == 401:
        hint = " Credentials were rejected - the delegation may not have propagated yet."
    elif "time zone" in text.lower():
        hint = (
            f" Set GOOGLE_CALENDAR_TIMEZONE to a valid IANA zone "
            f"(currently '{settings.GOOGLE_CALENDAR_TIMEZONE}')."
        )

    label = f"HTTP {status}" if status else type(exc).__name__
    return f"{label}: {reason or exc}{hint}"


def service_account_identity() -> dict:
    """
    The service account's email and numeric client ID, read from the credentials file.

    The numeric client ID is the value that must be pasted into the Workspace domain-wide
    delegation form, and it appears nowhere in the console UI of this project - surfacing it
    in diagnostics turns a scavenger hunt into a copy-paste.
    """
    import json

    cred_path = _credentials_path()
    if not cred_path:
        return {}

    try:
        with open(cred_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception as exc:
        logger.warning("Could not read service account file '%s': %s", cred_path, exc)
        return {}

    return {
        "client_email": data.get("client_email"),
        "client_id": data.get("client_id"),
        "project_id": data.get("project_id"),
    }


def _scope_candidates() -> list[list[str]]:
    """Scope sets to try, narrowest first, or the single explicitly configured set."""
    configured = (settings.GOOGLE_CALENDAR_SCOPES or "").strip()
    if configured:
        scopes = [s.strip() for s in configured.replace(",", " ").split() if s.strip()]
        if scopes:
            return [scopes]
    return SCOPE_CANDIDATES


def _build_calendar_service(impersonate_email: str | None):
    """
    Builds a Calendar API client, acting as `impersonate_email` via domain-wide delegation
    when one is given and as the service account itself otherwise.

    When impersonating, the token is fetched eagerly and the scope candidates are tried in
    turn. Credentials are otherwise lazy, so an unauthorized-scope grant would surface much
    later as a confusing failure on the first real API call instead of here, where it can be
    retried against a narrower set.
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
            "No service account credentials file found for Calendar access. Set "
            "FIREBASE_CREDENTIALS_PATH or GOOGLE_APPLICATION_CREDENTIALS to a readable file."
        )

    global _working_scopes

    candidates = _scope_candidates()
    if _working_scopes and _working_scopes in candidates:
        # Put the known-good set first without dropping the others, so a delegation grant
        # that changes underneath a running process still recovers.
        candidates = [_working_scopes] + [c for c in candidates if c != _working_scopes]

    last_error: Exception | None = None

    for scopes in candidates:
        try:
            credentials = service_account.Credentials.from_service_account_file(
                cred_path, scopes=scopes
            )
            if impersonate_email:
                credentials = credentials.with_subject(impersonate_email)
                credentials.refresh(Request())
        except Exception as exc:
            last_error = exc
            logger.info(
                "Calendar scope set %s was refused for '%s'; trying the next candidate.",
                scopes, impersonate_email,
            )
            continue

        if _working_scopes != scopes:
            logger.info("Calendar authenticated as '%s' with scopes %s.", impersonate_email, scopes)
            _working_scopes = scopes

        try:
            return build("calendar", "v3", credentials=credentials, cache_discovery=False)
        except Exception as exc:
            raise GoogleMeetError(
                f"Could not build the Calendar client: {_describe_api_error(exc)}"
            ) from exc

    _working_scopes = None
    raise GoogleMeetError(
        f"Could not authenticate with the Calendar API. {_describe_api_error(last_error)}"
    )


def _resolve_impersonation_email(teacher_email: str | None) -> str | None:
    """
    Picks the Workspace identity to act as, or None to act as the service account.

    Domain-wide delegation only permits impersonating users inside the configured domain, so
    a teacher with an outside address falls back to the configured service identity.
    """
    if not settings.GOOGLE_CALENDAR_IMPERSONATION:
        return None

    domain = (settings.GOOGLE_WORKSPACE_DOMAIN or "").strip().lower()
    fallback = (settings.GOOGLE_IMPERSONATION_FALLBACK or "").strip()

    if teacher_email and (not domain or teacher_email.lower().endswith(f"@{domain}")):
        return teacher_email

    if fallback:
        logger.info(
            "Teacher email '%s' is outside domain '%s'; impersonating fallback '%s'.",
            teacher_email, domain, fallback,
        )
        return fallback

    raise GoogleMeetError(
        f"Teacher email '{teacher_email}' is outside the Workspace domain '{domain}' and "
        "GOOGLE_IMPERSONATION_FALLBACK is not configured. Set that fallback, or set "
        "GOOGLE_CALENDAR_IMPERSONATION=False to use a calendar shared with the service account."
    )


def _event_time(moment: datetime) -> dict:
    """
    Formats a datetime for the Calendar API.

    A naive dateTime with no accompanying timeZone is rejected outright ("Missing time zone
    definition for start time"), which is the single most common reason Meet link generation
    appears to do nothing. Aware datetimes carry their own offset and need no help.
    """
    payload = {"dateTime": moment.isoformat()}
    if moment.tzinfo is None:
        payload["timeZone"] = settings.GOOGLE_CALENDAR_TIMEZONE or "UTC"
    return payload


def is_enabled() -> bool:
    """True when Meet generation is switched on and credentials exist."""
    return bool(settings.ENABLE_GOOGLE_MEET and _credentials_path())


def configuration_problems() -> list[str]:
    """Static configuration issues, without touching the network."""
    problems: list[str] = []

    if not settings.ENABLE_GOOGLE_MEET:
        problems.append("ENABLE_GOOGLE_MEET is False, so no Meet links are generated.")

    if not _credentials_path():
        problems.append(
            "No service account credentials file found "
            f"(looked for '{settings.FIREBASE_CREDENTIALS_PATH}' and "
            f"'{settings.GOOGLE_APPLICATION_CREDENTIALS}')."
        )

    if settings.GOOGLE_CALENDAR_IMPERSONATION and not (
        settings.GOOGLE_WORKSPACE_DOMAIN or settings.GOOGLE_IMPERSONATION_FALLBACK
    ):
        problems.append(
            "Impersonation is on but neither GOOGLE_WORKSPACE_DOMAIN nor "
            "GOOGLE_IMPERSONATION_FALLBACK is set, so there is no identity to act as."
        )

    if not settings.GOOGLE_CALENDAR_IMPERSONATION and settings.GOOGLE_CALENDAR_ID == "primary":
        problems.append(
            "GOOGLE_CALENDAR_IMPERSONATION is off but GOOGLE_CALENDAR_ID is still 'primary'. "
            "A service account has no primary calendar; point this at a calendar shared with it."
        )

    return problems


def describe_configuration() -> dict:
    """Non-secret snapshot of how Meet is wired up, for the admin diagnostics endpoint."""
    return {
        "enabled": bool(settings.ENABLE_GOOGLE_MEET),
        "calendar_id": settings.GOOGLE_CALENDAR_ID,
        "timezone": settings.GOOGLE_CALENDAR_TIMEZONE,
        "impersonation": settings.GOOGLE_CALENDAR_IMPERSONATION,
        "workspace_domain": settings.GOOGLE_WORKSPACE_DOMAIN or None,
        "impersonation_fallback": settings.GOOGLE_IMPERSONATION_FALLBACK or None,
        "invite_attendees": settings.GOOGLE_MEET_INVITE_ATTENDEES,
        "credentials_file": _credentials_path(),
        # The numeric client_id is what the delegation form wants; surfacing it here saves
        # digging through the service account JSON.
        "service_account": service_account_identity(),
        "scope_candidates": _scope_candidates(),
        "problems": configuration_problems(),
    }


def check_access(teacher_email: str | None = None) -> dict:
    """
    Live probe: authenticate, then read one event off the target calendar.

    Reading a single event rather than the calendar's own metadata is deliberate - the
    narrow calendar.events scope can list events but cannot call calendars.get, so probing
    with the latter would report a correctly configured minimal grant as broken.

    Returns {"ok", "detail", ...}. Nothing is written, so this is safe to call at any time.
    """
    result = describe_configuration()

    if result["problems"]:
        return {**result, "ok": False, "detail": " ".join(result["problems"])}

    try:
        impersonate = _resolve_impersonation_email(teacher_email)
        service = _build_calendar_service(impersonate)
        service.events().list(
            calendarId=settings.GOOGLE_CALENDAR_ID, maxResults=1
        ).execute()
    except GoogleMeetError as exc:
        return {**result, "ok": False, "detail": str(exc)}
    except Exception as exc:
        return {
            **result,
            "ok": False,
            "detail": f"Could not read calendar '{settings.GOOGLE_CALENDAR_ID}'. {_describe_api_error(exc)}",
        }

    identity = impersonate or "the service account"
    return {
        **result,
        "ok": True,
        "acting_as": identity,
        "granted_scopes": _working_scopes,
        "detail": (
            f"Calendar '{settings.GOOGLE_CALENDAR_ID}' is reachable as {identity} "
            f"with scopes {_working_scopes}."
        ),
    }


def create_meeting(
    teacher_email: str,
    title: str,
    scheduled_time: datetime,
    duration_minutes: int = 60,
    description: str | None = None,
    attendee_emails: list[str] | None = None,
) -> dict:
    """
    Creates a Calendar event with an attached Google Meet conference.

    Always returns a dict:
      {"ok": bool, "meeting_link", "event_id", "calendar_id", "html_link", "error"}

    `ok` is False - with `error` explaining why - when Meet generation is disabled or the
    Calendar call fails. Callers persist the meeting either way, but can now show the reason
    instead of leaving the user staring at an empty link field.
    """
    failure = {
        "ok": False,
        "meeting_link": None,
        "event_id": None,
        "calendar_id": None,
        "html_link": None,
        "error": None,
    }

    problems = configuration_problems()
    if problems:
        detail = " ".join(problems)
        logger.info("Google Meet not created: %s", detail)
        return {**failure, "error": detail}

    try:
        impersonate = _resolve_impersonation_email(teacher_email)
        service = _build_calendar_service(impersonate)
    except GoogleMeetError as exc:
        logger.warning("Google Meet not created: %s", exc)
        return {**failure, "error": str(exc)}

    end_time = scheduled_time + timedelta(minutes=max(duration_minutes, 1))
    event_body = {
        "summary": title,
        "description": description or f"LMS scheduled session: {title}",
        "start": _event_time(scheduled_time),
        "end": _event_time(end_time),
        "conferenceData": {
            "createRequest": {
                "requestId": str(uuid.uuid4()),
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        },
    }

    attendees = []
    if settings.GOOGLE_MEET_INVITE_ATTENDEES and attendee_emails:
        attendees = [{"email": email} for email in attendee_emails if email]

    def insert(with_attendees: bool):
        body = dict(event_body)
        if with_attendees and attendees:
            body["attendees"] = attendees
        return service.events().insert(
            calendarId=settings.GOOGLE_CALENDAR_ID,
            body=body,
            conferenceDataVersion=1,
            sendUpdates="all" if (with_attendees and attendees) else "none",
        ).execute()

    warning = None
    try:
        event = insert(with_attendees=True)
    except Exception as exc:
        # Inviting guests can be blocked independently of creating the event (external
        # sharing policy, or a non-Workspace project). Losing the invitations is far better
        # than losing the Meet link, so retry once without them.
        if attendees:
            warning = (
                f"Students were not invited as Calendar guests. {_describe_api_error(exc)}"
            )
            logger.warning("Retrying Meet creation without attendees: %s", warning)
            try:
                event = insert(with_attendees=False)
            except Exception as retry_exc:
                detail = f"Could not create the Calendar event. {_describe_api_error(retry_exc)}"
                logger.error("Google Meet creation failed: %s", detail)
                return {**failure, "error": detail}
        else:
            detail = f"Could not create the Calendar event. {_describe_api_error(exc)}"
            logger.error("Google Meet creation failed: %s", detail)
            return {**failure, "error": detail}

    meeting_link = event.get("hangoutLink")
    if not meeting_link:
        # Conference creation is asynchronous in rare cases; fall back to the entry point.
        entry_points = event.get("conferenceData", {}).get("entryPoints", [])
        video = next((e for e in entry_points if e.get("entryPointType") == "video"), None)
        meeting_link = video.get("uri") if video else None

    if not meeting_link:
        status = (event.get("conferenceData", {}).get("createRequest", {})
                  .get("status", {}).get("statusCode"))
        warning = (
            f"The Calendar event was created but no Meet link came back (conference status: "
            f"{status or 'unknown'}). Google Meet may not be enabled for this account."
        )
        logger.warning(warning)

    return {
        "ok": bool(meeting_link),
        "meeting_link": meeting_link,
        "event_id": event.get("id"),
        "calendar_id": settings.GOOGLE_CALENDAR_ID,
        "html_link": event.get("htmlLink"),
        "error": warning,
    }


def update_meeting(
    teacher_email: str,
    event_id: str,
    calendar_id: str | None = None,
    title: str | None = None,
    scheduled_time: datetime | None = None,
    duration_minutes: int | None = None,
) -> bool:
    """Patches an existing Calendar event so edits propagate to attendees' calendars."""
    if not is_enabled() or not event_id:
        return False

    try:
        impersonate = _resolve_impersonation_email(teacher_email)
        service = _build_calendar_service(impersonate)
        target_calendar = calendar_id or settings.GOOGLE_CALENDAR_ID

        patch: dict = {}
        if title is not None:
            patch["summary"] = title
        if scheduled_time is not None:
            patch["start"] = _event_time(scheduled_time)
            end_time = scheduled_time + timedelta(minutes=max(duration_minutes or 60, 1))
            patch["end"] = _event_time(end_time)

        if not patch:
            return True

        service.events().patch(
            calendarId=target_calendar,
            eventId=event_id,
            body=patch,
            sendUpdates="all",
        ).execute()
        return True

    except GoogleMeetError as exc:
        logger.warning("Google Meet event not updated: %s", exc)
        return False
    except Exception as exc:
        logger.error(
            "Could not update Google Meet event '%s': %s", event_id, _describe_api_error(exc)
        )
        return False


def delete_meeting(teacher_email: str, event_id: str, calendar_id: str | None = None) -> bool:
    """Cancels the Calendar event, notifying attendees that the session is off."""
    if not is_enabled() or not event_id:
        return False

    try:
        impersonate = _resolve_impersonation_email(teacher_email)
        service = _build_calendar_service(impersonate)

        service.events().delete(
            calendarId=calendar_id or settings.GOOGLE_CALENDAR_ID,
            eventId=event_id,
            sendUpdates="all",
        ).execute()
        return True

    except GoogleMeetError as exc:
        logger.warning("Google Meet event not deleted: %s", exc)
        return False
    except Exception as exc:
        # A 410/404 means it is already gone, which satisfies the caller's intent.
        logger.warning(
            "Could not delete Google Meet event '%s': %s", event_id, _describe_api_error(exc)
        )
        return False
