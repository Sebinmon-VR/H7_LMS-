"""
Outbound email, through Azure Communication Services Email or any SMTP relay.

ACS is the default: it is the same Communication resource the live classes run on, once an
email domain is connected to it, and it needs no mailbox or app password. ACS_EMAIL_SENDER is
the MailFrom address of that domain. SMTP remains for a school that already has a relay.

Delivery is best-effort by design: `send_email` returns False instead of raising, so a mail
outage never rolls back an account that was successfully provisioned. Callers surface the
boolean to the admin and fall back to reading the credentials off the screen.
"""

import logging
import smtplib
import ssl
import threading
from email.message import EmailMessage
from email.utils import formataddr

from app.core.config import settings

logger = logging.getLogger("mailer")

# The implicit-TLS port. Anything else is treated as STARTTLS on a plaintext connect.
_IMPLICIT_TLS_PORT = 465

PROVIDER_ACS = "ACS"
PROVIDER_SMTP = "SMTP"

_acs_client = None
_acs_lock = threading.Lock()


def _acs_connection_string() -> str:
    return (settings.ACS_EMAIL_CONNECTION_STRING or settings.ACS_CONNECTION_STRING or "").strip()


def _acs_ready() -> bool:
    return bool(_acs_connection_string() and (settings.ACS_EMAIL_SENDER or "").strip())


def _smtp_ready() -> bool:
    address = (settings.SMTP_FROM or settings.SMTP_USER or "").strip()
    return bool(settings.SMTP_HOST and settings.SMTP_USER and settings.SMTP_PASSWORD and address)


def provider() -> str | None:
    """Which transport sends mail: the one asked for, else whichever is configured."""
    wanted = (settings.EMAIL_PROVIDER or "").strip().upper()
    if wanted == PROVIDER_SMTP:
        return PROVIDER_SMTP if _smtp_ready() else None
    if wanted == PROVIDER_ACS:
        return PROVIDER_ACS if _acs_ready() else None
    if _acs_ready():
        return PROVIDER_ACS
    if _smtp_ready():
        return PROVIDER_SMTP
    return None


def _sender() -> tuple[str, str]:
    """Returns the (display name, address) the message is sent from."""
    name = settings.SMTP_FROM_NAME or settings.resolved_school_name or settings.PROJECT_NAME
    if provider() == PROVIDER_ACS:
        return name, settings.ACS_EMAIL_SENDER.strip()
    return name, (settings.SMTP_FROM or settings.SMTP_USER or "").strip()


def is_configured() -> bool:
    """True when enough settings are present to attempt a send."""
    return bool(settings.ENABLE_EMAIL_NOTIFICATIONS and provider())


def describe() -> dict:
    """Non-secret summary for the admin's integrations screen."""
    chosen = provider()
    return {
        "enabled": settings.ENABLE_EMAIL_NOTIFICATIONS,
        "configured": is_configured(),
        "provider": chosen,
        "sender": _sender()[1] if chosen else None,
        "acs_configured": _acs_ready(),
        "smtp_configured": _smtp_ready(),
        "smtp_host": settings.SMTP_HOST or None,
    }


def absolute_link(link: str | None) -> str | None:
    """
    A link that works from an inbox. Class links are stored as app paths (/call/...), which
    only mean something inside the LMS; an email needs the app's origin in front.
    """
    if not link:
        return link
    if link.lower().startswith(("http://", "https://")):
        return link
    base = settings.resolved_app_url
    return f"{base}{link if link.startswith('/') else '/' + link}" if base else None


def _send_acs(to: str, subject: str, body_text: str, body_html: str | None) -> bool:
    global _acs_client
    from azure.communication.email import EmailClient

    with _acs_lock:
        if _acs_client is None:
            _acs_client = EmailClient.from_connection_string(_acs_connection_string())
    # ACS takes the display name from the sender configured on the email domain, not from
    # the message, so only the address is sent.
    _, from_address = _sender()
    content = {"subject": subject, "plainText": body_text}
    if body_html:
        content["html"] = body_html
    poller = _acs_client.begin_send({
        "senderAddress": from_address,
        "recipients": {"to": [{"address": to}]},
        "content": content,
    })
    # ACS accepts the message at once and delivers it in the background. Wait a little for
    # the verdict, but a message still in flight counts as sent: it was accepted.
    poller.wait(timeout=settings.SMTP_TIMEOUT_SECONDS)
    if poller.done():
        result = poller.result()
        status = str((result or {}).get("status") or "").lower()
        if status and status not in ("succeeded", "running", "notstarted"):
            logger.error("ACS did not send '%s' to %s: %s", subject, to, (result or {}).get("error"))
            return False
    return True


