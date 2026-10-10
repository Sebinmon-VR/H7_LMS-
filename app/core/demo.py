"""
Demo accounts: what a prospective family may try before admission, and for how long.

A demo account is an ordinary user with three extra fields - `is_demo`, `demo_expires_on`
and `demo_modules` - so it signs in, joins classes and appears on rosters exactly like a
real one, and becomes a real one by switching the flag off. Everything else follows:

* **Classes are always open.** The point of a demo is to sit a class: the home page, the
  subjects or tutors, the timetable, live classes and the call itself are never locked.
* **Every other module is locked** unless the admin unlocks it on the account. Locking is
  enforced here, on every request (`assert_allowed`, called from `get_current_user`), so a
  locked screen stays locked when somebody types its address; the frontend's padlocks are
  the friendly half of the same rule.
* **The account ends on its expiry date.** It works through `demo_expires_on` (school
  time), is refused from the next morning, and the maintenance sweep then erases it with
  everything it left behind (`app.services.demo_accounts.purge_expired`).

Nothing here touches the database, so the catalogue can be read at import time and from
the schemas' validators.
"""

from datetime import date, datetime

from fastapi import HTTPException, status

LOCKED_DETAIL = (
    "This feature is locked on demo accounts. Ask the school to unlock it, or to complete "
    "the admission for full access."
)
EXPIRED_DETAIL = (
    "This demo account has expired. Please contact the school to continue with the admission."
)

# The lockable modules. `api` lists the path prefixes (after /api/v1) that belong to each;
# a request under one of them from a demo account that has not unlocked it is refused.
# Paths not listed - classes, calls, the account itself, presence - are always open.
MODULES: list[dict] = [
    {"key": "attendance", "label": "Attendance",
     "description": "Attendance record and hours taught.",
     "api": ["/students/attendance", "/tuition/students/reports"]},
    {"key": "syllabus", "label": "What we learned",
     "description": "Topics the class has covered.",
     "api": ["/students/topics"]},
    {"key": "library", "label": "Library",
     "description": "Class videos, notes and books.",
     "api": ["/students/materials", "/tuition/library"]},
    {"key": "exams", "label": "Homework & exams",
     "description": "Homework, assignments, exams and marks.",
     "api": ["/exams", "/students/exams", "/students/grades", "/homework",
             "/tuition/students/assessments"]},
    {"key": "report_cards", "label": "Report cards",
     "description": "Term report cards.",
     "api": ["/report-cards", "/students/report-cards", "/tuition/students/report-cards"]},
    {"key": "games", "label": "Games",
     "description": "Quiz Battle, the Virtual Lab and the other learning games.",
     "api": ["/arena", "/lab", "/tuition/arena", "/tuition/lab", "/tuition/students/arena",
             "/tuition/students/lab"]},
    {"key": "fees", "label": "Fees",
     "description": "Invoices, payments and the fee breakdown.",
     "api": ["/fees", "/tuition/students/fees", "/tuition/students/invoices",
             "/tuition/students/package"]},
    {"key": "notices", "label": "Notices",
     "description": "The school's notice board.",
     "api": ["/notices", "/tuition/notices"]},
    {"key": "calendar", "label": "Calendar",
     "description": "Lessons, classes, exams and homework on one calendar.",
     "api": ["/calendar"]},
    {"key": "support", "label": "Help & support",
     "description": "Raise a support ticket with the school.",
     "api": ["/support"]},
]
MODULE_KEYS = frozenset(m["key"] for m in MODULES)

# Longest prefix first, so "/tuition/students/report-cards" is matched before anything
# shorter that happens to share its start.
_PREFIXES = sorted(
    ((prefix, m["key"]) for m in MODULES for prefix in m["api"]),
    key=lambda pair: len(pair[0]), reverse=True,
)


def module_for_path(path: str, api_prefix: str = "/api/v1") -> str | None:
    """The module an API path belongs to, or None for the always-open ones."""
    if path.startswith(api_prefix):
        path = path[len(api_prefix):]
    for prefix, key in _PREFIXES:
        if path == prefix or path.startswith(prefix + "/"):
            return key
    return None


def clean_modules(values) -> list[str]:
    """Known module keys only, in catalogue order. An unknown key is a 400, not dropped."""
    wanted = [str(v).strip() for v in (values or []) if str(v).strip()]
    unknown = [v for v in wanted if v not in MODULE_KEYS]
    if unknown:
        raise ValueError(f"Unknown demo module(s): {', '.join(unknown)}")
    return [m["key"] for m in MODULES if m["key"] in wanted]


def _today() -> date:
    try:
        from app.services.timetable import school_timezone

        return datetime.now(school_timezone()).date()
    except Exception:  # pragma: no cover - the clock must not depend on the database
        return date.today()


def expires_on(user_document: dict) -> date | None:
    value = user_document.get("demo_expires_on")
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def is_expired(user_document: dict, today: date | None = None) -> bool:
    """A demo account works through its expiry date and is expired from the next day."""
    if not user_document.get("is_demo"):
        return False
    ends = expires_on(user_document)
    return ends is not None and (today or _today()) > ends


def assert_not_expired(user_document: dict) -> None:
    if is_expired(user_document):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=EXPIRED_DETAIL)


def assert_allowed(user_document: dict, path: str) -> None:
    """Refuses an expired demo account outright, and a locked module's paths."""
    if not user_document.get("is_demo"):
        return
    assert_not_expired(user_document)
    module = module_for_path(path)
    if module and module not in set(user_document.get("demo_modules") or []):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=LOCKED_DETAIL)


def catalogue() -> list[dict]:
    """What the admin's demo checklist offers. Classes are listed as always included."""
    return [{"key": m["key"], "label": m["label"], "description": m["description"]} for m in MODULES]
