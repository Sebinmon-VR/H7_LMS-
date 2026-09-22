"""
Academic years and admission categories.

Two small reference collections and one genuinely load-bearing rule: exactly one year is
current **per program** at a time. Everything that later asks "this year's students", "this
year's fee plan" or "this year's report" resolves through `current_year(program)`, and if two
documents could both claim the flag those callers would each pick a different winner -
silently, and differently on different screens.

Per program, not globally, because the school and the tuition programme keep their own
calendars. A school year running April to March and a tuition batch running January to
December are both current, at the same time, and neither should un-set the other. A year may
declare both programs, in which case it is the current year for both and collides with both.

The flag is therefore not something a caller sets directly. `_make_current` clears it across
the affected programs first, so the invariant is restored by the same write that could break
it. Year *names* are scoped the same way: a school and a tuition programme may both run
something called "2025-26".

Deletion is guarded rather than cascading. A year with students admitted into it is the one
record that must not vanish: the students would keep the id, the id would resolve to nothing,
and every historical report would quietly lose a year's worth of rows. The admin is told what
is in the way and asked to move it.
"""

import logging
import re
from datetime import date, datetime, timedelta

from fastapi import HTTPException

from app.core.enums import (
    ACADEMIC_YEAR_START_MONTH, TERM_2_START_MONTH, TERM_NAMES, AcademicTerm,
    AcademicYearStatus, Program, UserRole,
)
from app.core.firebase import (
    firestore_academic_years, firestore_admission_categories,
    firestore_student_enrollments, firestore_users, require_document,
)

logger = logging.getLogger("admissions")


def _now() -> str:
    return datetime.utcnow().isoformat()


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _programs(value, fallback=(Program.LMS.value,)) -> list[str]:
    """
    Program list for a year or category.

    Unlike `normalize_programs` on a user profile, an empty list here resolves to the
    fallback rather than to nothing, because a school that runs one product never fills this
    in and a year that reached no product at all would be a baffling default. Resolved to an
    explicit list so callers never have to branch on empty.
    """
    if not value:
        return list(fallback)
    resolved = [getattr(p, "value", str(p)).upper() for p in value]
    return [p for p in resolved if p in {Program.LMS.value, Program.TUITION.value}] or list(fallback)


# ---------------------------------------------------------------------------------------
# The calendar: an April-March year, cut into two terms
#
# The school year runs April to March and is billed in two halves: Term 1 to the end of
# October, Term 2 from November. Both packages and instalments hang off these dates, which
# is why they are derived in one place and stored on the year rather than recomputed by
# every caller from the constants - a school that cuts its terms differently edits the year,
# and everything downstream follows.
# ---------------------------------------------------------------------------------------

def _as_date(value) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _day_before(day: date) -> date:
    return day - timedelta(days=1)


def default_year_window(name: str | None, today: date | None = None) -> tuple[date, date]:
    """
    The April-March window a year name refers to.

    "2026-27", "2026-2027", "AY 2026/27" and "Tuition 2026" all resolve to 1 April 2026 -
    31 March 2027: the first four-digit year in the name is the year the session starts in.
    A name carrying no year resolves to the April-March window the clock is currently in,
    which is what a school creating "Current year" on a fresh install means by it.
    """
    match = re.search(r"(20\d{2})", str(name or ""))
    if match:
        start_year = int(match.group(1))
    else:
        now = today or date.today()
        start_year = now.year if now.month >= ACADEMIC_YEAR_START_MONTH else now.year - 1

    start = date(start_year, ACADEMIC_YEAR_START_MONTH, 1)
    end = _day_before(date(start_year + 1, ACADEMIC_YEAR_START_MONTH, 1))
    return start, end


