"""
Outbound email, through Azure Communication Services Email or any SMTP relay.

ACS is the default: it is the same Communication resource the live classes run on, once an
email domain is connected to it, and it needs no mailbox or app password. ACS_EMAIL_SENDER is
the MailFrom address of that domain. SMTP remains for a school that already has a relay.

Delivery is best-effort by design: `send_email` returns False instead of raising, so a mail
outage never rolls back an account that was successfully provisioned. Callers surface the
boolean to the admin and fall back to reading the credentials off the screen.
"""

import html
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
    name = settings.SMTP_FROM_NAME or settings.resolved_school_name
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


def _send_acs(to: str, subject: str, body_text: str, body_html: str | None,
              reply_to: str | None = None) -> bool:
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
    message = {
        "senderAddress": from_address,
        "recipients": {"to": [{"address": to}]},
        "content": content,
    }
    if reply_to:
        # The sender is a no-reply mailbox; a parent who presses Reply should reach the office.
        message["replyTo"] = [{"address": reply_to}]
    poller = _acs_client.begin_send(message)
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


def _send_smtp(to: str, subject: str, body_text: str, body_html: str | None,
               reply_to: str | None = None) -> bool:
    from_name, from_address = _sender()

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = formataddr((from_name, from_address))
    message["To"] = to
    if reply_to:
        message["Reply-To"] = reply_to
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


def send_email(to: str, subject: str, body_text: str, body_html: str | None = None,
               reply_to: str | None = None) -> bool:
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
        sent = (_send_acs if transport == PROVIDER_ACS else _send_smtp)(
            to, subject, body_text, body_html, reply_to=reply_to
        )
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


# ---------------------------------------------------------------------------------------
# Family-facing letters
#
# Every message a parent receives is built by `_letter`, so they all read as one sender: the
# school's name and logo at the top, one clear next step as a button, the facts in a small
# table, and a footer that says who to contact - because the From address is a no-reply
# mailbox, and a parent who replies to it is talking to nobody.
#
# Everything interpolated into the HTML is escaped. The names in these emails were typed
# into a public form by whoever filled it in, and a letter is no place for their markup.
# ---------------------------------------------------------------------------------------

_BRAND = "#322E4F"      # the website's purple
_BRAND_SOFT = "#F5F2FB"  # its lilac background
_ACCENT = "#14261e"
_MINT = "#33fd9e"
_LINE = "#EAE5F4"
_MUTED = "#6b6880"


def school_profile() -> dict:
    """The school's name and contacts, from School Settings, never failing a send."""
    try:
        from app.services.tuition.settings_store import school_profile as stored_profile

        return stored_profile()
    except Exception as exc:  # the database being down must not stop an email
        logger.warning("School profile unreadable (%s); using environment values.", exc)
        return {
            "school_name": settings.resolved_school_name,
            "school_contact_email": settings.SCHOOL_CONTACT_EMAIL or settings.ADMISSION_REQUEST_NOTIFY_EMAIL,
            "school_contact_phone": settings.SCHOOL_CONTACT_PHONE,
            "school_address": settings.SCHOOL_ADDRESS,
            "school_website_url": settings.SCHOOL_WEBSITE_URL,
            "school_logo_url": settings.SCHOOL_LOGO_URL,
            "web_app_url": settings.resolved_app_url,
            "mobile_app_url": settings.MOBILE_APP_URL,
            "admission_portal_url": settings.ADMISSION_PORTAL_URL,
        }


def class_label(name: str | None) -> str | None:
    """
    How a class reads in a sentence. A school that names its classes "8" means Class 8, and
    "no seat available in 8" reads like a typo to a parent.
    """
    if not name:
        return name
    text = str(name).strip()
    if text.isdigit() or text.upper() in {
        "I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X", "XI", "XII",
    }:
        return f"Class {text}"
    return text


def _e(value) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _multiline(value) -> str:
    """Escaped text with its line breaks kept - for notes an administrator typed."""
    return _e(value).replace("\r\n", "\n").replace("\n", "<br>")


