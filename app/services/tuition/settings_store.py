"""
Runtime settings an administrator edits without a redeploy.

Environment variables are the right place for credentials and for facts about the *machine*.
They are the wrong place for "remind people 10 minutes before class" and "a class is 45
minutes", which are decisions the person running the school makes, changes their mind about,
and cannot make at all if it needs an engineer and a restart. The brief asks for exactly
these to be settable by the admin - for both products - so they live in Firestore.

The resolution order is: the stored document, then the environment default, then a hardcoded
constant. That ordering matters more than it looks. It means the feature ships working with
no configuration at all, an existing deployment's `.env` keeps deciding behaviour until
somebody actually changes something in the admin UI, and a Firestore outage degrades to the
environment values rather than to nothing.

One document per program, in `app_settings`. Cached, because the reminder sweep reads it
every two minutes and every timezone conversion in the API reads it too.
"""

import logging
from typing import Any

from app.core.config import settings as env_settings
from app.core.enums import Program
from app.core.firebase import document_cache, firestore_app_settings

logger = logging.getLogger("tuition.settings")

TUITION_DOC = Program.TUITION.value.lower()   # "tuition"
LMS_DOC = Program.LMS.value.lower()           # "lms"


def _int(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _float(value: Any, fallback: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _bool(value: Any, fallback: bool) -> bool:
    if value is None:
        return fallback
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "on"}:
        return True
    if text in {"false", "0", "no", "off"}:
        return False
    return fallback


def _mode(value: Any, fallback: str) -> str:
    """
    An identifier mode, coerced to AUTO or MANUAL.

    Anything unrecognized falls back rather than raising, for the same reason the other
    coercers here do: a hand-edited settings document should cost one behaviour, not every
    request that reads the configuration.
    """
    from app.core.enums import IdentifierMode

    text = str(value or "").strip().upper()
    if text in {m.value for m in IdentifierMode}:
        return text
    fallback_text = str(fallback or "").strip().upper()
    if fallback_text in {m.value for m in IdentifierMode}:
        return fallback_text
    return IdentifierMode.AUTO.value


def _rate_source(value: Any, fallback: str = "MANUAL") -> str:
    """
    Where a currency's rate comes from: LIVE or MANUAL.

    Defaults to MANUAL, and anything unrecognized falls back to it rather than raising - the
    conservative direction, since MANUAL uses a number the school chose and LIVE reaches out
    to a third party.
    """
    text = str(value or "").strip().upper()
    return text if text in {"LIVE", "MANUAL"} else fallback


def _rounding(value: Any, fallback: str) -> str:
    """A rounding mode, coerced to one this codebase implements."""
    allowed = {"NONE", "NEAREST", "UP", "DOWN"}
    text = str(value or "").strip().upper()
    if text in allowed:
        return text
    fallback_text = str(fallback or "").strip().upper()
    return fallback_text if fallback_text in allowed else "NONE"


def _offsets(value: Any, fallback: list[int]) -> list[int]:
    """
    Reminder offsets, cleaned up.

    Accepts a list of numbers or a "10, 60" string, because the same setting arrives from a
    JSON body one way and from an environment variable the other. Unreadable entries are
    dropped rather than raising: a bad offset should cost one reminder, not the sweep.
    """
    if value is None:
        return list(fallback)
    if isinstance(value, str):
        tokens = value.replace(",", " ").split()
    else:
        try:
            tokens = list(value)
        except TypeError:
            return list(fallback)

    resolved = set()
    for token in tokens:
        try:
            number = int(token)
        except (TypeError, ValueError):
            continue
        if number >= 0:
            resolved.add(number)
    return sorted(resolved, reverse=True) or list(fallback)


def _stored(program_doc: str) -> dict:
    """The stored overrides for a program, or an empty dict when there are none."""
    try:
        return firestore_app_settings.get_document(program_doc) or {}
    except Exception as exc:  # pragma: no cover - configuration must never 500 a request
        logger.warning("Could not read '%s' settings (%s); using environment defaults.",
                       program_doc, exc)
        return {}


def tuition_settings() -> dict:
    """
    The effective tuition configuration.

    Always returns every key, fully typed. Callers can therefore read
    `tuition_settings()["default_session_minutes"]` without defaulting at each use site,
    which is what keeps the fallback logic in this one file instead of scattered across the
    scheduler, the session service and the reminder sweep.
    """
    stored = _stored(TUITION_DOC)
    return {
        "program": Program.TUITION.value,
        "timezone": (stored.get("timezone") or env_settings.resolved_tuition_timezone).strip(),
        "default_session_minutes": _int(
            stored.get("default_session_minutes"),
            env_settings.TUITION_DEFAULT_SESSION_MINUTES,
        ),
        "reminders_enabled": _bool(
            stored.get("reminders_enabled"), env_settings.ENABLE_TUITION_REMINDERS
        ),
        "reminder_minutes_before": _offsets(
            stored.get("reminder_minutes_before"), env_settings.tuition_reminder_offsets
        ),
        "remind_teachers": _bool(stored.get("remind_teachers"), True),
        "reminder_max_lateness_minutes": _int(
            stored.get("reminder_max_lateness_minutes"),
            env_settings.REMINDER_MAX_LATENESS_MINUTES,
        ),
        "auto_start_class": _bool(
            stored.get("auto_start_class"), env_settings.TUITION_AUTO_START_CLASS
        ),
        "max_teacher_late_extension_minutes": _int(
            stored.get("max_teacher_late_extension_minutes"),
            env_settings.TUITION_MAX_TEACHER_LATE_EXTENSION_MINUTES,
        ),
        "teacher_no_show_minutes": _int(
            stored.get("teacher_no_show_minutes"),
            env_settings.TUITION_TEACHER_NO_SHOW_MINUTES,
        ),
        "student_late_grace_minutes": _int(
            stored.get("student_late_grace_minutes"),
            env_settings.TUITION_STUDENT_LATE_GRACE_MINUTES,
        ),
        "min_gap_minutes": _int(
            stored.get("min_gap_minutes"), env_settings.TUITION_MIN_GAP_MINUTES
        ),
        "session_horizon_days": _int(
            stored.get("session_horizon_days"), env_settings.TUITION_SESSION_HORIZON_DAYS
        ),
        "student_uploads_need_approval": _bool(
            stored.get("student_uploads_need_approval"),
            env_settings.TUITION_STUDENT_UPLOADS_NEED_APPROVAL,
        ),
        "currency": (stored.get("currency") or env_settings.TUITION_CURRENCY).strip() or "INR",
        # Library access for students. Two independent halves - see `STUDENT_LIBRARY_*`
        # in config for why a school might want either without the other.
        "student_library_uploads_enabled": _bool(
            stored.get("student_library_uploads_enabled"),
            env_settings.STUDENT_LIBRARY_UPLOADS_ENABLED,
        ),
        "student_library_downloads_enabled": _bool(
            stored.get("student_library_downloads_enabled"),
            env_settings.STUDENT_LIBRARY_DOWNLOADS_ENABLED,
        ),
        "library_syllabus_filter": _bool(
            stored.get("library_syllabus_filter"), env_settings.LIBRARY_SYLLABUS_FILTER
        ),
        "auto_create_meet": _bool(stored.get("auto_create_meet"), env_settings.ENABLE_GOOGLE_MEET),
    }


def lms_settings() -> dict:
    """
    The effective LMS reminder configuration.

    Narrower than the tuition equivalent because the LMS's other behaviour is already
    settled by its own module; what the brief asks to be admin-editable here is the reminder
    timing and the timezone, and adding more would be inventing requirements.
    """
    stored = _stored(LMS_DOC)
    return {
        "program": Program.LMS.value,
        "timezone": (stored.get("timezone") or env_settings.resolved_school_timezone).strip(),
        "reminders_enabled": _bool(
            stored.get("reminders_enabled"), env_settings.ENABLE_CLASS_REMINDERS
        ),
        "reminder_minutes_before": _offsets(
            stored.get("reminder_minutes_before"), env_settings.reminder_offsets
        ),
        "remind_teachers": _bool(stored.get("remind_teachers"), env_settings.REMIND_TEACHERS),
        "reminder_max_lateness_minutes": _int(
            stored.get("reminder_max_lateness_minutes"),
            env_settings.REMINDER_MAX_LATENESS_MINUTES,
        ),
        # Live classes. The school equivalents of the tuition timing settings, so a student
        # who takes both products meets one answer to "can I join yet?" rather than two.
        "auto_start_class": _bool(
            stored.get("auto_start_class"), env_settings.SCHOOL_AUTO_START_CLASS
        ),
        "join_open_minutes_before": _int(
            stored.get("join_open_minutes_before"),
            env_settings.SCHOOL_JOIN_OPEN_MINUTES_BEFORE,
        ),
        "default_class_minutes": _int(
            stored.get("default_class_minutes"), env_settings.SCHOOL_DEFAULT_CLASS_MINUTES
        ),
        "join_grace_minutes": _int(
            stored.get("join_grace_minutes"), env_settings.SCHOOL_JOIN_GRACE_MINUTES
        ),
        "extra_class_needs_approval": _bool(
            stored.get("extra_class_needs_approval"), env_settings.EXTRA_CLASS_NEEDS_APPROVAL
        ),
        # Library access for students. Two independent halves - see `STUDENT_LIBRARY_*`
        # in config for why a school might want either without the other.
        "student_library_uploads_enabled": _bool(
            stored.get("student_library_uploads_enabled"),
            env_settings.STUDENT_LIBRARY_UPLOADS_ENABLED,
        ),
        "student_library_downloads_enabled": _bool(
            stored.get("student_library_downloads_enabled"),
            env_settings.STUDENT_LIBRARY_DOWNLOADS_ENABLED,
        ),
        "library_syllabus_filter": _bool(
            stored.get("library_syllabus_filter"), env_settings.LIBRARY_SYLLABUS_FILTER
        ),
        # Identifier issuing. See `IdentifierMode` for why both modes have to exist; the
        # prefixes are here rather than in the environment because a school renames its
        # admission series far more often than it redeploys.
        "admission_id_mode": _mode(
            stored.get("admission_id_mode"), env_settings.SCHOOL_ADMISSION_ID_MODE
        ),
        "admission_id_prefix": (
            stored.get("admission_id_prefix") or env_settings.SCHOOL_ADMISSION_PREFIX
        ).strip() or "ADM",
        "employee_id_mode": _mode(
            stored.get("employee_id_mode"), env_settings.SCHOOL_EMPLOYEE_ID_MODE
        ),
        "employee_id_prefix": (
            stored.get("employee_id_prefix") or env_settings.SCHOOL_EMPLOYEE_PREFIX
        ).strip() or "EMP",
    }


def finance_settings(program: str = Program.LMS.value) -> dict:
    """
    The effective money configuration for a program: charges, tax, late fees, rounding.

    Read from the same per-program document as the rest of the settings rather than a
    separate one. A second document would mean two reads on every fee calculation and two
    places to look when a figure is wrong, and these values are edited on the same admin
    screen anyway.

    Defaults are deliberately inert - tax off, late fee off, no convenience charge, no
    rounding - so a school that never opens the finance screen bills exactly the amounts
    somebody typed into the fee structure, with nothing added that they did not ask for.
    """
    stored = _stored(str(program).lower())
    default_currency = (
        env_settings.TUITION_CURRENCY
        if str(program).upper() == Program.TUITION.value
        else env_settings.SCHOOL_CURRENCY
    )

    return {
        "program": str(program).upper(),
        "currency": (stored.get("currency") or default_currency).strip() or "INR",

        "tax_enabled": _bool(stored.get("tax_enabled"), env_settings.FINANCE_TAX_ENABLED),
        "tax_percent": _float(stored.get("tax_percent"), env_settings.FINANCE_TAX_PERCENT),
        "tax_label": (stored.get("tax_label") or env_settings.FINANCE_TAX_LABEL).strip() or "Tax",
        "tax_inclusive": _bool(
            stored.get("tax_inclusive"), env_settings.FINANCE_TAX_INCLUSIVE
        ),

        "late_fee_enabled": _bool(
            stored.get("late_fee_enabled"), env_settings.FINANCE_LATE_FEE_ENABLED
        ),
        "late_fee_percent": _float(
            stored.get("late_fee_percent"), env_settings.FINANCE_LATE_FEE_PERCENT
        ),
        "late_fee_amount": _float(
            stored.get("late_fee_amount"), env_settings.FINANCE_LATE_FEE_AMOUNT
        ),
        "late_fee_grace_days": _int(
            stored.get("late_fee_grace_days"), env_settings.FINANCE_LATE_FEE_GRACE_DAYS
        ),

        "convenience_percent": _float(
            stored.get("convenience_percent"), env_settings.FINANCE_CONVENIENCE_PERCENT
        ),
        "convenience_amount": _float(
            stored.get("convenience_amount"), env_settings.FINANCE_CONVENIENCE_AMOUNT
        ),

        "invoice_prefix": (
            stored.get("invoice_prefix") or env_settings.FINANCE_INVOICE_PREFIX
        ).strip() or "INV",
        "invoice_due_days": _int(
            stored.get("invoice_due_days"), env_settings.FINANCE_INVOICE_DUE_DAYS
        ),
        "rounding": _rounding(stored.get("rounding"), env_settings.FINANCE_ROUNDING),

        # No provider is wired. The flag exists so the payment page can decide whether to
        # offer "pay online" without the frontend hardcoding the answer.
        "gateway_enabled": _bool(
            stored.get("gateway_enabled"), env_settings.FINANCE_GATEWAY_ENABLED
        ),
        "gateway_provider": (
            stored.get("gateway_provider") or env_settings.FINANCE_GATEWAY_PROVIDER
        ).strip() or None,
    }


def program_settings(program: str) -> dict:
    """Effective settings for either program, by value ('LMS' or 'TUITION')."""
    return lms_settings() if str(program).upper() == Program.LMS.value else tuition_settings()


# Which keys each program will accept from an update. An unknown key is rejected rather than
# stored, so a typo in the admin UI ("remind_teacher") fails loudly at the API instead of
# being written to Firestore and silently ignored forever after.
#
# Written out rather than derived from the resolver functions above: deriving it would mean
# a Firestore read at import time, which is the least reliable moment in the process to
# reach a network service and the exact failure this codebase has been bitten by before.
#
# Split into a shared core and a per-program remainder. The identifier keys below are LMS
# only - tuition issues its own numbers from `TUITION_ADMISSION_PREFIX`, and listing them for
# both programs would let an admin save a tuition value that nothing ever reads back, which
# is precisely the silent no-op this whole mechanism exists to prevent.
_SHARED_KEYS = frozenset({
    "timezone", "reminders_enabled", "reminder_minutes_before", "remind_teachers",
    "reminder_max_lateness_minutes",
})
# The money settings, editable for both programs from the same admin screen. `currency` is
# in here rather than in the tuition block because both products bill in one, and having it
# defined twice is how a school ends up with an invoice in rupees and a receipt in dirhams.
_FINANCE_KEYS = frozenset({
    "currency",
    "tax_enabled", "tax_percent", "tax_label", "tax_inclusive",
    "late_fee_enabled", "late_fee_percent", "late_fee_amount", "late_fee_grace_days",
    "convenience_percent", "convenience_amount",
    "invoice_prefix", "invoice_due_days", "rounding",
    "gateway_enabled", "gateway_provider",
})
# Library access, writable for both products.
_LIBRARY_KEYS = frozenset({
    "student_library_uploads_enabled", "student_library_downloads_enabled",
    "library_syllabus_filter",
})
_LMS_KEYS = _SHARED_KEYS | _FINANCE_KEYS | _LIBRARY_KEYS | {
    "admission_id_mode", "admission_id_prefix",
    "employee_id_mode", "employee_id_prefix",
    "auto_start_class", "join_open_minutes_before", "default_class_minutes",
    "join_grace_minutes", "extra_class_needs_approval",
}
WRITABLE_KEYS = {
    Program.LMS.value: _LMS_KEYS,
    Program.TUITION.value: _SHARED_KEYS | _FINANCE_KEYS | _LIBRARY_KEYS | {
        "default_session_minutes", "auto_start_class", "max_teacher_late_extension_minutes",
        "teacher_no_show_minutes", "student_late_grace_minutes", "min_gap_minutes",
        "session_horizon_days", "student_uploads_need_approval", "currency",
        "auto_create_meet",
    },
}


def save_settings(program: str, updates: dict, actor_id: int | None = None) -> dict:
    """
    Merges an admin's changes into a program's settings document.

    Merge rather than replace: the admin UI may only render half these fields, and a PUT
    that dropped the rest would reset behaviour nobody meant to touch. Returns the full
    effective settings afterwards, so the caller sees what actually took effect - including
    the environment values that filled the gaps.
    """
    from datetime import datetime

    program_value = str(program).upper()
    allowed = WRITABLE_KEYS.get(program_value)
    if allowed is None:
        raise ValueError(f"Unknown program '{program}'")

    payload = {key: value for key, value in updates.items() if key in allowed and value is not None}
    if payload:
        payload["updated_at"] = datetime.utcnow().isoformat()
        payload["updated_by"] = actor_id
        firestore_app_settings.add_document(program_value.lower(), payload)
        # add_document invalidates this key already; clearing the whole collection's cache
        # too is cheap here and covers the reminder sweep's separate read path.
        document_cache.invalidate(firestore_app_settings.collection_name)

    return program_settings(program_value)


# ---------------------------------------------------------------------------------------
# Multi-currency
#
# A school publishes its fees in one currency and collects in another. The two are not a
# straight conversion: the charges differ per currency because the costs behind them differ -
# a domestic transfer in INR carries a gateway percentage an AED card payment does not, and
# the tax position is rarely the same either.
#
# So a currency is not just a rate. It is a rate *and its own charge rules*, and this is where
# both live. Every key a currency does not override falls through to the program's own
# finance settings, which means a school running a single currency never has to fill any of
# this in.
# ---------------------------------------------------------------------------------------

# Symbols for the currencies this is expected to be used with. Only a display convenience -
# an unknown currency falls back to its code, which is always correct if less pretty.
_CURRENCY_SYMBOLS = {
    "INR": "₹",
    "AED": "د.إ",
    "USD": "$",
    "GBP": "£",
    "EUR": "€",
    "SAR": "ر.س",
    "QAR": "ر.ق",
    "OMR": "ر.ع.",
}

# What a currency may override. Deliberately the charge-shaped keys only: `invoice_prefix`
# and the identifier settings are properties of the school, not of the money, and letting a
# currency override them would produce two invoice number series for one set of bills.
CURRENCY_OVERRIDE_KEYS = frozenset({
    "tax_enabled", "tax_percent", "tax_label", "tax_inclusive",
    "late_fee_enabled", "late_fee_percent", "late_fee_amount", "late_fee_grace_days",
    "convenience_percent", "convenience_amount", "rounding",
})


def _supported_codes() -> list[str]:
    """The currency codes this deployment offers, from the environment default."""
    raw = (env_settings.FINANCE_SUPPORTED_CURRENCIES or "").replace(",", " ").split()
    codes = [c.strip().upper() for c in raw if c.strip()]
    return codes or [env_settings.FINANCE_BASE_CURRENCY.upper()]


def currency_settings(program: str = Program.LMS.value) -> dict:
    """
    Every currency this program offers, each with its rate and its own charge rules.

    The base currency always has a rate of exactly 1 and cannot be otherwise - it is the
    unit everything else is expressed in, and a base currency with a rate would make the
    arithmetic circular.

    A currency present in the stored document but disabled is still returned, marked
    `enabled: false`, so the admin screen can show it as a row to turn back on rather than
    losing the rate they configured last term.
    """
    stored = _stored(str(program).lower())
    base = (stored.get("base_currency") or env_settings.FINANCE_BASE_CURRENCY).strip().upper()
    defaults = finance_settings(program)

    configured = stored.get("currencies") or {}
    if not isinstance(configured, dict):
        configured = {}

    # The base currency is always offered, even if nobody has configured it, because a
    # deployment with no currency at all cannot render a fee.
    codes = list(dict.fromkeys(
        [base] + _supported_codes() + [str(c).upper() for c in configured]
    ))

    resolved = {}
    for code in codes:
        overrides = configured.get(code) or configured.get(code.lower()) or {}
        if not isinstance(overrides, dict):
            overrides = {}

        entry = {
            "code": code,
            "symbol": (overrides.get("symbol") or _CURRENCY_SYMBOLS.get(code) or code),
            "is_base": code == base,
            "enabled": _bool(overrides.get("enabled"), True),
            # The administrator's own rate. Always kept, even when the currency is set to
            # LIVE, because it is the floor the live path falls back to when the provider is
            # unreachable - a school that has configured one never sees a broken fee page.
            "manual_rate": 1.0 if code == base else _float(
                overrides.get("rate_from_base"), 0.0
            ),
            # LIVE fetches from the FX provider; MANUAL uses the rate above and ignores it.
            # MANUAL is the default: a published fee is a commercial decision a school holds
            # for a term, and a bill that moves with the spot rate is one a parent can argue
            # with. LIVE is opted into per currency.
            "rate_source": _rate_source(overrides.get("rate_source")),
        }

        # Resolve the effective rate. The base is 1 by definition and never consults
        # anything; a mistyped rate on the base would otherwise rescale every fee in the
        # system.
        if code == base:
            entry["rate_from_base"] = 1.0
            entry["rate_status"] = "base"
            entry["rate_fetched_at"] = None
        elif entry["rate_source"] == "LIVE":
            from app.core import fx

            live = fx.rate_for(base, code)
            if live.get("rate"):
                entry["rate_from_base"] = round(float(live["rate"]), 6)
                entry["rate_status"] = live["status"]
                entry["rate_fetched_at"] = live.get("fetched_at")
            else:
                # Falls back rather than failing. See `app.core.fx` - a fee page that 500s
                # because a currency API is down is worse than one showing the school's own
                # configured rate.
                entry["rate_from_base"] = entry["manual_rate"]
                entry["rate_status"] = "fallback_manual"
                entry["rate_fetched_at"] = None
                entry["rate_detail"] = live.get("detail")
        else:
            entry["rate_from_base"] = entry["manual_rate"]
            entry["rate_status"] = "manual"
            entry["rate_fetched_at"] = None

        entry.setdefault("rate_detail", None)

        # Charge rules: this currency's own, else the program's.
        for key in CURRENCY_OVERRIDE_KEYS:
            if key in overrides and overrides[key] is not None:
                if key in {"tax_enabled", "tax_inclusive", "late_fee_enabled"}:
                    entry[key] = _bool(overrides[key], defaults[key])
                elif key == "rounding":
                    entry[key] = _rounding(overrides[key], defaults[key])
                elif key == "tax_label":
                    entry[key] = str(overrides[key]).strip() or defaults[key]
                elif key == "late_fee_grace_days":
                    entry[key] = _int(overrides[key], defaults[key])
                else:
                    entry[key] = _float(overrides[key], defaults[key])
                entry.setdefault("_overrides", set()).add(key)
            else:
                entry[key] = defaults[key]

        entry["overrides"] = sorted(entry.pop("_overrides", set()))
        resolved[code] = entry

    available = [
        c for c, e in resolved.items()
        if e["enabled"] and (e["is_base"] or e["rate_from_base"] > 0)
    ]

    return {
        "program": str(program).upper(),
        "base_currency": base,
        "currencies": resolved,
        # Only the ones a payer may actually pick. A currency with no rate is not offered -
        # showing a price of zero is worse than not showing the currency at all.
        "available": available,
        # The base is recommended: it is the currency the fees were set in, the only one
        # that never carries a conversion, and the one the office reconciles in.
        "recommended_currency": base,
        "options": [currency_option(resolved[code]) for code in available],
    }


def _trim(value) -> str:
    number = float(value or 0)
    return str(int(number)) if number == int(number) else f"{number:g}"


def charges_summary(entry: dict) -> str:
    """
    One line saying what a currency adds on top of the fee - "GST 18% + 2% convenience".

    Shown next to each currency in the payer's switcher, so choosing AED over INR is a
    choice made knowing that AED carries VAT and a card surcharge, rather than a surprise
    on the next screen.
    """
    parts = []
    if entry.get("tax_enabled") and float(entry.get("tax_percent") or 0) > 0:
        label = entry.get("tax_label") or "Tax"
        suffix = " incl." if entry.get("tax_inclusive") else ""
        parts.append(f"{label} {_trim(entry['tax_percent'])}%{suffix}")
    conv = []
    if float(entry.get("convenience_percent") or 0) > 0:
        conv.append(f"{_trim(entry['convenience_percent'])}%")
    if float(entry.get("convenience_amount") or 0) > 0:
        symbol = entry.get("symbol") or entry.get("code") or ""
        conv.append(f"{symbol}{_trim(entry['convenience_amount'])}")
    if conv:
        parts.append(" + ".join(conv) + " convenience")
    return " + ".join(parts) if parts else "No extra charges"


def currency_option(entry: dict) -> dict:
    """
    One row of the payer's currency switcher.

    Carries the charge rules the currency brings with it - not just the code - so a page can
    show what each choice costs before the payer makes it. `recommended` marks the base.
    """
    return {
        "code": entry["code"],
        "symbol": entry.get("symbol"),
        "is_base": bool(entry.get("is_base")),
        "recommended": bool(entry.get("is_base")),
        "rate_from_base": float(entry.get("rate_from_base") or 1.0),
        "rate_status": entry.get("rate_status"),
        "tax_enabled": bool(entry.get("tax_enabled")),
        "tax_percent": float(entry.get("tax_percent") or 0),
        "tax_label": entry.get("tax_label") or "Tax",
        "tax_inclusive": bool(entry.get("tax_inclusive")),
        "convenience_percent": float(entry.get("convenience_percent") or 0),
        "convenience_amount": float(entry.get("convenience_amount") or 0),
        "rounding": entry.get("rounding") or "NONE",
        "charges_summary": charges_summary(entry),
    }


def resolve_currency(program: str, currency: str | None = None) -> dict:
    """
    The charge rules and rate for one currency, ready to price with.

    Falls back to the base currency when the caller names none, or names one this program
    does not offer. Falling back rather than raising is deliberate: a stale bookmark with
    `?currency=USD` should show the fee in rupees, not an error page.
    """
    config = currency_settings(program)
    wanted = str(currency or "").strip().upper()

    if wanted and wanted in config["currencies"]:
        entry = config["currencies"][wanted]
        if entry["enabled"] and (entry["is_base"] or entry["rate_from_base"] > 0):
            return {**entry, "base_currency": config["base_currency"],
                    "available": config["available"],
                    "recommended_currency": config["recommended_currency"],
                    "options": config["options"],
                    "all_currencies": config["currencies"]}

    base = config["currencies"][config["base_currency"]]
    return {**base, "base_currency": config["base_currency"],
            "available": config["available"],
            "recommended_currency": config["recommended_currency"],
            "options": config["options"],
            "all_currencies": config["currencies"]}


def save_currency_settings(program: str, base_currency: str | None,
                           currencies: dict, actor_id: int | None = None) -> dict:
    """
    Merges an admin's currency changes into a program's settings.

    Merged per currency rather than replaced wholesale, so an admin editing the AED rate does
    not have to resend the INR charge rules they were not looking at. An unknown key inside a
    currency is dropped rather than stored, for the same reason `save_settings` rejects one:
    a typo that saves silently and is ignored forever is worse than one that fails.
    """
    from datetime import datetime

    program_value = str(program).upper()
    stored = _stored(program_value.lower())
    existing = dict(stored.get("currencies") or {})

    for code, overrides in (currencies or {}).items():
        code = str(code).strip().upper()
        if not code:
            continue
        if not isinstance(overrides, dict):
            continue

        current = dict(existing.get(code) or {})
        for key, value in overrides.items():
            if value is None:
                continue
            if key in CURRENCY_OVERRIDE_KEYS or key in {
                "enabled", "symbol", "rate_from_base", "rate_source"
            }:
                current[key] = value
        existing[code] = current

    payload = {
        "currencies": existing,
        "updated_at": datetime.utcnow().isoformat(),
        "updated_by": actor_id,
    }
    if base_currency:
        payload["base_currency"] = str(base_currency).strip().upper()

    firestore_app_settings.add_document(program_value.lower(), payload)
    document_cache.invalidate(firestore_app_settings.collection_name)
    return currency_settings(program_value)