def default_terms(start: date, end: date) -> list[dict]:
    """
    Cuts a year into Term 1 and Term 2 at the start of `TERM_2_START_MONTH`.

    For the standard April-March year that is April-October and November-March. For a year
    that does not contain a November at all - a short summer batch - the split falls at the
    midpoint instead, so every year has two terms and nothing downstream has to special-case
    a year with one.
    """
    boundary = None
    for year in (start.year, start.year + 1):
        candidate = date(year, TERM_2_START_MONTH, 1)
        if start < candidate <= end:
            boundary = candidate
            break
    if boundary is None:
        boundary = start + timedelta(days=(end - start).days // 2 + 1)

    return [
        {
            "key": AcademicTerm.TERM_1.value,
            "name": TERM_NAMES[AcademicTerm.TERM_1.value],
            "start_date": start.isoformat(),
            "end_date": _day_before(boundary).isoformat(),
        },
        {
            "key": AcademicTerm.TERM_2.value,
            "name": TERM_NAMES[AcademicTerm.TERM_2.value],
            "start_date": boundary.isoformat(),
            "end_date": end.isoformat(),
        },
    ]


def _field(entry, name):
    if isinstance(entry, dict):
        return entry.get(name)
    return getattr(entry, name, None)


def _validate_terms(terms, start: date | None, end: date | None) -> list[dict]:
    """
    Normalises admin-supplied terms and refuses ones that do not fit the year.

    Both terms are required, in order and without overlap, and inside the year's dates: a
    Term 2 that ends after the year does is the kind of record that bills a March class into
    a year that has closed.
    """
    cleaned = []
    for entry in terms or []:
        key = getattr(_field(entry, "key"), "value", _field(entry, "key"))
        term_start = _as_date(_field(entry, "start_date"))
        term_end = _as_date(_field(entry, "end_date"))
        if key not in TERM_NAMES or not term_start or not term_end:
            raise HTTPException(
                status_code=400, detail="Each term needs key, start_date and end_date."
            )
        cleaned.append({
            "key": key,
            "name": (_field(entry, "name") or TERM_NAMES[key]).strip(),
            "start_date": term_start.isoformat(),
            "end_date": term_end.isoformat(),
        })

    keys = [t["key"] for t in cleaned]
    if sorted(keys) != sorted(TERM_NAMES):
        raise HTTPException(
            status_code=400,
            detail=f"A year has exactly two terms: {', '.join(TERM_NAMES)}.",
        )
    cleaned.sort(key=lambda t: t["start_date"])
    first, second = cleaned
    if first["end_date"] >= second["start_date"]:
        raise HTTPException(status_code=400, detail="Term 1 must end before Term 2 starts.")
    if start and first["start_date"] < start.isoformat():
        raise HTTPException(status_code=400, detail="Term 1 cannot start before the year does.")
    if end and second["end_date"] > end.isoformat():
        raise HTTPException(status_code=400, detail="Term 2 cannot end after the year does.")
    return cleaned


def terms_of(year: dict) -> list[dict]:
    """
    A year's terms, derived from its dates when none were stored.

    Years created before terms existed carry none; deriving here rather than migrating means
    they bill correctly the moment this code deploys, and the derived dates are what a
    migration would have written anyway.
    """
    stored = year.get("terms") or []
    if len(stored) == 2 and all(t.get("start_date") and t.get("end_date") for t in stored):
        return sorted(
            [{"key": t.get("key"), "name": t.get("name") or TERM_NAMES.get(t.get("key"), ""),
              "start_date": str(t["start_date"])[:10], "end_date": str(t["end_date"])[:10]}
             for t in stored],
            key=lambda t: t["start_date"],
        )
    start = _as_date(year.get("start_date"))
    end = _as_date(year.get("end_date"))
    if not start or not end or end <= start:
        return []
    return default_terms(start, end)


def term_for_date(year: dict, on: date) -> dict | None:
    """The term of `year` that a date falls in, or None when the date is outside both."""
    wanted = on.isoformat()
    for term in terms_of(year):
        if term["start_date"] <= wanted <= term["end_date"]:
            return term
    return None


def term_window(year: dict, term_key: str | None) -> tuple[date | None, date | None]:
    """The dates of one term, or of the whole year when no term is named."""
    if term_key:
        for term in terms_of(year):
            if term["key"] == term_key:
                return _as_date(term["start_date"]), _as_date(term["end_date"])
        return None, None
    return _as_date(year.get("start_date")), _as_date(year.get("end_date"))


# ---------------------------------------------------------------------------------------
# Academic years
# ---------------------------------------------------------------------------------------

def list_years(program: str | None = None) -> list[dict]:
    """
    Every year, newest first.

    Sorted by start date rather than by id: a school backfilling last year's records creates
    it after this year's, and an id-ordered list would put it on top.
    """
    years = firestore_academic_years.list_all()
    if program:
        wanted = str(program).upper()
        years = [y for y in years if wanted in _programs(y.get("programs"))]
    return sorted(years, key=lambda y: str(y.get("start_date") or ""), reverse=True)


def require_year(year_id) -> dict:
    return require_document(firestore_academic_years, year_id, "Academic year")


def current_year(program: str = Program.LMS.value) -> dict | None:
    """
    The year everything else defaults to.

    Falls back to the year the clock is actually inside when nobody has set the flag, so a
    deployment that never opened the admin screen still behaves sensibly. Returns None only
    when no year has been created at all - callers treat that as "not configured yet" rather
    than as an error, because every feature here has to keep working for a school that has
    not got round to admissions setup.
    """
    years = list_years(program)
    for year in years:
        if year.get("is_current"):
            return year

    today = date.today().isoformat()
    for year in years:
        start, end = str(year.get("start_date") or ""), str(year.get("end_date") or "")
        if start and end and start <= today <= end:
            return year
    return None


def _shares_program(a: dict, programs: list[str]) -> bool:
    """Whether a stored year overlaps a set of programs at all."""
    return bool(set(_programs(a.get("programs"))) & set(programs))


def belongs_to_program(record: dict, program: str) -> bool:
    """
    Whether a year or category applies to one product.

    The tuition admissions screen lists and edits only tuition records, and answers 404 for
    a school year rather than 403: the id is not wrong for lack of permission, it is simply
    not on that board.
    """
    return str(program).upper() in _programs(record.get("programs"))


def user_in_program(user: dict, program: str) -> bool:
    """A profile with no `programs` field is an LMS account - the codebase-wide default."""
    programs = user.get("programs") or [Program.LMS.value]
    return str(program).upper() in [str(p).upper() for p in programs]


def _make_current(year_id, programs: list[str]) -> None:
    """
    Clears `is_current` on other years **of the same program**.

    Per program, not globally. The school and the tuition programme run on their own
    calendars - a tuition batch starting in January while the school year runs April to March
    is the normal case, not an edge one - so marking the tuition year current must not
    silently un-set the school's. `current_year()` filters by program on the way out, and a
    global clear here would leave it with nothing to find.

    A year belonging to both programs collides with both, which is correct: it *is* the
    current year for both, and nothing else can be.
    """
    for other in firestore_academic_years.list_all():
        if str(other.get("id")) == str(year_id) or not other.get("is_current"):
            continue
        if _shares_program(other, programs):
            firestore_academic_years.add_document(str(other["id"]), {"is_current": False})


def _assert_name_free(name: str, programs: list[str], exclude_id=None) -> None:
    """
    Rejects a duplicate year name within the same program.

    Scoped rather than global for the same reason as above: a school and a tuition programme
    may both legitimately run something called "2025-26", and refusing the second one would
    force a naming convention on the admin to work around a check that was only ever meant to
    catch a genuine double-entry.
    """
    lowered = str(name).strip().lower()
    for year in firestore_academic_years.list_all():
        if str(year.get("name", "")).strip().lower() != lowered:
            continue
        if exclude_id is not None and str(year.get("id")) == str(exclude_id):
            continue
        if not _shares_program(year, programs):
            continue
        raise HTTPException(
            status_code=400,
            detail=(
                f"An academic year named '{name}' already exists for "
                f"{', '.join(_programs(year.get('programs')))} (id {year['id']})."
            ),
        )


def year_for_date(on: date, program: str = Program.LMS.value) -> dict | None:
    """
    The year a given date falls inside, for one program.

    Used by billing to decide which year's fees apply to an invoice period, rather than
    trusting whichever year happens to be flagged current - an invoice raised in April for
    March's classes belongs to March's year, and the flag has usually moved on by then.

    **Overlapping years are legitimate and have a stated tie-break.** A school's session years
    do not overlap, but a tuition programme's batches routinely do - a summer intake running
    inside the main one is normal. When more than one year covers the date, the order is:

        1. the year flagged `is_current`, if it covers the date - the admin's explicit signal
        2. the narrowest range, as the more specific answer
        3. the latest start, purely so the result is deterministic

    Without this the answer depended on Firestore's return order, which meant the same invoice
    could price against different years on two runs.
    """
    wanted = on.isoformat()

    covering = []
    for year in list_years(program):
        start, end = str(year.get("start_date") or ""), str(year.get("end_date") or "")
        if start and end and start <= wanted <= end:
            covering.append((year, start, end))

    if not covering:
        return None

    def _span(entry) -> int:
        _, start, end = entry
        try:
            return (date.fromisoformat(end) - date.fromisoformat(start)).days
        except ValueError:
            # Unparseable dates sort as the widest possible range, so a corrupted record
            # never wins the tie-break against a well-formed one.
            return 10 ** 6

    covering.sort(key=lambda e: (
        not e[0].get("is_current"),   # flagged current first
        _span(e),                     # then narrowest
        # Latest start last, negated via reverse-friendly string comparison below.
    ))
    # Among equals on the first two keys, the latest start wins. Applied as a stable
    # secondary pass so the two ascending keys above keep their meaning.
    best_rank = (not covering[0][0].get("is_current"), _span(covering[0]))
    tied = [e for e in covering if (not e[0].get("is_current"), _span(e)) == best_rank]
    tied.sort(key=lambda e: e[1], reverse=True)
    return tied[0][0]


def create_year(payload, actor_id: int | None = None) -> dict:
    programs = _programs(payload.programs)
    _assert_name_free(payload.name, programs)

    start = _as_date(getattr(payload, "start_date", None))
    end = _as_date(getattr(payload, "end_date", None))
    if not start or not end:
        # "2026-27" is enough to say when the year runs: the April-March calendar is the
        # school's, and asking for the dates every time is how one gets typed as 2026-03-31.
        start, end = default_year_window(payload.name)

    given_terms = getattr(payload, "terms", None)
    terms = _validate_terms(given_terms, start, end) if given_terms else default_terms(start, end)

    year_id = firestore_academic_years.get_next_numeric_id()
    document = {
        "name": payload.name.strip(),
        "code": (payload.code or "").strip() or None,
        "start_date": _iso(start),
        "end_date": _iso(end),
        "terms": terms,
        "status": getattr(payload.status, "value", payload.status),
        "is_current": bool(payload.is_current),
        "admissions_open": bool(payload.admissions_open),
        "programs": programs,
        "notes": payload.notes,
        "created_by": actor_id,
        "created_at": _now(),
    }

    if document["is_current"]:
        _make_current(year_id, programs)

    firestore_academic_years.add_document(str(year_id), document)
    document["id"] = year_id
    logger.info("Created academic year %s (%s).", year_id, document["name"])

    # A school year gets its two-term instalment plan the moment it exists, so the terms
    # are visible under Instalment plans rather than implied by the biller.
    if Program.LMS.value in programs:
        try:
            from app.services import finance as finance_service

            finance_service.ensure_default_plan(document, Program.LMS.value, actor_id)
        except Exception as exc:  # pragma: no cover - never fail the year for its plan
            logger.warning("Default instalment plan not created for year %s: %s", year_id, exc)
    return document


def update_year(year: dict, payload, actor_id: int | None = None) -> dict:
    updates = payload.model_dump(exclude_unset=True, exclude_none=True)

    # The program list may itself be part of this update, so both checks run against the
    # set the year will have afterwards - not the one it had before.
    effective_programs = (
        _programs(updates["programs"]) if "programs" in updates
        else _programs(year.get("programs"))
    )

    if "name" in updates:
        _assert_name_free(updates["name"], effective_programs, exclude_id=year["id"])
    for field in ("start_date", "end_date"):
        if field in updates:
            updates[field] = _iso(updates[field])
    if "status" in updates:
        updates["status"] = getattr(updates["status"], "value", updates["status"])
    if "programs" in updates:
        updates["programs"] = _programs(updates["programs"])

    # Both dates matter to the comparison, and only one of them may be in the payload, so
    # the stored value stands in for whichever was left out.
    start = updates.get("start_date", year.get("start_date"))
    end = updates.get("end_date", year.get("end_date"))
    if start and end and str(end) <= str(start):
        raise HTTPException(status_code=400, detail="end_date must fall after start_date")

    # Terms follow the dates. Given explicitly they are checked against the (possibly new)
    # window; moved dates with no terms given re-cut the year, because term dates that fall
    # outside the year would bill classes into the wrong session.
    if updates.get("terms"):
        updates["terms"] = _validate_terms(updates["terms"], _as_date(start), _as_date(end))
    elif "terms" in updates:
        updates.pop("terms")
    elif "start_date" in updates or "end_date" in updates:
        updates["terms"] = default_terms(_as_date(start), _as_date(end))

    if updates.get("is_current"):
        _make_current(year["id"], effective_programs)

    updates["updated_at"] = _now()
    updates["updated_by"] = actor_id
    firestore_academic_years.add_document(str(year["id"]), updates)
    return {**year, **updates}


def students_in_year(year_id) -> list[dict]:
    return firestore_users.query_documents("academic_year_id", "==", int(year_id))


def delete_year(year: dict) -> None:
    """Refuses while anything still points at the year. See the module docstring."""
    students = students_in_year(year["id"])
    if students:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{len(students)} student(s) are admitted into '{year['name']}'. "
                "Move them to another year before deleting it."
            ),
        )

    categories = firestore_admission_categories.query_documents(
        "academic_year_id", "==", int(year["id"])
    )
    if categories:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{len(categories)} admission category/categories are scoped to "
                f"'{year['name']}'. Delete or re-scope them first."
            ),
        )

    firestore_academic_years.delete_document(str(year["id"]))
    logger.info("Deleted academic year %s (%s).", year["id"], year.get("name"))