def _button(label: str, url: str, primary: bool = True) -> str:
    background = _BRAND if primary else "#ffffff"
    color = "#ffffff" if primary else _BRAND
    border = _BRAND
    return (
        f'<a href="{_e(url)}" target="_blank" rel="noopener" style="display:inline-block;'
        f"background:{background};color:{color};border:1px solid {border};padding:12px 22px;"
        f"border-radius:999px;font-weight:600;font-size:14px;text-decoration:none;"
        f'margin:4px 8px 4px 0;">{_e(label)}</a>'
    )


def _letter(
    *,
    subject: str,
    greeting: str,
    paragraphs: list[str],
    rows: list[tuple[str, str]] | None = None,
    buttons: list[tuple[str, str]] | None = None,
    note: str | None = None,
    note_title: str = "A note from the admissions office",
    blocks: list[tuple[str, list[tuple[str, str]]]] | None = None,
    bullets: list[str] | None = None,
    closing: str | None = None,
    profile: dict | None = None,
) -> tuple[str, str]:
    """
    One letter, as (html, plain text). `paragraphs`, `bullets` and table values are plain
    text and escaped here; nothing passed in is trusted as markup.

    `blocks` are titled tables - a login card per account in the welcome email.
    """
    profile = profile or school_profile()
    school = profile["school_name"]
    rows = [(label, value) for label, value in (rows or []) if value not in (None, "")]
    buttons = [(label, url) for label, url in (buttons or []) if url]

    # ---- plain text
    text_parts = [greeting, ""]
    for paragraph in paragraphs:
        text_parts += [paragraph, ""]
    if bullets:
        text_parts += [f"  - {item}" for item in bullets] + [""]
    if rows:
        width = max(len(label) for label, _ in rows)
        text_parts += [f"  {label.ljust(width)} : {value}" for label, value in rows] + [""]
    for title, block_rows in blocks or []:
        text_parts.append(title)
        text_parts += [f"  {label}: {value}" for label, value in block_rows if value] + [""]
    if note:
        text_parts += [f"{note_title}:", note, ""]
    for label, url in buttons:
        text_parts += [f"{label}: {url}"]
    if buttons:
        text_parts.append("")
    text_parts += [closing or "Warm regards,", f"Admissions Office, {school}", ""]
    contact_bits = [profile.get("school_contact_phone"), profile.get("school_contact_email"),
                    profile.get("school_website_url")]
    contact_line = " | ".join(bit for bit in contact_bits if bit)
    if contact_line:
        text_parts.append(contact_line)
    text_parts.append(
        "This email was sent from an address that does not receive replies."
        + (f" To reach us, write to {profile['school_contact_email']}." if profile.get("school_contact_email") else "")
    )
    body_text = "\n".join(text_parts)

    # ---- HTML
    logo = profile.get("school_logo_url")
    header = (
        f'<img src="{_e(logo)}" alt="{_e(school)}" height="40" style="height:40px;max-width:180px;'
        f'border:0;display:block;margin:0 auto 8px;">' if logo else ""
    ) + f'<div style="font-size:18px;font-weight:700;color:#ffffff;letter-spacing:.2px;">{_e(school)}</div>'

    body = [f'<p style="margin:0 0 16px;font-size:15px;color:#2A2642;">{_e(greeting)}</p>']
    for paragraph in paragraphs:
        body.append(f'<p style="margin:0 0 14px;font-size:15px;line-height:1.6;color:#2A2642;">{_e(paragraph)}</p>')
    if bullets:
        items = "".join(f'<li style="margin:0 0 6px;">{_e(item)}</li>' for item in bullets)
        body.append(f'<ul style="margin:0 0 16px;padding-left:20px;font-size:15px;line-height:1.5;color:#2A2642;">{items}</ul>')
    if rows:
        cells = "".join(
            f'<tr><td style="padding:9px 14px;color:{_MUTED};font-size:13px;border-bottom:1px solid {_LINE};'
            f'white-space:nowrap;vertical-align:top;">{_e(label)}</td>'
            f'<td style="padding:9px 14px;font-size:14px;font-weight:600;color:#2A2642;border-bottom:1px solid {_LINE};">'
            f"{_e(value)}</td></tr>"
            for label, value in rows
        )
        body.append(
            f'<table role="presentation" cellpadding="0" cellspacing="0" width="100%" style="border-collapse:collapse;'
            f'background:{_BRAND_SOFT};border-radius:12px;margin:4px 0 18px;">{cells}</table>'
        )
    for title, block_rows in blocks or []:
        cells = "".join(
            f'<tr><td style="padding:6px 14px;color:{_MUTED};font-size:13px;width:110px;">{_e(label)}</td>'
            f'<td style="padding:6px 14px;font-size:14px;font-weight:600;color:#2A2642;font-family:Consolas,Menlo,monospace;">'
            f"{_e(value)}</td></tr>"
            for label, value in block_rows if value
        )
        body.append(
            f'<div style="border:1px solid {_LINE};border-radius:12px;margin:0 0 12px;padding:10px 0;">'
            f'<div style="padding:0 14px 6px;font-size:13px;font-weight:700;color:{_BRAND};">{_e(title)}</div>'
            f'<table role="presentation" cellpadding="0" cellspacing="0" width="100%">{cells}</table></div>'
        )
    if note:
        body.append(
            f'<div style="border-left:3px solid {_BRAND};background:{_BRAND_SOFT};padding:12px 16px;'
            f'border-radius:0 10px 10px 0;margin:0 0 18px;">'
            f'<div style="font-size:12px;font-weight:700;color:{_MUTED};text-transform:uppercase;letter-spacing:.6px;'
            f'margin-bottom:4px;">{_e(note_title)}</div>'
            f'<div style="font-size:14px;line-height:1.55;color:#2A2642;">{_multiline(note)}</div></div>'
        )
    if buttons:
        body.append(
            '<div style="margin:6px 0 18px;">'
            + "".join(_button(label, url, primary=index == 0) for index, (label, url) in enumerate(buttons))
            + "</div>"
        )
    body.append(
        f'<p style="margin:18px 0 0;font-size:15px;line-height:1.6;color:#2A2642;">{_multiline(closing or "Warm regards,")}<br>'
        f"<strong>Admissions Office</strong><br>{_e(school)}</p>"
    )

    footer_lines = []
    if profile.get("school_address"):
        footer_lines.append(_e(profile["school_address"]))
    contact_html = " &middot; ".join(
        bit for bit in [
            _e(profile["school_contact_phone"]) if profile.get("school_contact_phone") else "",
            f'<a href="mailto:{_e(profile["school_contact_email"])}" style="color:{_MUTED};">'
            f'{_e(profile["school_contact_email"])}</a>' if profile.get("school_contact_email") else "",
            f'<a href="{_e(profile["school_website_url"])}" style="color:{_MUTED};">'
            f'{_e(profile["school_website_url"].replace("https://", "").replace("http://", "").rstrip("/"))}</a>'
            if profile.get("school_website_url") else "",
        ] if bit
    )
    if contact_html:
        footer_lines.append(contact_html)
    footer_lines.append(
        "This email was sent from an address that does not receive replies."
        + (f' To reach us, write to <a href="mailto:{_e(profile["school_contact_email"])}" style="color:{_MUTED};">'
           f'{_e(profile["school_contact_email"])}</a>.' if profile.get("school_contact_email") else "")
    )

    body_html = (
        f'<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
        f"<title>{_e(subject)}</title></head>"
        f'<body style="margin:0;padding:0;background:{_BRAND_SOFT};">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{_BRAND_SOFT};">'
        f'<tr><td align="center" style="padding:24px 12px;">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:600px;'
        f'font-family:Segoe UI,Helvetica,Arial,sans-serif;">'
        f'<tr><td align="center" style="background:{_BRAND};padding:22px 24px;border-radius:16px 16px 0 0;">{header}</td></tr>'
        f'<tr><td style="background:#ffffff;padding:28px 28px 26px;border-radius:0 0 16px 16px;">{"".join(body)}</td></tr>'
        f'<tr><td align="center" style="padding:16px 18px;font-size:12px;line-height:1.6;color:{_MUTED};">'
        f'{"<br>".join(footer_lines)}</td></tr>'
        f"</table></td></tr></table></body></html>"
    )
    return body_html, body_text