def _send_smtp(to: str, subject: str, body_text: str, body_html: str | None) -> bool:
    from_name, from_address = _sender()

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = formataddr((from_name, from_address))
    message["To"] = to
    message.set_content(body_text)
    if body_html:
        message.add_alternative(body_html, subtype="html")

    context = ssl.create_default_context()
    if settings.SMTP_PORT == _IMPLICIT_TLS_PORT:
        with smtplib.SMTP_SSL(
            settings.SMTP_HOST, settings.SMTP_PORT,
            timeout=settings.SMTP_TIMEOUT_SECONDS, context=context,
        ) as server:
            server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            server.send_message(message)
    else:
        with smtplib.SMTP(
            settings.SMTP_HOST, settings.SMTP_PORT,
            timeout=settings.SMTP_TIMEOUT_SECONDS,
        ) as server:
            server.ehlo()
            if settings.SMTP_USE_TLS:
                server.starttls(context=context)
                server.ehlo()
            server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            server.send_message(message)
    return True


def send_email(to: str, subject: str, body_text: str, body_html: str | None = None) -> bool:
    """
    Sends one message and reports whether the mail service accepted it.

    Never raises: every failure path is logged and returns False.
    """
    if not is_configured():
        logger.warning(
            "Email not sent to '%s': no mail transport is configured "
            "(set ACS_EMAIL_SENDER with ACS_CONNECTION_STRING, or the SMTP_* settings).",
            to,
        )
        return False

    transport = provider()
    try:
        sent = (_send_acs if transport == PROVIDER_ACS else _send_smtp)(to, subject, body_text, body_html)
        if sent:
            logger.info("Sent '%s' to %s via %s", subject, to, transport)
        return sent
    except smtplib.SMTPAuthenticationError as exc:
        logger.error("SMTP authentication failed for '%s': %s", settings.SMTP_USER, exc)
        return False
    except Exception as exc:
        logger.error("Failed to send '%s' to %s via %s: %s", subject, to, transport, exc)
        return False


def send_class_reminder_email(
    to: str,
    full_name: str,
    subject_name: str,
    class_name: str,
    starts_at_label: str,
    minutes_before: int,
    teacher_name: str | None = None,
    room: str | None = None,
    meeting_link: str | None = None,
) -> bool:
    """
    Nudges a student or teacher that a timetabled period is about to start.

    `starts_at_label` is pre-formatted by the caller in the school's timezone: this module
    has no business deciding how a school writes a time, and the scheduler has already done
    the zone arithmetic.
    """
    display_name = full_name or to
    meeting_link = absolute_link(meeting_link)
    lead = (
        f"starts in {minutes_before} minutes"
        if minutes_before > 0 else "is starting now"
    )

    details = [("Subject", subject_name), ("Class", class_name), ("Starts", starts_at_label)]
    if teacher_name:
        details.append(("Teacher", teacher_name))
    if room:
        details.append(("Room", room))

    text_details = "\n".join(f"    {label}: {value}" for label, value in details)
    join_line = f"\nJoin the session: {meeting_link}\n" if meeting_link else ""

    body_text = (
        f"Hello {display_name},\n\n"
        f"Your {subject_name} class {lead}.\n\n"
        f"{text_details}\n"
        f"{join_line}\n"
        f"You are receiving this because class reminders are on for your account.\n"
    )

    rows = "".join(
        f'<tr><td style="color:#5f6368;padding:6px 12px;">{label}</td>'
        f'<td style="padding:6px 12px;"><strong>{value}</strong></td></tr>'
        for label, value in details
    )
    button = (
        f'<p style="margin:24px 0;">'
        f'<a href="{meeting_link}" style="background:#1a73e8;color:#ffffff;padding:10px 20px;'
        f'border-radius:4px;text-decoration:none;display:inline-block;">Join the class</a></p>'
        if meeting_link else ""
    )
    body_html = (
        f'<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#202124;">'
        f"<p>Hello {display_name},</p>"
        f"<p>Your <strong>{subject_name}</strong> class {lead}.</p>"
        f'<table cellpadding="0" style="border-collapse:collapse;background:#f8f9fa;'
        f'border:1px solid #dadce0;border-radius:4px;">{rows}</table>'
        f"{button}"
        f'<p style="color:#5f6368;">You are receiving this because class reminders are on '
        f"for your account.</p>"
        f"</div>"
    )

    return send_email(
        to=to,
        subject=f"Reminder: {subject_name} {lead}",
        body_text=body_text,
        body_html=body_html,
    )


def _html_shell(paragraphs: list[str], rows: list[tuple[str, str]] | None = None,
                footer: str | None = None) -> str:
    """The plain HTML frame every family-facing message uses, so they read as one sender."""
    table = ""
    if rows:
        cells = "".join(
            f'<tr><td style="color:#5f6368;padding:6px 12px;">{label}</td>'
            f'<td style="padding:6px 12px;"><strong>{value}</strong></td></tr>'
            for label, value in rows if value
        )
        table = (
            f'<table cellpadding="0" style="border-collapse:collapse;background:#f8f9fa;'
            f'border:1px solid #dadce0;border-radius:4px;margin:16px 0;">{cells}</table>'
        )
    body = "".join(f"<p>{p}</p>" for p in paragraphs)
    tail = f'<p style="color:#5f6368;">{footer}</p>' if footer else ""
    return (
        f'<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#202124;">'
        f"{body}{table}{tail}</div>"
    )