def present_year(year: dict, with_counts: bool = False) -> dict:
    terms = terms_of(year)
    today = date.today().isoformat()
    view = {
        "id": int(year["id"]),
        "name": year.get("name"),
        "code": year.get("code"),
        "start_date": year.get("start_date"),
        "end_date": year.get("end_date"),
        "status": year.get("status") or AcademicYearStatus.UPCOMING.value,
        "is_current": bool(year.get("is_current")),
        "admissions_open": bool(year.get("admissions_open", True)),
        "programs": _programs(year.get("programs")),
        "terms": [
            {**term, "is_current": term["start_date"] <= today <= term["end_date"]}
            for term in terms
        ],
        "current_term": next(
            (t["key"] for t in terms if t["start_date"] <= today <= t["end_date"]), None
        ),
        "notes": year.get("notes"),
        "created_at": year.get("created_at"),
        "updated_at": year.get("updated_at"),
    }
    if with_counts:
        view["student_count"] = len(students_in_year(year["id"]))
        view["category_count"] = len(
            firestore_admission_categories.query_documents(
                "academic_year_id", "==", int(year["id"])
            )
        )
    return view


# ---------------------------------------------------------------------------------------
# Admission categories
# ---------------------------------------------------------------------------------------

def list_categories(program: str | None = None, academic_year_id=None,
                    include_inactive: bool = False) -> list[dict]:
    """
    Categories applying to a year.

    A standing category (no `academic_year_id`) is included in every year's list. That is the
    whole point of the null: "Regular" and "Staff Ward" are defined once, not re-created every
    July, and a school that never scopes a category to a year never has to think about the
    field at all.
    """
    categories = firestore_admission_categories.list_all()

    if program:
        wanted = str(program).upper()
        categories = [c for c in categories if wanted in _programs(c.get("programs"))]
    if academic_year_id is not None:
        wanted_year = int(academic_year_id)
        categories = [
            c for c in categories
            if c.get("academic_year_id") in (None, "", wanted_year)
        ]
    if not include_inactive:
        categories = [c for c in categories if c.get("is_active", True)]

    return sorted(
        categories,
        key=lambda c: (int(c.get("sort_order") or 0), str(c.get("name") or "")),
    )