def _send_letter(to: str, subject: str, profile: dict | None = None, **letter) -> bool:
    profile = profile or school_profile()
    body_html, body_text = _letter(subject=subject, profile=profile, **letter)
    return send_email(
        to=to, subject=subject, body_text=body_text, body_html=body_html,
        reply_to=profile.get("school_contact_email") or None,
    )


def _greeting(name: str | None) -> str:
    return f"Dear {name}," if name else "Dear Parent,"


def _application_rows(reference: str, student_name: str, class_name: str | None,
                      year_name: str | None) -> list[tuple[str, str]]:
    return [
        ("Application reference", reference),
        ("Student", student_name),
        ("Class applied for", class_label(class_name) or ""),
        ("Academic year", year_name or ""),
    ]


def money_label(amount, currency: str | None) -> str:
    """'INR 25,000.00' - the code, not a symbol a mail client may not render."""
    try:
        value = f"{float(amount):,.2f}"
    except (TypeError, ValueError):
        value = str(amount)
    return f"{(currency or '').upper()} {value}".strip()


# ---- 1. The application arrived ------------------------------------------------------

def send_admission_request_received_email(
    to: str,
    contact_name: str,
    student_name: str,
    reference: str,
    class_name: str | None,
    year_name: str | None,
    portal_url: str | None = None,
) -> bool:
    """
    Acknowledges an admission request the moment it is filed.

    A family that hears nothing after pressing Submit assumes the form failed and fills it
    in again, which is where duplicate applications come from. The reference number is what
    they quote when they ring the office.
    """
    profile = school_profile()
    school = profile["school_name"]
    return _send_letter(
        to, f"We have received {student_name}'s application ({reference})", profile,
        greeting=_greeting(contact_name),
        paragraphs=[
            f"Thank you for choosing {school}. We have received the admission application for "
            f"{student_name}, and our admissions team will review it shortly.",
            "If the application is shortlisted, we will write to you with the next steps, "
            "which include uploading a few documents and paying the admission fee. There is "
            "nothing more you need to do for now.",
            "Please keep the application reference below; it helps us find your file quickly "
            "whenever you contact us.",
        ],
        rows=_application_rows(reference, student_name, class_name, year_name),
        buttons=[("Track your application", portal_url)] if portal_url else None,
    )


