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