def require_category(category_id) -> dict:
    return require_document(firestore_admission_categories, category_id, "Admission category")


def _assert_code_free(code: str, exclude_id=None) -> None:
    lowered = str(code).strip().lower()
    for category in firestore_admission_categories.list_all():
        if str(category.get("code", "")).strip().lower() != lowered:
            continue
        if exclude_id is not None and str(category.get("id")) == str(exclude_id):
            continue
        raise HTTPException(
            status_code=400,
            detail=f"An admission category with code '{code}' already exists "
                   f"(id {category['id']}).",
        )


def create_category(payload, actor_id: int | None = None) -> dict:
    _assert_code_free(payload.code)
    if payload.academic_year_id is not None:
        require_year(payload.academic_year_id)

    category_id = firestore_admission_categories.get_next_numeric_id()
    document = {
        "name": payload.name.strip(),
        "code": payload.code.strip().upper(),
        "description": payload.description,
        "academic_year_id": (
            int(payload.academic_year_id) if payload.academic_year_id is not None else None
        ),
        "programs": _programs(payload.programs),
        "default_discount_percent": payload.default_discount_percent,
        "waives_admission_charge": bool(payload.waives_admission_charge),
        "is_active": bool(payload.is_active),
        "sort_order": int(payload.sort_order or 0),
        "created_by": actor_id,
        "created_at": _now(),
    }
    firestore_admission_categories.add_document(str(category_id), document)
    document["id"] = category_id
    logger.info("Created admission category %s (%s).", category_id, document["code"])
    return document