def send_admission_request_received_email(
    to: str,
    contact_name: str,
    student_name: str,
    reference: str,
    class_name: str | None,
    year_name: str | None,
) -> bool:
    """
    Acknowledges an admission request the moment it is filed.

    A family that hears nothing after pressing Submit assumes the form failed and fills it
    in again, which is where duplicate applications come from. The reference number is what
    they quote when they ring the office.
    """
    school = settings.resolved_school_name
    rows = [("Reference", reference), ("Student", student_name),
            ("Class applied for", class_name or ""), ("Session", year_name or "")]
    detail_lines = "\n".join(f"    {label}: {value}" for label, value in rows if value)

    body_text = (
        f"Dear {contact_name},\n\n"
        f"Thank you for applying to {school}. We have received your admission request for "
        f"{student_name} and the office will be in touch once it has been reviewed.\n\n"
        f"{detail_lines}\n\n"
        f"Please quote the reference above in any correspondence. There is nothing more you "
        f"need to do for now.\n"
    )
    body_html = _html_shell(
        [
            f"Dear {contact_name},",
            f"Thank you for applying to <strong>{school}</strong>. We have received your "
            f"admission request for <strong>{student_name}</strong> and the office will be in "
            f"touch once it has been reviewed.",
        ],
        rows,
        footer="Please quote the reference above in any correspondence. There is nothing more "
               "you need to do for now.",
    )
    return send_email(
        to=to,
        subject=f"{school}: admission request received ({reference})",
        body_text=body_text,
        body_html=body_html,
    )


def send_admission_request_alert_email(to: str, request: dict, admin_url: str | None = None) -> bool:
    """Tells the office inbox a new request is waiting. Short: the detail is on the admin page."""
    rows = [
        ("Reference", request.get("reference") or ""),
        ("Student", request.get("student_full_name") or ""),
        ("Class applied for", request.get("class_name") or ""),
        ("Session", request.get("academic_year_name") or ""),
        ("Contact", f"{request.get('contact_name') or ''} "
                    f"({request.get('contact_phone') or ''}, {request.get('contact_email') or ''})"),
    ]
    detail_lines = "\n".join(f"    {label}: {value}" for label, value in rows if value)
    link_line = f"\nReview it here: {admin_url}\n" if admin_url else ""

    body_text = (
        f"A new admission request has been submitted from the website.\n\n"
        f"{detail_lines}\n{link_line}\n"
        f"Open Admission Requests in the admin panel to review, admit or decline it.\n"
    )
    body_html = _html_shell(
        ["A new admission request has been submitted from the website."]
        + ([f'<a href="{admin_url}">Review it in the admin panel</a>.'] if admin_url else []),
        rows,
        footer="Open Admission Requests in the admin panel to review, admit or decline it.",
    )
    return send_email(
        to=to,
        subject=f"New admission request: {request.get('student_full_name') or ''} "
                f"({request.get('reference') or ''})",
        body_text=body_text,
        body_html=body_html,
    )


def send_admission_decision_email(
    to: str,
    contact_name: str,
    student_name: str,
    reference: str,
    status: str,
    note: str | None = None,
    class_name: str | None = None,
    year_name: str | None = None,
) -> bool:
    """
    Tells the family what the office decided.

    ADMITTED, WAITLISTED and REJECTED each get their own opening line; the optional note is
    whatever the administrator typed and goes in verbatim, because it is the part of the
    message that actually answers the family's question.
    """
    school = settings.resolved_school_name
    if status == "ADMITTED":
        subject = f"{school}: admission confirmed for {student_name}"
        opening = (
            f"We are pleased to confirm that {student_name} has been admitted to {school}"
            + (f" in {class_name}" if class_name else "")
            + (f" for the {year_name} session" if year_name else "")
            + ". The office will contact you about the next steps, including fees and the "
              "documents to bring."
        )
    elif status == "WAITLISTED":
        subject = f"{school}: admission request for {student_name} placed on the waiting list"
        opening = (
            f"Thank you for applying to {school}. There is no seat available for "
            f"{student_name}"
            + (f" in {class_name}" if class_name else "")
            + " at the moment, so the application has been placed on our waiting list. We "
              "will contact you as soon as a place opens up."
        )
    else:
        subject = f"{school}: an update on the admission request for {student_name}"
        opening = (
            f"Thank you for your interest in {school}. We are sorry to let you know that we are "
            f"unable to offer {student_name} a place"
            + (f" in {class_name}" if class_name else "")
            + " at this time."
        )

    note_text = f"\nMessage from the school:\n{note}\n" if note else ""
    body_text = (
        f"Dear {contact_name},\n\n{opening}\n{note_text}\n"
        f"    Reference: {reference}\n\n"
        f"If you have any questions, please contact the school office and quote the reference "
        f"above.\n"
    )
    paragraphs = [f"Dear {contact_name},", opening]
    if note:
        paragraphs.append(f"<em>Message from the school:</em><br>{note}")
    body_html = _html_shell(
        paragraphs,
        [("Reference", reference)],
        footer="If you have any questions, please contact the school office and quote the "
               "reference above.",
    )
    return send_email(to=to, subject=subject, body_text=body_text, body_html=body_html)