def send_admission_request_alert_email(to: str, request: dict, admin_url: str | None = None,
                                       event: str = "A new admission application has arrived.") -> bool:
    """Tells the office inbox something needs doing. Short: the detail is on the admin page."""
    rows = [
        ("Reference", request.get("reference") or ""),
        ("Student", request.get("student_full_name") or ""),
        ("Class applied for", class_label(request.get("class_name")) or ""),
        ("Academic year", request.get("academic_year_name") or ""),
        ("Parent", request.get("contact_name") or ""),
        ("Phone", request.get("contact_phone") or ""),
        ("Email", request.get("contact_email") or ""),
    ]
    return _send_letter(
        to, f"{event.rstrip('.')}: {request.get('student_full_name') or ''} ({request.get('reference') or ''})",
        greeting="Hello,",
        paragraphs=[event, "Open Admission Requests in the admin panel to take the next step."],
        rows=rows,
        buttons=[("Open in the admin panel", admin_url)] if admin_url else None,
        closing="Thank you,",
    )


# ---- 2. Decisions that end or pause the application ----------------------------------

def send_admission_decision_email(
    to: str,
    contact_name: str,
    student_name: str,
    reference: str,
    status: str,
    note: str | None = None,
    class_name: str | None = None,
    year_name: str | None = None,
    portal_url: str | None = None,
) -> bool:
    """
    Tells the family the application is waitlisted or cannot go ahead. The administrator's
    note goes in as written, because it is the part that actually answers the family.
    """
    profile = school_profile()
    school = profile["school_name"]
    where = f" in {class_label(class_name)}" if class_name else ""
    if status == "WAITLISTED":
        subject = f"Update on {student_name}'s application: waiting list ({reference})"
        paragraphs = [
            f"Thank you for your interest in {school}. We have carefully reviewed {student_name}'s "
            f"application. At present all seats{where} are filled, so we have placed the "
            f"application on our waiting list.",
            "Your place on the list is kept, and we will contact you as soon as a seat becomes "
            "available. You do not need to apply again.",
        ]
    else:
        subject = f"Update on {student_name}'s application ({reference})"
        paragraphs = [
            f"Thank you for your interest in {school} and for the time you took to apply. After "
            f"careful consideration, we regret that we are unable to offer {student_name} a "
            f"place{where} at this time.",
            "We truly appreciate your interest and wish {name} every success.".format(name=student_name),
        ]
    return _send_letter(
        to, subject, profile,
        greeting=_greeting(contact_name),
        paragraphs=paragraphs,
        rows=_application_rows(reference, student_name, class_name, year_name),
        note=note,
        buttons=[("View your application", portal_url)] if portal_url else None,
    )