def update_category(category: dict, payload, actor_id: int | None = None) -> dict:
    updates = payload.model_dump(exclude_unset=True, exclude_none=True)

    if "code" in updates:
        _assert_code_free(updates["code"], exclude_id=category["id"])
        updates["code"] = updates["code"].strip().upper()
    if "academic_year_id" in updates and updates["academic_year_id"] is not None:
        require_year(updates["academic_year_id"])
        updates["academic_year_id"] = int(updates["academic_year_id"])
    if "programs" in updates:
        updates["programs"] = _programs(updates["programs"])

    updates["updated_at"] = _now()
    updates["updated_by"] = actor_id
    firestore_admission_categories.add_document(str(category["id"]), updates)
    return {**category, **updates}


def students_in_category(category_id) -> list[dict]:
    return firestore_users.query_documents("admission_category_id", "==", int(category_id))


def delete_category(category: dict) -> None:
    """
    Refuses while students hold the category.

    Deactivating is the answer for a category the school has stopped offering: it disappears
    from the admission form's dropdown while every student already admitted under it keeps a
    resolvable record of why they pay what they pay.
    """
    students = students_in_category(category["id"])
    if students:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{len(students)} student(s) were admitted under '{category['name']}'. "
                "Deactivate it instead of deleting, so their records stay resolvable."
            ),
        )
    firestore_admission_categories.delete_document(str(category["id"]))
    logger.info("Deleted admission category %s (%s).", category["id"], category.get("code"))