def send_credentials_email(
    to: str,
    full_name: str,
    role: str,
    login_email: str,
    password: str,
) -> bool:
    """
    Delivers a newly issued email/password pair to the account holder.

    The password travels in plaintext, which is what the admin workflow calls for; treat any
    stored copy of this message as sensitive and tell users to change it after first login.
    """
    login_url = settings.LMS_LOGIN_URL or ""
    friendly_role = (role or "user").replace("_", " ").title()
    display_name = full_name or login_email

    link_line = f"\nSign in here: {login_url}\n" if login_url else ""
    body_text = (
        f"Hello {display_name},\n\n"
        f"A {friendly_role} account has been created for you on {settings.PROJECT_NAME}.\n"
        f"Sign in with the email and password below.\n"
        f"{link_line}\n"
        f"    Email:    {login_email}\n"
        f"    Password: {password}\n\n"
        f"Please change your password after you sign in for the first time, and do not "
        f"share these details with anyone.\n\n"
        f"If you were not expecting this account, contact your administrator.\n"
    )

    button = (
        f'<p style="margin:24px 0;">'
        f'<a href="{login_url}" style="background:#1a73e8;color:#ffffff;padding:10px 20px;'
        f'border-radius:4px;text-decoration:none;display:inline-block;">Sign in</a></p>'
        if login_url else ""
    )
    body_html = (
        f'<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#202124;">'
        f"<p>Hello {display_name},</p>"
        f"<p>A <strong>{friendly_role}</strong> account has been created for you on "
        f"<strong>{settings.PROJECT_NAME}</strong>. Sign in with the email and password below.</p>"
        f'<table cellpadding="8" style="border-collapse:collapse;background:#f8f9fa;'
        f'border:1px solid #dadce0;border-radius:4px;">'
        f'<tr><td style="color:#5f6368;">Email</td>'
        f'<td><strong>{login_email}</strong></td></tr>'
        f'<tr><td style="color:#5f6368;">Password</td>'
        f'<td><strong>{password}</strong></td></tr>'
        f"</table>"
        f"{button}"
        f'<p style="color:#5f6368;">Please change your password after your first sign-in, '
        f"and do not share these details with anyone. If you were not expecting this "
        f"account, contact your administrator.</p>"
        f"</div>"
    )

    return send_email(
        to=to,
        subject=f"Your {settings.PROJECT_NAME} login details",
        body_text=body_text,
        body_html=body_html,
    )


def send_password_reset_email(to: str, full_name: str, reset_url: str) -> bool:
    """
    Sends a "forgot password" link. The link works once, for PASSWORD_RESET_TOKEN_MINUTES.

    Wording is deliberate about the case where the person did not ask: nothing has changed
    on their account until the link is used, so ignoring the message is always safe.
    """
    display_name = full_name or to
    minutes = settings.PASSWORD_RESET_TOKEN_MINUTES
    school = settings.resolved_school_name

    body_text = (
        f"Hello {display_name},\n\n"
        f"Somebody asked to reset the password of your {school} account.\n"
        f"Choose a new password here (the link works once, for {minutes} minutes):\n\n"
        f"{reset_url}\n\n"
        f"If you did not ask for this, ignore this email - your password has not changed.\n"
    )
    body_html = (
        f'<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#202124;">'
        f"<p>Hello {display_name},</p>"
        f"<p>Somebody asked to reset the password of your <strong>{school}</strong> account.</p>"
        f'<p style="margin:24px 0;">'
        f'<a href="{reset_url}" style="background:#1a73e8;color:#ffffff;padding:10px 20px;'
        f'border-radius:4px;text-decoration:none;display:inline-block;">Choose a new password</a></p>'
        f'<p style="color:#5f6368;">The link works once, for {minutes} minutes. If you did not '
        f"ask for this, ignore this email - your password has not changed.</p>"
        f"</div>"
    )

    return send_email(
        to=to,
        subject=f"Reset your {school} password",
        body_text=body_text,
        body_html=body_html,
    )