# ---- 3. Documents --------------------------------------------------------------------

def send_documents_requested_email(
    to: str, contact_name: str, student_name: str, reference: str, class_name: str | None,
    year_name: str | None, documents: list[str], portal_url: str, note: str | None = None,
    deadline: str | None = None,
) -> bool:
    """The application is shortlisted: here is what to upload, and where."""
    profile = school_profile()
    school = profile["school_name"]
    paragraphs = [
        f"We are pleased to let you know that {student_name}'s application to {school} has been "
        f"shortlisted for admission.",
        "To continue, please upload clear scans or photos of the following documents using the "
        "secure link below:",
    ]
    after = ["PDF, JPG and PNG files are accepted. Once everything is uploaded, press "
             "\"Submit for verification\" on the same page and our team will check the documents."]
    if deadline:
        after.insert(0, f"Please upload the documents by {deadline}.")
    return _send_letter(
        to, f"Next step for {student_name}'s admission: upload documents ({reference})", profile,
        greeting=_greeting(contact_name),
        paragraphs=paragraphs,
        bullets=documents,
        rows=_application_rows(reference, student_name, class_name, year_name),
        note=note,
        buttons=[("Upload documents", portal_url)],
        closing=" ".join(after) + "\n\nWarm regards,",
    )


def send_documents_received_email(to: str, contact_name: str, student_name: str,
                                  reference: str, portal_url: str | None = None) -> bool:
    """The family pressed Submit on their documents: tell them it worked."""
    return _send_letter(
        to, f"Documents received for {student_name} ({reference})",
        greeting=_greeting(contact_name),
        paragraphs=[
            f"Thank you. We have received the documents for {student_name}'s application.",
            "Our admissions team will verify them and get back to you, usually within two "
            "working days.",
        ],
        rows=[("Application reference", reference), ("Student", student_name)],
        buttons=[("View your application", portal_url)] if portal_url else None,
    )


def send_documents_changes_email(to: str, contact_name: str, student_name: str,
                                 reference: str, problems: list[str], portal_url: str,
                                 note: str | None = None) -> bool:
    """Some documents need to be uploaded again; say which and why."""
    return _send_letter(
        to, f"Action needed: please re-upload documents for {student_name} ({reference})",
        greeting=_greeting(contact_name),
        paragraphs=[
            f"Thank you for uploading the documents for {student_name}'s application. While "
            f"verifying them, our team found that the following need your attention:",
        ],
        bullets=problems,
        note=note,
        buttons=[("Upload documents", portal_url)],
        closing="Once you have uploaded the corrected documents, press \"Submit for "
                "verification\" again.\n\nWarm regards,",
    )