def present_category(category: dict, with_counts: bool = False) -> dict:
    year_id = category.get("academic_year_id")
    year_name = None
    if year_id is not None:
        year = firestore_academic_years.get_document(str(year_id))
        year_name = (year or {}).get("name")

    view = {
        "id": int(category["id"]),
        "name": category.get("name"),
        "code": category.get("code"),
        "description": category.get("description"),
        "academic_year_id": int(year_id) if year_id is not None else None,
        "academic_year_name": year_name,
        "programs": _programs(category.get("programs")),
        "default_discount_percent": category.get("default_discount_percent"),
        "waives_admission_charge": bool(category.get("waives_admission_charge")),
        "is_active": bool(category.get("is_active", True)),
        "sort_order": int(category.get("sort_order") or 0),
        "created_at": category.get("created_at"),
        "updated_at": category.get("updated_at"),
    }
    if with_counts:
        view["student_count"] = len(students_in_category(category["id"]))
    return view


def assert_admission_fields(academic_year_id, admission_category_id) -> None:
    """
    Validates the two admission references on a user payload.

    Called from the account routes rather than enforced in the schema because the ids have to
    be checked against the database, and because both are optional: a school that has not set
    up admissions at all must still be able to create students.
    """
    if academic_year_id is not None:
        require_year(academic_year_id)
    if admission_category_id is not None:
        require_category(admission_category_id)


# ---------------------------------------------------------------------------------------
# Mapping students into a year
#
# Two operations, and the difference between them matters.
#
# `assign_students` is the flat one: put these students in this year. It is what you use when
# importing an existing school for the first time, or correcting a handful of records.
#
# `promote` is the July one: take last year's students, move them up a class, and file them
# under the new year. It has to touch two things per student - the `academic_year_id` on the
# profile and the row in `student_enrollments` that says which class they sit in - and doing
# only the first is the bug that makes a whole school look enrolled in last year's classes.
#
# Both are previewable. A rollover touches every student in the school, and an admin who
# cannot see what it will do before it does it will not press the button - or worse, will
# press it and then need it undone.
# ---------------------------------------------------------------------------------------

def _natural_key(text: str) -> list:
    """
    Sort key that reads runs of digits as numbers.

    Plain string ordering puts "Class 10" before "Class 7", which is wrong on every roster a
    school will ever print. Splitting on digit runs and comparing the numeric parts as
    integers fixes that without needing class records to carry an explicit order field, and
    it degrades sensibly for names with no digits at all ("Lower Kindergarten").

    The leading `0`/`1` in each pair keeps ints and strings from being compared to each
    other, which Python 3 refuses to do.
    """
    import re

    parts = re.split(r"(\d+)", str(text or ""))
    return [(0, int(p)) if p.isdigit() else (1, p.lower()) for p in parts if p != ""]


def _roster_sort_key(row: dict) -> tuple:
    """Class first (naturally ordered), then student name. Unplaced students sort last."""
    return (
        row.get("class_name") is None,
        _natural_key(row.get("class_name") or ""),
        str(row.get("full_name") or "").lower(),
    )


def _class_of_student(student_id) -> tuple[int | None, dict | None]:
    """The student's current class id and the enrollment row that says so."""
    rows = firestore_student_enrollments.query_documents("student_id", "==", int(student_id))
    if not rows:
        return None, None
    row = rows[0]
    class_id = row.get("class_id")
    return (int(class_id) if class_id is not None else None), row


