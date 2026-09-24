"""
Outbound email over SMTP, targeting Gmail and Google Workspace.

Workspace does not accept an ordinary account password over SMTP. `SMTP_PASSWORD` must be
a 16-character App Password generated at https://myaccount.google.com/apppasswords, which
in turn requires 2-Step Verification on the sending account. Workspace admins must also
leave SMTP relay / "less secure app" restrictions permissive enough for the sending user.

Delivery is best-effort by design: `send_email` returns False instead of raising, so a mail
outage never rolls back an account that was successfully provisioned. Callers surface the
boolean to the admin and fall back to reading the credentials off the screen.
"""

import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

from app.core.config import settings

logger = logging.getLogger("mailer")

# Gmail's implicit-TLS port. Anything else is treated as STARTTLS on a plaintext connect.
_IMPLICIT_TLS_PORT = 465


def _sender() -> tuple[str, str]:
    """Returns the (display name, address) the message is sent from."""
    address = (settings.SMTP_FROM or settings.SMTP_USER or "").strip()
    return (settings.SMTP_FROM_NAME or settings.PROJECT_NAME, address)


def is_configured() -> bool:
    """True when enough SMTP settings are present to attempt a send."""
    if not settings.ENABLE_EMAIL_NOTIFICATIONS:
        return False
    _, address = _sender()
    return bool(settings.SMTP_HOST and settings.SMTP_USER and settings.SMTP_PASSWORD and address)


def send_email(to: str, subject: str, body_text: str, body_html: str | None = None) -> bool:
    """
    Sends one message and reports whether the SMTP server accepted it.

    Never raises: every failure path is logged and returns False.
    """
    if not is_configured():
        logger.warning(
            "Email not sent to '%s': SMTP is not configured "
            "(set ENABLE_EMAIL_NOTIFICATIONS, SMTP_HOST, SMTP_USER, SMTP_PASSWORD).",
            to,
        )
        return False

    from_name, from_address = _sender()

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = formataddr((from_name, from_address))
    message["To"] = to
    message.set_content(body_text)
    if body_html:
        message.add_alternative(body_html, subtype="html")

    try:
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

        logger.info("Sent '%s' to %s", subject, to)
        return True
    except smtplib.SMTPAuthenticationError as exc:
        logger.error(
            "SMTP authentication failed for '%s'. Gmail and Workspace require an App "
            "Password, not the account password: %s",
            settings.SMTP_USER, exc,
        )
        return False
    except Exception as exc:
        logger.error("Failed to send '%s' to %s: %s", subject, to, exc)
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