def send_documents_verified_email(to: str, contact_name: str, student_name: str,
                                  reference: str, portal_url: str | None = None,
                                  note: str | None = None, payment_follows: bool = True) -> bool:
    """Every document checked out."""
    paragraphs = [
        f"We are happy to confirm that all the documents submitted for {student_name} have "
        f"been verified successfully.",
    ]
    if payment_follows:
        paragraphs.append(
            "The next step is the admission fee. We will send you the payment details shortly; "
            "once the payment is confirmed, we will complete the admission and send you the "
            "login details."
        )
    return _send_letter(
        to, f"Documents verified for {student_name} ({reference})",
        greeting=_greeting(contact_name),
        paragraphs=paragraphs,
        rows=[("Application reference", reference), ("Student", student_name),
              ("Documents", "Verified")],
        note=note,
        buttons=[("View your application", portal_url)] if portal_url else None,
    )


# ---- 4. Payment ----------------------------------------------------------------------

def send_payment_requested_email(
    to: str, contact_name: str, student_name: str, reference: str, amount, currency: str,
    description: str, portal_url: str, pay_url: str | None = None, due_date: str | None = None,
    instructions: str | None = None, note: str | None = None,
) -> bool:
    """The fee, how to pay it, and how to tell us if the payment page misbehaves."""
    paragraphs = [
        f"To confirm {student_name}'s admission, please pay the {description.lower()} using the "
        f"details below.",
    ]
    if pay_url:
        paragraphs.append(
            "Use the \"Pay now\" button for a secure online payment. If you have paid by another "
            "method, or the payment went through but the page did not confirm it, please share "
            "the transaction ID or a screenshot of the payment using \"Submit payment details\"."
        )
    else:
        paragraphs.append(
            "After paying, please share the transaction ID or a screenshot of the payment using "
            "\"Submit payment details\", so our accounts team can confirm it."
        )
    rows = [
        ("Application reference", reference),
        ("Student", student_name),
        ("Payment for", description),
        ("Amount", money_label(amount, currency)),
        ("Pay by", due_date or ""),
    ]
    buttons = [("Pay now", pay_url), ("Submit payment details", portal_url)] if pay_url \
        else [("Submit payment details", portal_url)]
    return _send_letter(
        to, f"Admission fee for {student_name}: {money_label(amount, currency)} ({reference})",
        greeting=_greeting(contact_name),
        paragraphs=paragraphs,
        rows=rows,
        note="\n\n".join(part for part in [instructions, note] if part) or None,
        note_title="How to pay" if instructions else "A note from the admissions office",
        buttons=buttons,
    )


def send_payment_received_email(to: str, contact_name: str, student_name: str, reference: str,
                                amount, currency: str, transaction: str | None = None,
                                confirmed: bool = True, portal_url: str | None = None) -> bool:
    """
    `confirmed` False: the family sent proof and the office has yet to check it.
    True: the money is confirmed, by the gateway or by the office.
    """
    if confirmed:
        subject = f"Payment confirmed for {student_name} ({reference})"
        paragraphs = [
            f"Thank you. We have received and confirmed the payment of "
            f"{money_label(amount, currency)} for {student_name}'s admission.",
            "We are now completing the admission. You will receive a separate email with the "
            "admission details and the login details for our learning app.",
        ]
    else:
        subject = f"Payment details received for {student_name} ({reference})"
        paragraphs = [
            f"Thank you. We have received your payment details for {student_name}'s admission.",
            "Our accounts team will verify the payment and confirm it by email, usually within "
            "one working day.",
        ]
    return _send_letter(
        to, subject,
        greeting=_greeting(contact_name),
        paragraphs=paragraphs,
        rows=[("Application reference", reference), ("Student", student_name),
              ("Amount", money_label(amount, currency)), ("Transaction", transaction or "")],
        buttons=[("View your application", portal_url)] if portal_url else None,
    )


def send_payment_issue_email(to: str, contact_name: str, student_name: str, reference: str,
                             portal_url: str, note: str | None = None) -> bool:
    """The proof did not check out. Kind wording: it is usually a typo in a transaction id."""
    return _send_letter(
        to, f"We could not confirm the payment for {student_name} ({reference})",
        greeting=_greeting(contact_name),
        paragraphs=[
            f"Thank you for sending the payment details for {student_name}'s admission. "
            f"Unfortunately, our accounts team could not match them to a payment received.",
            "Please check the details and submit them again, or contact the office and we will "
            "be glad to help.",
        ],
        note=note,
        buttons=[("Submit payment details", portal_url)],
    )