def students_in_year_detailed(year_id, class_id=None,
                              include_inactive: bool = False,
                              program: str | None = None) -> list[dict]:
    """
    The roster: who is mapped into this year, with their class and category resolved.

    Sorted by class then name, which is the order a roster is read in and checked against a
    paper list. `program` narrows to accounts with that product access - the tuition screen
    passes TUITION so a year shared by both products does not show the school's pupils on the
    tuition roster.
    """
    from app.core.firebase import firestore_classes

    rows = []
    for student in students_in_year(year_id):
        if not include_inactive and not student.get("is_active", True):
            continue
        if program and not user_in_program(student, program):
            continue

        enrolled_class, _ = _class_of_student(student["id"])
        if class_id is not None and enrolled_class != int(class_id):
            continue

        class_name = None
        if enrolled_class is not None:
            class_name = (firestore_classes.get_document(str(enrolled_class)) or {}).get("name")

        category_name = None
        if student.get("admission_category_id") is not None:
            category_name = (
                firestore_admission_categories.get_document(
                    str(student["admission_category_id"])
                ) or {}
            ).get("name")

        rows.append({
            "student_id": int(student["id"]),
            "full_name": student.get("full_name"),
            "email": student.get("email"),
            "admission_number": student.get("admission_number"),
            "roll_number": student.get("roll_number"),
            "class_id": enrolled_class,
            "class_name": class_name,
            "admission_category_id": student.get("admission_category_id"),
            "admission_category_name": category_name,
            "syllabus": student.get("syllabus"),
            "is_active": bool(student.get("is_active", True)),
        })

    rows.sort(key=_roster_sort_key)
    return rows


def unassigned_students(include_inactive: bool = False,
                        program: str | None = None) -> list[dict]:
    """
    Students carrying no `academic_year_id` at all.

    Every account created before this module existed is in here, which is exactly who a
    school needs to see first when they open the admissions screen. Returned as its own
    query rather than as a filter on the roster, because "who have we not mapped yet?" is
    the question, and a filter that returns everybody by default answers it badly.

    `program` narrows to one product's accounts, so the tuition screen is not asked to map
    the school's pupils into a tuition batch.
    """
    from app.core.firebase import firestore_classes

    rows = []
    for student in firestore_users.query_documents("role", "==", UserRole.STUDENT.value):
        if student.get("academic_year_id") is not None:
            continue
        if not include_inactive and not student.get("is_active", True):
            continue
        if program and not user_in_program(student, program):
            continue

        enrolled_class, _ = _class_of_student(student["id"])
        class_name = None
        if enrolled_class is not None:
            class_name = (firestore_classes.get_document(str(enrolled_class)) or {}).get("name")

        rows.append({
            "student_id": int(student["id"]),
            "full_name": student.get("full_name"),
            "email": student.get("email"),
            "admission_number": student.get("admission_number"),
            "class_id": enrolled_class,
            "class_name": class_name,
            "is_active": bool(student.get("is_active", True)),
        })

    rows.sort(key=_roster_sort_key)
    return rows


def assign_students(year_id, student_ids: list[int], actor_id: int | None = None,
                    dry_run: bool = False) -> dict:
    """
    Maps a list of students into a year.

    Every id is validated before anything is written, so a typo in one of three hundred does
    not leave two hundred of them moved and the rest not. Ids that are not students, or do
    not exist, are reported as skipped rather than failing the whole call - a roster pasted
    out of a spreadsheet nearly always has one bad row, and refusing all of it teaches people
    to stop using the import.
    """
    year = require_year(year_id)

    moved, skipped, unchanged = [], [], []
    for student_id in student_ids:
        student = firestore_users.get_document(str(student_id))
        if not student:
            skipped.append({"student_id": student_id, "reason": "no such user"})
            continue
        if student.get("role") != UserRole.STUDENT.value:
            skipped.append({
                "student_id": student_id,
                "reason": f"is a {student.get('role')}, not a student",
            })
            continue
        if str(student.get("academic_year_id")) == str(year["id"]):
            unchanged.append(int(student_id))
            continue

        moved.append({
            "student_id": int(student_id),
            "full_name": student.get("full_name"),
            "from_year_id": student.get("academic_year_id"),
            "_admission_year_id": student.get("admission_year_id"),
        })

    if not dry_run:
        for row in moved:
            updates = {
                "academic_year_id": int(year["id"]),
                "updated_at": _now(),
                "updated_by": actor_id,
            }
            # The admission year is fixed the first time a student is placed anywhere: the
            # year they came from if they had one, else this one. Never overwritten.
            if not row.get("_admission_year_id"):
                updates["admission_year_id"] = int(row.get("from_year_id") or year["id"])
            firestore_users.add_document(str(row["student_id"]), updates)
        logger.info("Mapped %s student(s) into academic year %s (%s).",
                    len(moved), year["id"], year.get("name"))

    return {
        "dry_run": dry_run,
        "academic_year_id": int(year["id"]),
        "academic_year_name": year.get("name"),
        "moved": moved,
        "moved_count": len(moved),
        "already_in_year": unchanged,
        "skipped": skipped,
        "detail": (
            f"{'Would map' if dry_run else 'Mapped'} {len(moved)} student(s) into "
            f"'{year.get('name')}'. {len(unchanged)} already there, {len(skipped)} skipped."
        ),
    }


def promote(source_year_id, target_year_id, class_map: dict[int, int] | None = None,
            graduating_class_ids: list[int] | None = None,
            actor_id: int | None = None, dry_run: bool = True) -> dict:
    """
    The July rollover: move a year's students into the next one, up a class.

    `class_map` is `{from_class_id: to_class_id}`. A student whose class is not in the map
    keeps the class they are in - which is the right default for a school that re-uses class
    records year on year rather than creating new ones, and means a partial map is safe.

    `graduating_class_ids` names the classes that have no next year. Those students are
    *not* moved into the target year and are not deactivated either: leaving is an
    administrative act with fee and record consequences, and a rollover quietly closing
    thirty accounts is not something anybody would find until a parent could not log in.
    They are listed as `graduating` for the admin to deal with deliberately.

    **Defaults to a dry run.** A rollover touches every student in the school; the caller has
    to ask for it to actually happen. See the section docstring.
    """
    source = require_year(source_year_id)
    target = require_year(target_year_id)

    if str(source["id"]) == str(target["id"]):
        raise HTTPException(
            status_code=400,
            detail="The source and target year are the same. Pick the year to promote into.",
        )

    class_map = {int(k): int(v) for k, v in (class_map or {}).items()}
    graduating = {int(c) for c in (graduating_class_ids or [])}

    # Every target class is checked once, up front. A rollover that half-completed because
    # the twelfth class id was a typo is the worst possible outcome here.
    from app.core.firebase import firestore_classes
    for from_class, to_class in class_map.items():
        require_document(firestore_classes, from_class, "Source class")
        require_document(firestore_classes, to_class, "Target class")

    promoted, graduating_rows, unchanged_class = [], [], []

    for student in students_in_year(source["id"]):
        if not student.get("is_active", True):
            continue

        current_class, enrollment_row = _class_of_student(student["id"])

        if current_class is not None and current_class in graduating:
            graduating_rows.append({
                "student_id": int(student["id"]),
                "full_name": student.get("full_name"),
                "class_id": current_class,
            })
            continue

        new_class = class_map.get(current_class) if current_class is not None else None
        if new_class is None and current_class is not None:
            unchanged_class.append(int(student["id"]))

        promoted.append({
            "student_id": int(student["id"]),
            "full_name": student.get("full_name"),
            "admission_number": student.get("admission_number"),
            "from_class_id": current_class,
            "to_class_id": new_class if new_class is not None else current_class,
            "class_changed": new_class is not None and new_class != current_class,
            "_enrollment_id": (enrollment_row or {}).get("id"),
            "_admission_year_id": student.get("admission_year_id"),
        })

    if not dry_run:
        for row in promoted:
            updates = {
                "academic_year_id": int(target["id"]),
                "updated_at": _now(),
                "updated_by": actor_id,
            }
            # A promoted student was admitted in the year they are leaving, at the latest.
            # Fixing it here is what keeps the admission charge off next year's bill.
            if not row.get("_admission_year_id"):
                updates["admission_year_id"] = int(source["id"])
            firestore_users.add_document(str(row["student_id"]), updates)

            # The class row is the half that is easy to forget; see the section docstring.
            if row["class_changed"]:
                if row["_enrollment_id"] is not None:
                    firestore_student_enrollments.add_document(
                        str(row["_enrollment_id"]),
                        {"class_id": int(row["to_class_id"]),
                         "enrolled_at": _now()},
                    )
                else:
                    enrollment_id = firestore_student_enrollments.get_next_numeric_id()
                    firestore_student_enrollments.add_document(str(enrollment_id), {
                        "student_id": int(row["student_id"]),
                        "class_id": int(row["to_class_id"]),
                        "enrolled_at": _now(),
                    })

        logger.info("Promoted %s student(s) from year %s to %s.",
                    len(promoted), source["id"], target["id"])

    for row in promoted:
        row.pop("_enrollment_id", None)

    return {
        "dry_run": dry_run,
        "source_year_id": int(source["id"]),
        "source_year_name": source.get("name"),
        "target_year_id": int(target["id"]),
        "target_year_name": target.get("name"),
        "promoted": promoted,
        "promoted_count": len(promoted),
        "class_changed_count": sum(1 for r in promoted if r["class_changed"]),
        "kept_same_class_count": len(unchanged_class),
        "graduating": graduating_rows,
        "graduating_count": len(graduating_rows),
        "detail": (
            f"{'Would promote' if dry_run else 'Promoted'} {len(promoted)} student(s) from "
            f"'{source.get('name')}' to '{target.get('name')}'; "
            f"{sum(1 for r in promoted if r['class_changed'])} change class, "
            f"{len(graduating_rows)} graduating and left where they are."
        ),
    }