# ---- 5. Admitted ---------------------------------------------------------------------

def send_admission_welcome_email(
    to: str,
    contact_name: str,
    student_name: str,
    reference: str,
    details: list[tuple[str, str]],
    logins: list[dict],
    note: str | None = None,
) -> bool:
    """
    The admission is complete: the confirmation, the admission details, the login details
    for every account issued, and where the apps are. One email rather than three, so the
    family has everything they need in one place.

    `logins` items: {"title", "email", "password"}. Passwords travel in plaintext, which is
    what the admin workflow calls for; the email asks for them to be changed.
    """
    profile = school_profile()
    school = profile["school_name"]
    paragraphs = [
        f"Congratulations! We are delighted to confirm that {student_name} has been admitted to "
        f"{school}. Welcome to our school family.",
    ]
    if logins:
        paragraphs.append(
            "Your accounts on our learning app are ready. Use the login details below to sign in "
            "on the web or on the mobile app, where you can follow classes, homework, results, "
            "notices and fees."
        )
    else:
        paragraphs.append("We will send the login details for our learning app separately.")
    blocks = [
        (login["title"], [("Login email", login.get("email") or ""), ("Password", login.get("password") or "")])
        for login in logins
    ]
    closing = (
        "For your security, please change the password after signing in for the first time, "
        "and do not share it with anyone.\n\nWe look forward to a wonderful year together.\n\nWarm regards,"
        if logins else "We look forward to a wonderful year together.\n\nWarm regards,"
    )
    return _send_letter(
        to, f"Welcome to {school}: admission confirmed for {student_name}", profile,
        greeting=_greeting(contact_name),
        paragraphs=paragraphs,
        rows=[("Application reference", reference), *details],
        blocks=blocks,
        note=note,
        buttons=[
            ("Open the web app", profile.get("web_app_url") or settings.LMS_LOGIN_URL),
            ("Get the mobile app", profile.get("mobile_app_url")),
        ],
        closing=closing,
    )


# ---- Accounts ------------------------------------------------------------------------

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
    profile = school_profile()
    school = profile["school_name"]
    friendly_role = (role or "user").replace("_", " ").lower()
    article = "an" if friendly_role[:1] in "aeiou" else "a"
    return _send_letter(
        to, f"Your {school} login details", profile,
        greeting=f"Dear {full_name or login_email},",
        paragraphs=[
            f"{article.capitalize()} {friendly_role} account has been set up for you on the "
            f"{school} learning app. Sign in with the details below on the web or on the "
            f"mobile app.",
        ],
        blocks=[("Your login", [("Login email", login_email), ("Password", password)])],
        buttons=[
            ("Sign in", settings.LMS_LOGIN_URL or profile.get("web_app_url")),
            ("Get the mobile app", profile.get("mobile_app_url")),
        ],
        closing="For your security, please change the password after signing in for the first "
                "time, and do not share it with anyone. If you were not expecting this account, "
                "please contact the school office.\n\nWarm regards,",
    )


def send_password_reset_email(to: str, full_name: str, reset_url: str) -> bool:
    """
    Sends a "forgot password" link. The link works once, for PASSWORD_RESET_TOKEN_MINUTES.

    Wording is deliberate about the case where the person did not ask: nothing has changed
    on their account until the link is used, so ignoring the message is always safe.
    """
    profile = school_profile()
    minutes = settings.PASSWORD_RESET_TOKEN_MINUTES
    return _send_letter(
        to, f"Reset your {profile['school_name']} password", profile,
        greeting=f"Dear {full_name or to},",
        paragraphs=[
            f"We received a request to reset the password for your {profile['school_name']} "
            f"account. Use the button below to choose a new password. The link works once, "
            f"for {minutes} minutes.",
            "If you did not ask for this, you can safely ignore this email; your password has "
            "not changed.",
        ],
        buttons=[("Choose a new password", reset_url)],
    )
