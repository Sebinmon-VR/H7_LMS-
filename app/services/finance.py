"""
School fees: resolving what a student is charged, and billing it.

`compute_breakdown` is the function everything else here exists to serve. It takes a student
and a year and returns the full derivation of what they owe - every head, the concessions
applied to it, the tax computed on what is left, the charges added afterwards, and the
instalments it is collected in. The payment page renders it, the invoice generator freezes
it, and the parent's fee screen reads it. One function, because a preview that disagrees with
the invoice is worse than having no preview at all.

**The order of the arithmetic is the design.** It runs:

    1. lines      - the fee heads from the resolved structure
    2. discounts  - applied per head, only to heads marked discountable
    3. tax        - computed on the *discounted* taxable base, never the gross
    4. charges    - non-fee extras, which are not themselves discounted
    5. late fees  - on overdue instalments, never taxed
    6. rounding   - on the final payable only

Every one of those steps is somewhere a school could reasonably disagree, so each is stated
once here rather than being implied by whatever order the code happened to run in. Taxing the
gross and then discounting produces a different, larger number, and both are defensible - but
only one of them can be what the school actually charges.

**Discount stacking** is "best single rule, plus any explicitly stackable ones". A student who
is a staff ward, a second child and an early payer would otherwise combine three concessions
into a negative bill. The safe default is that concessions compete; stacking is a decision an
admin makes per rule.

**Rounding happens once.** Rounding each line would leave the lines not summing to the total,
which is the first thing anybody checking a bill notices.
"""

import logging
from datetime import date, datetime, timedelta
from typing import Any

from fastapi import HTTPException

from app.core.enums import (
    ChargeKind, DiscountBasis, DiscountValueType, FeeFrequency, InstalmentStatus, InvoiceStatus,
    PaymentIntentStatus, Program, TERMINAL_INTENT_STATES, UserRole,
)
from app.core.firebase import (
    firestore_classes, firestore_discount_rules, firestore_fee_heads,
    firestore_fee_invoices, firestore_fee_structures, firestore_instalment_plans,
    firestore_payment_intents, firestore_student_enrollments,
    firestore_tuition_enrollments, firestore_users, require_document,
)
from app.services import admissions as admission_service
from app.services import families as family_service
from app.services.tuition.settings_store import (
    currency_settings, finance_settings, resolve_currency,
)

logger = logging.getLogger("finance")


def _now() -> str:
    return datetime.utcnow().isoformat()


def money(value: Any) -> float:
    """
    Two decimal places, every time.

    Applied at each step rather than only at the end, because a chain of unrounded floats
    produces totals like 12000.000000000002, and a bill showing that has lost the parent's
    confidence before they have read the rest of it.
    """
    try:
        return round(float(value or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def _as_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _apply_rounding(amount: float, mode: str) -> float:
    """Rounds the final payable. See the module docstring for why only the final one."""
    if mode == "NEAREST":
        return float(round(amount))
    if mode == "UP":
        import math
        return float(math.ceil(amount))
    if mode == "DOWN":
        import math
        return float(math.floor(amount))
    return money(amount)


# ---------------------------------------------------------------------------------------
# Fee heads
# ---------------------------------------------------------------------------------------

def require_head(head_id) -> dict:
    return require_document(firestore_fee_heads, head_id, "Fee head")


def list_heads(program: str | None = None, include_inactive: bool = False) -> list[dict]:
    heads = firestore_fee_heads.list_all()
    if program:
        wanted = str(program).upper()
        heads = [h for h in heads if (h.get("program") or Program.LMS.value) == wanted]
    if not include_inactive:
        heads = [h for h in heads if h.get("is_active", True)]
    return sorted(heads, key=lambda h: (int(h.get("sort_order") or 0), str(h.get("name") or "")))


def _assert_head_code_free(code: str, program: str, exclude_id=None) -> None:
    lowered = str(code).strip().lower()
    for head in firestore_fee_heads.list_all():
        if str(head.get("code", "")).strip().lower() != lowered:
            continue
        if (head.get("program") or Program.LMS.value) != str(program).upper():
            continue
        if exclude_id is not None and str(head.get("id")) == str(exclude_id):
            continue
        raise HTTPException(
            status_code=400,
            detail=f"A fee head with code '{code}' already exists for {program} "
                   f"(id {head['id']}).",
        )


def create_head(payload, actor_id: int | None = None) -> dict:
    program = getattr(payload.program, "value", payload.program)
    _assert_head_code_free(payload.code, program)

    head_id = firestore_fee_heads.get_next_numeric_id()
    document = {
        "name": payload.name.strip(),
        "code": payload.code.strip().upper(),
        "kind": getattr(payload.kind, "value", payload.kind),
        "frequency": getattr(payload.frequency, "value", payload.frequency),
        "default_amount": money(payload.default_amount),
        "is_taxable": bool(payload.is_taxable),
        "tax_percent": payload.tax_percent,
        "is_discountable": bool(payload.is_discountable),
        "is_admission_charge": bool(payload.is_admission_charge),
        "program": program,
        "description": payload.description,
        "is_active": bool(payload.is_active),
        "sort_order": int(payload.sort_order or 0),
        "created_by": actor_id,
        "created_at": _now(),
    }
    firestore_fee_heads.add_document(str(head_id), document)
    document["id"] = head_id
    return document


def update_head(head: dict, payload, actor_id: int | None = None) -> dict:
    updates = payload.model_dump(exclude_unset=True, exclude_none=True)
    if "code" in updates:
        _assert_head_code_free(
            updates["code"], head.get("program") or Program.LMS.value, exclude_id=head["id"]
        )
        updates["code"] = updates["code"].strip().upper()
    for field in ("kind", "frequency", "program"):
        if field in updates:
            updates[field] = getattr(updates[field], "value", updates[field])
    if "default_amount" in updates:
        updates["default_amount"] = money(updates["default_amount"])

    updates["updated_at"] = _now()
    updates["updated_by"] = actor_id
    firestore_fee_heads.add_document(str(head["id"]), updates)
    return {**head, **updates}


def delete_head(head: dict) -> None:
    """
    Refuses while any structure still charges the head.

    The structures would keep the id and the id would resolve to nothing, so every bill built
    from them would quietly drop a line. Deactivating removes it from the editor's picker
    while leaving existing structures intact.
    """
    users_of = [
        s for s in firestore_fee_structures.list_all()
        if any(str(i.get("fee_head_id")) == str(head["id"]) for i in s.get("items") or [])
    ]
    if users_of:
        raise HTTPException(
            status_code=400,
            detail=f"{len(users_of)} fee structure(s) charge '{head['name']}'. "
                   "Remove it from them, or deactivate the head instead.",
        )
    firestore_fee_heads.delete_document(str(head["id"]))


# ---------------------------------------------------------------------------------------
# Structures and plans
# ---------------------------------------------------------------------------------------

def require_structure(structure_id) -> dict:
    return require_document(firestore_fee_structures, structure_id, "Fee structure")


def require_plan(plan_id) -> dict:
    return require_document(firestore_instalment_plans, plan_id, "Instalment plan")


def _scope_matches(record: dict, class_id, category_id) -> bool:
    """
    Whether a structure or plan applies to this student.

    An empty list means "any", which is what makes a single default structure enough for a
    school that charges everybody the same. A non-empty list that does not contain the
    student's value excludes them outright.
    """
    class_ids = record.get("class_ids") or []
    if class_ids and (class_id is None or int(class_id) not in [int(c) for c in class_ids]):
        return False

    category_ids = record.get("admission_category_ids") or []
    if category_ids and (
        category_id is None or int(category_id) not in [int(c) for c in category_ids]
    ):
        return False
    return True


def _specificity(record: dict) -> int:
    """
    How narrowly a record is scoped. Higher beats lower.

    Naming both a class and a category is more specific than naming either, which is more
    specific than the year default. This is what lets a school set one structure for
    everybody and then override it for the scholarship intake without deleting anything.
    """
    return (2 if record.get("class_ids") else 0) + (1 if record.get("admission_category_ids") else 0)


def _student_class_id(student_id) -> int | None:
    enrollments = firestore_student_enrollments.query_documents(
        "student_id", "==", int(student_id)
    )
    return int(enrollments[0]["class_id"]) if enrollments and enrollments[0].get("class_id") else None


def why_structure_missed(structure: dict, student: dict, academic_year_id,
                         class_id, program: str) -> list[str]:
    """
    Why one structure does not apply to this student, in words an administrator can act on.

    The counterpart to `discounts_not_applied`, and it exists for the same reason: "no fee
    structure applies to this student" is a dead end. It does not say whether the structure
    is for another class, another year, or whether the student is simply missing the field
    being matched on - and the last of those is by far the most common, because every account
    created before the admissions module existed carries no `academic_year_id` at all.
    """
    reasons = []
    category_id = student.get("admission_category_id")

    if not structure.get("is_active", True):
        reasons.append("The structure is inactive.")

    structure_program = structure.get("program") or Program.LMS.value
    if structure_program != str(program).upper():
        reasons.append(
            f"The structure is for {structure_program}, not {str(program).upper()}."
        )

    if str(structure.get("academic_year_id")) != str(academic_year_id):
        if academic_year_id is None:
            reasons.append(
                "This student has no academic year set, so no year-scoped structure can "
                "match. Assign them to a year under Admissions."
            )
        else:
            reasons.append(
                f"The structure is for a different academic year "
                f"(structure: {structure.get('academic_year_id')}, student: {academic_year_id})."
            )

    class_ids = [int(c) for c in structure.get("class_ids") or []]
    if class_ids and (class_id is None or int(class_id) not in class_ids):
        reasons.append(
            "This student is not enrolled in any class, and the structure is scoped to "
            "specific classes." if class_id is None else
            f"The structure is scoped to other classes (student is in class {class_id})."
        )

    category_ids = [int(c) for c in structure.get("admission_category_ids") or []]
    if category_ids and (category_id is None or int(category_id) not in category_ids):
        reasons.append(
            "This student has no admission category set, and the structure is scoped to "
            "specific categories. Set their category, or widen the structure."
            if category_id is None else
            f"The structure is scoped to other admission categories "
            f"(student's category is {category_id})."
        )

    return reasons


def resolve_structure(student: dict, academic_year_id, program: str = Program.LMS.value) -> dict | None:
    """
    The fee structure that applies to one student. Most specific wins; see `_specificity`.

    Returns None when nothing matches, which is a normal state for a school mid-setup rather
    than an error - the caller shows "no fee structure configured" instead of a 500. When it
    returns None the caller should also read `why_structure_missed` for each structure, so the
    screen can say *why* rather than leaving an administrator to guess.
    """
    class_id = _student_class_id(student["id"])
    category_id = student.get("admission_category_id")

    candidates = [
        s for s in firestore_fee_structures.list_all()
        if s.get("is_active", True)
        and (s.get("program") or Program.LMS.value) == str(program).upper()
        and str(s.get("academic_year_id")) == str(academic_year_id)
        and _scope_matches(s, class_id, category_id)
    ]
    if not candidates:
        return None

    # Ties broken by priority, then by the newer document. Arbitrary but stated - better
    # than whichever Firestore happened to return first.
    candidates.sort(
        key=lambda s: (_specificity(s), int(s.get("priority") or 0), int(s.get("id") or 0)),
        reverse=True,
    )
    return candidates[0]


def resolve_plan(student: dict, structure: dict | None, academic_year_id,
                 program: str = Program.LMS.value) -> dict | None:
    """
    The instalment schedule for a student.

    A plan naming this structure explicitly wins over one matched only by class or category,
    because "this is how *these fees* are collected" is a stronger statement than "this is how
    this class usually pays".
    """
    class_id = _student_class_id(student["id"])
    category_id = student.get("admission_category_id")

    candidates = [
        p for p in firestore_instalment_plans.list_all()
        if p.get("is_active", True)
        and (p.get("program") or Program.LMS.value) == str(program).upper()
        and str(p.get("academic_year_id")) == str(academic_year_id)
        and _scope_matches(p, class_id, category_id)
        and (
            p.get("structure_id") is None
            or structure is None
            or str(p.get("structure_id")) == str(structure.get("id"))
        )
    ]
    if not candidates:
        return None

    candidates.sort(
        key=lambda p: (
            1 if (structure and str(p.get("structure_id")) == str(structure.get("id"))) else 0,
            _specificity(p),
            1 if p.get("is_default") else 0,
            int(p.get("id") or 0),
        ),
        reverse=True,
    )
    return candidates[0]


def validate_instalments(instalments: list[dict]) -> None:
    """
    Checks a plan's lines before it is saved.

    Percentages must sum to 100. An unvalidated plan under-bills silently, and nobody
    notices until the year is over and the money is not there - which is the single most
    expensive bug this module could have.

    A plan may use fixed amounts instead, in which case percentages are absent and no sum
    check is possible; mixing the two is refused because there is no sane reading of "40% and
    5,000" that does not depend on which is applied first.
    """
    if not instalments:
        raise HTTPException(status_code=400, detail="An instalment plan needs at least one line.")

    with_percent = [i for i in instalments if i.get("percent") is not None]
    with_amount = [i for i in instalments if i.get("amount") is not None]

    if with_percent and with_amount:
        raise HTTPException(
            status_code=400,
            detail="Use either percentages or fixed amounts across all instalments, not both. "
                   "Mixing them has no single correct order of application.",
        )
    if not with_percent and not with_amount:
        raise HTTPException(
            status_code=400,
            detail="Each instalment needs either a 'percent' or an 'amount'.",
        )

    if with_percent:
        total = sum(float(i.get("percent") or 0) for i in with_percent)
        if abs(total - 100.0) > 0.01:
            raise HTTPException(
                status_code=400,
                detail=f"Instalment percentages total {total:g}%, not 100%. "
                       "A plan that does not total 100% under- or over-bills every student on it.",
            )


# ---------------------------------------------------------------------------------------
# Discounts
# ---------------------------------------------------------------------------------------

def _rule_is_current(rule: dict, on: date) -> bool:
    valid_from = _as_date(rule.get("valid_from"))
    valid_until = _as_date(rule.get("valid_until"))
    if valid_from and on < valid_from:
        return False
    if valid_until and on > valid_until:
        return False
    return True


def concurrent_enrollment_count(student_id) -> int:
    """
    How many subjects a student is currently taking, across both products.

    The count a MULTI_REGISTRATION concession is priced from. Tuition enrollments are counted
    because that is where the brief's "same time 2 Reg." case actually occurs - a student
    taking maths and physics privately - and the school's own class enrollment is counted as
    one so a school-plus-tuition student reaches two.
    """
    school = len(firestore_student_enrollments.query_documents("student_id", "==", int(student_id)))
    tuition = len([
        e for e in firestore_tuition_enrollments.query_documents("student_id", "==", int(student_id))
        if e.get("status") == "ACTIVE"
    ])
    return school + tuition


def _rule_applies(rule: dict, student: dict, context: dict) -> tuple[bool, str]:
    """
    Whether one rule fires for this student, and why not when it does not.

    The reason string is returned rather than logged because it is shown in the fee preview:
    an admin asking "why is this family not getting the sibling discount?" gets "household has
    1 child; rule needs 2" instead of silence.
    """
    basis = rule.get("basis")
    nth = int(rule.get("applies_from_nth") or 2)

    if rule.get("class_ids"):
        class_id = context.get("class_id")
        if class_id is None or int(class_id) not in [int(c) for c in rule["class_ids"]]:
            return False, "not scoped to this student's class"

    if rule.get("admission_category_ids"):
        category_id = student.get("admission_category_id")
        if category_id is None or int(category_id) not in [
            int(c) for c in rule["admission_category_ids"]
        ]:
            return False, "not scoped to this student's admission category"

    if basis == DiscountBasis.SIBLING.value:
        group = context.get("sibling_group")
        if not group:
            return False, "student is not in a recorded household"
        position = family_service.birth_order(group, student["id"])
        count = context.get("sibling_count", 1)
        if count < int(rule.get("min_count") or nth):
            return False, f"household has {count} child(ren); rule needs {rule.get('min_count') or nth}"
        if position < nth:
            return False, f"child {position} of the household; rule applies from child {nth}"
        return True, ""

    if basis == DiscountBasis.MULTI_REGISTRATION.value:
        count = context.get("enrollment_count", 1)
        if count < int(rule.get("min_count") or nth):
            return False, f"student holds {count} enrollment(s); rule needs {rule.get('min_count') or nth}"
        return True, ""

    if basis == DiscountBasis.CATEGORY.value:
        if not rule.get("admission_category_ids"):
            return False, "rule names no admission category"
        return True, ""

    if basis in (DiscountBasis.SCHOLARSHIP.value, DiscountBasis.MANUAL.value):
        student_ids = [int(s) for s in rule.get("student_ids") or []]
        if not student_ids:
            return False, "rule names no student"
        if int(student["id"]) not in student_ids:
            return False, "not awarded to this student"
        return True, ""

    if basis == DiscountBasis.EARLY_PAYMENT.value:
        # Time-bounded by `valid_until`, already checked by the caller. Nothing further to
        # test: if the rule is current, paying now is early.
        return True, ""

    return False, f"unknown discount basis '{basis}'"


def applicable_discounts(student: dict, academic_year_id, context: dict,
                         program: str = Program.LMS.value,
                         on: date | None = None) -> tuple[list[dict], list[dict]]:
    """
    Which concessions fire for this student, and which did not and why.

    Returns `(applicable, rejected)`. The rejected list carries the reason each rule missed,
    which is what turns the fee preview into something an admin can act on rather than argue
    with.

    Selection, not just filtering: stackable rules all apply, and among the non-stackable
    ones only the single best applies. See the module docstring.
    """
    today = on or date.today()

    applicable, rejected = [], []
    for rule in firestore_discount_rules.list_all():
        if not rule.get("is_active", True):
            continue
        if (rule.get("program") or Program.LMS.value) != str(program).upper():
            continue
        if rule.get("academic_year_id") is not None and \
                str(rule["academic_year_id"]) != str(academic_year_id):
            continue
        if not _rule_is_current(rule, today):
            rejected.append({**rule, "_reason": "outside the rule's valid dates"})
            continue

        fires, reason = _rule_applies(rule, student, context)
        (applicable if fires else rejected).append(
            rule if fires else {**rule, "_reason": reason}
        )

    return applicable, rejected


def _discount_amount(rule: dict, base: float, rate: float = 1.0) -> float:
    """
    One rule's value against a base, capped by `max_amount`.

    `base` already arrives in the display currency, so a percentage needs no conversion. A
    flat AMOUNT and a `max_amount` cap are stored in the base currency and do, or "500 off"
    would mean 500 rupees to one payer and 500 dirhams to another - a nine-fold difference in
    the same concession.
    """
    if rule.get("value_type") == DiscountValueType.AMOUNT.value:
        amount = money(float(rule.get("value") or 0) * rate)
    else:
        amount = money(base * float(rule.get("value") or 0) / 100.0)

    cap = rule.get("max_amount")
    if cap is not None:
        amount = min(amount, money(float(cap) * rate))
    # Never more than the base. A 120% rule, or a flat rule larger than the fee, would
    # otherwise produce a negative line and a bill that owes the parent money.
    return money(min(amount, base))


def _select_discounts(rules: list[dict], base: float, rate: float = 1.0) -> list[dict]:
    """
    Applies the stacking policy: every stackable rule, plus the best single non-stackable one.

    Sorted by the amount each would actually produce rather than by its percentage, because a
    capped 50% can be worth less than an uncapped 20% and the school means to give the larger
    of the two.
    """
    stackable = [r for r in rules if r.get("is_stackable")]
    exclusive = [r for r in rules if not r.get("is_stackable")]

    chosen = list(stackable)
    if exclusive:
        exclusive.sort(
            key=lambda r: (_discount_amount(r, base, rate), int(r.get("priority") or 0)),
            reverse=True,
        )
        chosen.append(exclusive[0])
    return chosen


# ---------------------------------------------------------------------------------------
# The breakdown
# ---------------------------------------------------------------------------------------

def admission_year_of(student: dict, program: str = Program.LMS.value):
    """
    The year a student was first admitted in, as an id, or None when it cannot be known.

    Three sources, most reliable first: the `admission_year_id` recorded at admission or
    fixed by the first promotion; the year whose dates contain their `admission_date`; and,
    for a record with neither, the year they are in now - which is right for a school's
    first year on this system, when every student on the roll is new to it.
    """
    stored = student.get("admission_year_id")
    if stored not in (None, ""):
        return int(stored)
    admitted_on = _as_date(student.get("admission_date"))
    if admitted_on:
        year = admission_service.year_for_date(admitted_on, program)
        if year:
            return int(year["id"])
    current = student.get("academic_year_id")
    return int(current) if current not in (None, "") else None


def compute_breakdown(student: dict, academic_year_id=None,
                      program: str = Program.LMS.value,
                      on: date | None = None,
                      currency: str | None = None) -> dict:
    """
    Everything a student owes for a year, fully derived, in one currency.

    The single source of the fee figures: the payment page renders this, the invoice
    generator freezes it, and the parent's fee screen reads it. See the module docstring for
    the order the arithmetic runs in and why each step sits where it does.

    **Currency.** Fee structures are priced in the program's base currency. Naming another
    converts the fee amounts at the administrator's stored rate and then applies *that
    currency's own* charge rules - its tax rate, its convenience percentage, its rounding.
    That is the whole point of the feature: the charges genuinely differ, so a currency is a
    rate *and* a set of rules, not just a multiplier at the end. Converting the final total
    instead would give the same proportional charge in both, which is exactly what a school
    with different costs per currency does not want.

    Returns a structure that explains itself - every line carries the tax and concession
    applied to it, and `discounts_not_applied` says why each rule that missed did so.
    """
    config = finance_settings(program)
    money_config = resolve_currency(program, currency)
    # Every base-currency amount passes through this on its way into the response.
    rate = float(money_config["rate_from_base"]) or 1.0

    def to_display(amount) -> float:
        return money(float(amount or 0) * rate)

    today = on or date.today()

    if academic_year_id is None:
        year = admission_service.current_year(program)
        if not year:
            raise HTTPException(
                status_code=400,
                detail="No academic year is configured. Create one before billing.",
            )
        academic_year_id = year["id"]

    structure = resolve_structure(student, academic_year_id, program)
    class_id = _student_class_id(student["id"])

    empty = {
        "student_id": int(student["id"]),
        "student_name": student.get("full_name"),
        "academic_year_id": int(academic_year_id),
        "currency": money_config["code"],
        "currency_symbol": money_config["symbol"],
        "base_currency": money_config["base_currency"],
        "exchange_rate": rate,
        "available_currencies": money_config["available"],
        "recommended_currency": money_config.get("recommended_currency"),
        "currency_options": money_config.get("options") or [],
        "structure_id": None,
        "structure_name": None,
        "instalment_plan_id": None,
        "instalment_plan_name": None,
        "line_items": [],
        "discounts": [],
        "discounts_not_applied": [],
        "instalments": [],
        "subtotal": 0.0,
        "discount_total": 0.0,
        "taxable_base": 0.0,
        "tax_total": 0.0,
        "tax_label": money_config["tax_label"],
        "charge_total": 0.0,
        "convenience_total": 0.0,
        "total_amount": 0.0,
        "rounding_adjustment": 0.0,
        "gateway_enabled": config["gateway_enabled"],
        "gateway_provider": config["gateway_provider"],
        "structures_not_applied": [],
        "detail": "No fee structure applies to this student for this year.",
    }
    if not structure:
        # Say *why*, per structure. "No fee structure applies" on its own is a dead end, and
        # the usual cause - a student carrying no academic year because their account predates
        # the admissions module - is invisible from the fee screen.
        class_id = _student_class_id(student["id"])
        near_misses = []
        for candidate in firestore_fee_structures.list_all():
            if (candidate.get("program") or Program.LMS.value) != str(program).upper():
                continue
            reasons = why_structure_missed(
                candidate, student, academic_year_id, class_id, program
            )
            if reasons:
                near_misses.append({
                    "structure_id": candidate.get("id"),
                    "name": candidate.get("name"),
                    "reasons": reasons,
                })

        empty["structures_not_applied"] = near_misses
        if not near_misses:
            empty["detail"] = (
                "No fee structures have been created for this program yet. "
                "Add a fee head, then a structure, before billing."
            )
        else:
            first = near_misses[0]["reasons"][0]
            empty["detail"] = (
                f"No fee structure applies to this student. "
                f"Closest: '{near_misses[0]['name']}' - {first}"
            )
        return empty

    # --- 1. Lines -----------------------------------------------------------------------
    category = None
    if student.get("admission_category_id") is not None:
        category = admission_service.firestore_admission_categories.get_document(
            str(student["admission_category_id"])
        )

    lines = []
    for item in structure.get("items") or []:
        head = firestore_fee_heads.get_document(str(item.get("fee_head_id")))
        if not head or not head.get("is_active", True):
            continue

        # An admission charge belongs on one bill only: the year the student joined. A
        # student promoted into this year is not a new admission in it, and a category that
        # waives the charge removes it for new ones too. Either way the line is dropped
        # rather than shown at 0.00, so the bill does not carry a row nobody can explain.
        if head.get("is_admission_charge"):
            if category and category.get("waives_admission_charge"):
                continue
            if admission_year_of(student, program) != int(academic_year_id):
                continue

        # Converted here, once, so every figure downstream - discounts, tax, the instalment
        # split - is already in the display currency and no later step has to remember.
        amount = to_display(item.get("amount", head.get("default_amount")))
        lines.append({
            "fee_head_id": int(head["id"]),
            "name": item.get("label") or head.get("name"),
            "code": head.get("code"),
            "kind": head.get("kind") or ChargeKind.FEE.value,
            "frequency": item.get("frequency") or head.get("frequency"),
            "amount": amount,
            "taxable": bool(head.get("is_taxable")),
            "discountable": bool(head.get("is_discountable", True)),
            # A head's own rate is a property of what is being sold and holds across
            # currencies; the fallback is the rate for the currency being shown.
            "tax_percent": (
                head.get("tax_percent")
                if head.get("tax_percent") is not None
                else money_config["tax_percent"]
            ),
            "discount_amount": 0.0,
            "tax_amount": 0.0,
            "net_amount": amount,
        })

    fee_lines = [l for l in lines if l["kind"] == ChargeKind.FEE.value]
    charge_lines = [l for l in lines if l["kind"] != ChargeKind.FEE.value]

    subtotal = money(sum(l["amount"] for l in lines))
    discountable_base = money(sum(l["amount"] for l in lines if l["discountable"]))

    # --- 2. Discounts -------------------------------------------------------------------
    group = family_service.group_for_student(student["id"])
    context = {
        "class_id": class_id,
        "sibling_group": group,
        "sibling_count": family_service.sibling_count(student["id"]),
        "enrollment_count": concurrent_enrollment_count(student["id"]),
    }

    candidates, rejected = applicable_discounts(
        student, academic_year_id, context, program, today
    )
    # A category's own concession is expressed on the category, not as a rule, so it is
    # folded in here as a synthetic rule. That keeps one code path applying concessions
    # rather than two that round differently.
    if category and category.get("default_discount_percent"):
        candidates.append({
            "id": None,
            "name": f"{category.get('name')} concession",
            "basis": DiscountBasis.CATEGORY.value,
            "value_type": DiscountValueType.PERCENT.value,
            "value": float(category["default_discount_percent"]),
            "is_stackable": False,
            "priority": 0,
            "fee_head_ids": [],
            "max_amount": None,
        })

    chosen = _select_discounts(candidates, discountable_base, rate)

    applied = []
    for rule in chosen:
        # A rule may name the heads it touches; an empty list means every discountable one.
        head_ids = [int(h) for h in rule.get("fee_head_ids") or []]
        targets = [
            l for l in lines
            if l["discountable"] and (not head_ids or l["fee_head_id"] in head_ids)
        ]
        base = money(sum(l["amount"] - l["discount_amount"] for l in targets))
        if base <= 0:
            continue

        amount = _discount_amount(rule, base, rate)
        if amount <= 0:
            continue

        # Spread across the lines it targets, in proportion, so each line's net figure is
        # explainable. The last line absorbs the rounding remainder, which is why it is
        # computed as a subtraction rather than another proportion.
        remaining = amount
        for index, line in enumerate(targets):
            if index == len(targets) - 1:
                share = remaining
            else:
                line_base = money(line["amount"] - line["discount_amount"])
                share = money(amount * (line_base / base)) if base else 0.0
                remaining = money(remaining - share)
            line["discount_amount"] = money(line["discount_amount"] + share)

        applied.append({
            "rule_id": rule.get("id"),
            "name": rule.get("name"),
            "basis": rule.get("basis"),
            "value_type": rule.get("value_type"),
            "value": rule.get("value"),
            "amount": amount,
            "stacked": bool(rule.get("is_stackable")),
        })

    discount_total = money(sum(d["amount"] for d in applied))

    # --- 3. Tax, on the discounted base -------------------------------------------------
    tax_total = 0.0
    taxable_base = 0.0
    if money_config["tax_enabled"]:
        for line in lines:
            if not line["taxable"]:
                continue
            net = money(line["amount"] - line["discount_amount"])
            taxable_base = money(taxable_base + net)
            # Named `tax_rate`, not `rate`: `rate` is the exchange rate this whole function
            # converts with, and shadowing it here silently reported a tax percentage as the
            # exchange rate on the response - which then froze onto every invoice issued.
            tax_rate = float(line["tax_percent"] or 0)
            if money_config["tax_inclusive"]:
                # The entered amount already contains the tax, so it is extracted rather
                # than added - the payable does not change, only the way it is shown.
                line["tax_amount"] = (
                    money(net - (net / (1 + tax_rate / 100.0))) if tax_rate else 0.0
                )
            else:
                line["tax_amount"] = money(net * tax_rate / 100.0)
            tax_total = money(tax_total + line["tax_amount"])

    for line in lines:
        line["net_amount"] = money(
            line["amount"] - line["discount_amount"]
            + (0.0 if money_config["tax_inclusive"] else line["tax_amount"])
        )

    # --- 4. Charges and convenience -----------------------------------------------------
    charge_total = money(sum(
        l["amount"] - l["discount_amount"] for l in charge_lines
    ))

    payable_before_convenience = money(
        sum(l["net_amount"] for l in lines)
    )
    convenience = money(
        payable_before_convenience * float(money_config["convenience_percent"]) / 100.0
        + float(money_config["convenience_amount"])
    )

    raw_total = money(payable_before_convenience + convenience)
    total = _apply_rounding(raw_total, money_config["rounding"])

    # --- 5. Instalments -----------------------------------------------------------------
    plan = resolve_plan(student, structure, academic_year_id, program)
    instalments = build_instalments(plan, total, academic_year_id, config, lines)

    return {
        "student_id": int(student["id"]),
        "student_name": student.get("full_name"),
        "academic_year_id": int(academic_year_id),
        "currency": money_config["code"],
        "currency_symbol": money_config["symbol"],
        "base_currency": money_config["base_currency"],
        "exchange_rate": rate,
        "available_currencies": money_config["available"],
        "recommended_currency": money_config.get("recommended_currency"),
        "currency_options": money_config.get("options") or [],
        "structure_id": int(structure["id"]),
        "structure_name": structure.get("name"),
        "instalment_plan_id": int(plan["id"]) if plan else None,
        "instalment_plan_name": plan.get("name") if plan else None,
        "line_items": lines,
        "discounts": applied,
        "discounts_not_applied": [
            {"rule_id": r.get("id"), "name": r.get("name"),
             "basis": r.get("basis"), "reason": r.get("_reason")}
            for r in rejected
        ],
        "instalments": instalments,
        "subtotal": subtotal,
        "discount_total": discount_total,
        "taxable_base": taxable_base,
        "tax_total": tax_total,
        "tax_label": money_config["tax_label"],
        "charge_total": charge_total,
        "convenience_total": convenience,
        "total_amount": total,
        "rounding_adjustment": money(total - raw_total),
        "gateway_enabled": config["gateway_enabled"],
        "gateway_provider": config["gateway_provider"],
        "structures_not_applied": [],
        "detail": (
            f"{len(fee_lines)} fee line(s), {len(charge_lines)} charge(s), "
            f"{len(applied)} concession(s) applied."
        ),
    }


def build_instalments(plan: dict | None, total: float, academic_year_id,
                      config: dict, lines: list[dict] | None = None) -> list[dict]:
    """
    Splits a total into dated instalments.

    The schedule comes from the resolved plan, or, when the year has none, from its two
    terms in equal halves. Either way two rules hold:

      * A recurring fee - tuition, transport - is shared across the instalments in the
        plan's proportions. A ONE_TIME charge such as the admission fee is not averaged in
        and not folded into Term 1 either: it is an instalment of its own, due on joining,
        listed before the terms. So "30,000 tuition + 10,000 admission" bills as Admission
        Fee: 10,000, Term 1: 15,000, Term 2: 15,000, and each instalment carries
        `components` saying which fee contributed what.
      * An instalment tied to a term falls due at that term's start plus the configured
        due-days, so the dates follow the year's calendar rather than a number typed once.

    A plan of fixed amounts is taken as written. The final line absorbs the rounding
    remainder for the same reason a discount's last line does: instalments that do not sum
    to the total are the first thing anybody notices.
    """
    if total <= 0:
        return []

    year = admission_service.firestore_academic_years.get_document(str(academic_year_id)) or {}
    year_start = _as_date(year.get("start_date")) or date.today()
    due_days = int(config["invoice_due_days"])
    terms = admission_service.terms_of(year) if year else []
    term_starts = {t["key"]: _as_date(t.get("start_date")) for t in terms}

    if plan and plan.get("instalments"):
        schedule = list(plan["instalments"])
    elif terms:
        share = 100.0 / len(terms)
        schedule = [{"label": t["name"], "term": t["key"], "percent": share} for t in terms]
    else:
        return [{
            "label": "Full payment",
            "term": None,
            "due_date": (year_start + timedelta(days=due_days)).isoformat(),
            "amount": money(total),
            "amount_paid": 0.0,
            "status": InstalmentStatus.PENDING.value,
            "components": [
                {"name": l.get("name"), "amount": money(l.get("net_amount", l.get("amount")))}
                for l in (lines or [])
            ],
        }]

    lines = lines or []
    one_time = [l for l in lines if l.get("frequency") == FeeFrequency.ONE_TIME.value]
    recurring = [l for l in lines if l.get("frequency") != FeeFrequency.ONE_TIME.value]
    fixed = any(line.get("amount") is not None for line in schedule)
    one_time_total = 0.0 if fixed else money(
        sum(money(l.get("net_amount", l.get("amount"))) for l in one_time)
    )
    # Tax on a one-time line is already inside its net amount; convenience and rounding
    # sit in `total` and are shared out with the recurring part.
    spread_total = money(total - one_time_total)

    out = []
    remaining = money(total)

    # One-time charges first, each as its own instalment, due on joining. Kept apart from
    # the terms so the office sees "Admission Fee 10,000" as a line to collect and tick off,
    # not a lump hidden inside Term 1 - and so next year's bill, with no such line, still
    # reads the same way.
    if not fixed:
        for fee in one_time:
            amount = money(fee.get("net_amount", fee.get("amount")))
            if amount <= 0:
                continue
            remaining = money(remaining - amount)
            out.append({
                "label": fee.get("name") or "One-time charge",
                "term": None,
                "due_date": (year_start + timedelta(days=due_days)).isoformat(),
                "amount": amount,
                "amount_paid": 0.0,
                "status": InstalmentStatus.PENDING.value,
                "components": [{"name": fee.get("name"), "amount": amount}],
            })

    for index, line in enumerate(schedule):
        last = index == len(schedule) - 1
        percent = float(line.get("percent") or 0) / 100.0

        if line.get("amount") is not None:
            amount = money(line["amount"])
        elif last:
            amount = remaining
        else:
            amount = money(spread_total * percent)
        remaining = money(remaining - amount)

        components = []
        if not fixed:
            for fee in recurring:
                share = money(money(fee.get("net_amount", fee.get("amount"))) * percent)
                if share:
                    components.append({"name": fee.get("name"), "amount": share})

        term_key = line.get("term")
        due = _as_date(line.get("due_date"))
        if due is None and line.get("due_after_days") is not None:
            due = year_start + timedelta(days=int(line["due_after_days"]))
        if due is None and term_key and term_starts.get(term_key):
            due = term_starts[term_key] + timedelta(days=due_days)
        if due is None:
            due = year_start + timedelta(days=due_days)

        out.append({
            "label": line.get("label") or f"Instalment {index + 1}",
            "term": term_key,
            "due_date": due.isoformat(),
            "amount": amount,
            "amount_paid": 0.0,
            "status": InstalmentStatus.PENDING.value,
            "components": components,
        })

    # Fixed-amount plans need not sum to the total; the shortfall or excess lands on the
    # last line rather than silently disappearing from the bill.
    if out and abs(remaining) > 0.009:
        out[-1]["amount"] = money(out[-1]["amount"] + remaining)

    return out


def instalment_view(instalment: dict, grace_days: int, on: date | None = None) -> dict:
    """
    One instalment with its status resolved against the clock.

    OVERDUE is computed here and never stored. Storing it would mean an instalment became
    overdue only when a sweep ran, and a fee report that is wrong between midnight and the
    sweep is one nobody trusts.
    """
    today = on or date.today()
    view = dict(instalment)

    stored_status = view.get("status")
    if stored_status in (InstalmentStatus.PAID.value, InstalmentStatus.WAIVED.value):
        view["is_overdue"] = False
        view["days_overdue"] = 0
        return view

    due = _as_date(view.get("due_date"))
    outstanding = money(view.get("amount", 0) - view.get("amount_paid", 0))
    view["outstanding"] = outstanding

    if due and outstanding > 0 and today > due + timedelta(days=int(grace_days or 0)):
        view["status"] = InstalmentStatus.OVERDUE.value
        view["is_overdue"] = True
        view["days_overdue"] = (today - due).days
    else:
        view["is_overdue"] = False
        view["days_overdue"] = 0

    return view


# ---------------------------------------------------------------------------------------
# Structure, plan and rule management
# ---------------------------------------------------------------------------------------

def _validate_items(items) -> list[dict]:
    """
    Turns structure lines into storable dicts, checking each head exists and is usable.

    An inactive head is refused rather than silently dropped at billing time. A structure
    that looks like it charges transport but produces a bill without it is the hardest kind
    of fee bug to see, because nothing anywhere reports an error.
    """
    stored = []
    seen = set()
    for item in items:
        head = require_head(item.fee_head_id)
        if not head.get("is_active", True):
            raise HTTPException(
                status_code=400,
                detail=f"Fee head '{head.get('name')}' is inactive and cannot be charged. "
                       "Reactivate it, or remove it from this structure.",
            )
        if int(item.fee_head_id) in seen:
            raise HTTPException(
                status_code=400,
                detail=f"Fee head '{head.get('name')}' appears twice in this structure. "
                       "Charge it once, or create a second head for the second charge.",
            )
        seen.add(int(item.fee_head_id))

        stored.append({
            "fee_head_id": int(item.fee_head_id),
            "amount": money(item.amount),
            "frequency": getattr(item.frequency, "value", item.frequency),
            "label": item.label,
        })
    return stored


def create_structure(payload, actor_id: int | None = None) -> dict:
    admission_service.require_year(payload.academic_year_id)
    program = getattr(payload.program, "value", payload.program)
    config = finance_settings(program)

    structure_id = firestore_fee_structures.get_next_numeric_id()
    document = {
        "name": payload.name.strip(),
        "academic_year_id": int(payload.academic_year_id),
        "items": _validate_items(payload.items),
        "class_ids": [int(c) for c in payload.class_ids or []],
        "admission_category_ids": [int(c) for c in payload.admission_category_ids or []],
        "program": program,
        "currency": (payload.currency or config["currency"]).strip(),
        "priority": int(payload.priority or 0),
        "is_active": bool(payload.is_active),
        "notes": payload.notes,
        "created_by": actor_id,
        "created_at": _now(),
    }
    firestore_fee_structures.add_document(str(structure_id), document)
    document["id"] = structure_id

    # A structure with no schedule to collect it on is half a fee. The default two-term
    # plan is created alongside the first structure of a year, if the year has none yet.
    try:
        ensure_default_plan(admission_service.require_year(payload.academic_year_id),
                            program, actor_id)
    except Exception as exc:  # pragma: no cover - a plan is a convenience, not a gate
        logger.warning("Could not create the default instalment plan: %s", exc)
    return document


def update_structure(structure: dict, payload, actor_id: int | None = None) -> dict:
    updates = payload.model_dump(exclude_unset=True, exclude_none=True)
    if "items" in updates:
        updates["items"] = _validate_items(payload.items)
    for field in ("class_ids", "admission_category_ids"):
        if field in updates:
            updates[field] = [int(v) for v in updates[field]]

    updates["updated_at"] = _now()
    updates["updated_by"] = actor_id
    firestore_fee_structures.add_document(str(structure["id"]), updates)
    return {**structure, **updates}


def delete_structure(structure: dict) -> None:
    """
    Refuses while any invoice was built from the structure.

    An invoice keeps its own frozen copy of the lines, so deleting the structure would not
    corrupt the bill - but it would break every "which structure produced this?" answer, and
    that is the question anybody investigating a disputed fee asks first.
    """
    from app.core.firebase import firestore_fee_invoices

    built = [
        i for i in firestore_fee_invoices.list_all()
        if str(i.get("structure_id")) == str(structure["id"])
    ]
    if built:
        raise HTTPException(
            status_code=400,
            detail=f"{len(built)} invoice(s) were built from '{structure['name']}'. "
                   "Deactivate it instead, so those bills stay explainable.",
        )
    firestore_fee_structures.delete_document(str(structure["id"]))


def structure_total(structure: dict) -> float:
    return money(sum(money(i.get("amount")) for i in structure.get("items") or []))


def list_structures(program: str | None = None, academic_year_id=None,
                    include_inactive: bool = False) -> list[dict]:
    structures = firestore_fee_structures.list_all()
    if program:
        wanted = str(program).upper()
        structures = [
            s for s in structures if (s.get("program") or Program.LMS.value) == wanted
        ]
    if academic_year_id is not None:
        structures = [
            s for s in structures if str(s.get("academic_year_id")) == str(academic_year_id)
        ]
    if not include_inactive:
        structures = [s for s in structures if s.get("is_active", True)]
    return sorted(structures, key=lambda s: str(s.get("name") or ""))


def present_structure(structure: dict) -> dict:
    year = admission_service.firestore_academic_years.get_document(
        str(structure.get("academic_year_id"))
    ) or {}

    class_names = []
    for class_id in structure.get("class_ids") or []:
        room = firestore_classes.get_document(str(class_id))
        if room and room.get("name"):
            class_names.append(room["name"])

    category_names = []
    for category_id in structure.get("admission_category_ids") or []:
        category = admission_service.firestore_admission_categories.get_document(str(category_id))
        if category and category.get("name"):
            category_names.append(category["name"])

    # Each line gets its head's name resolved, so the structure editor renders without a
    # request per row.
    items = []
    for item in structure.get("items") or []:
        head = firestore_fee_heads.get_document(str(item.get("fee_head_id"))) or {}
        items.append({
            **item,
            "head_name": head.get("name"),
            "head_code": head.get("code"),
            "kind": head.get("kind"),
            "is_taxable": bool(head.get("is_taxable")),
        })

    return {
        "id": int(structure["id"]),
        "name": structure.get("name"),
        "academic_year_id": int(structure["academic_year_id"]),
        "academic_year_name": year.get("name"),
        "items": items,
        "class_ids": structure.get("class_ids") or [],
        "class_names": class_names,
        "admission_category_ids": structure.get("admission_category_ids") or [],
        "admission_category_names": category_names,
        "program": structure.get("program") or Program.LMS.value,
        "currency": structure.get("currency") or "INR",
        "priority": int(structure.get("priority") or 0),
        "is_active": bool(structure.get("is_active", True)),
        "notes": structure.get("notes"),
        "gross_total": structure_total(structure),
        "created_at": structure.get("created_at"),
    }


def _stored_instalments(lines) -> list[dict]:
    return [{
        "label": line.label,
        "percent": line.percent,
        "amount": money(line.amount) if line.amount is not None else None,
        "term": getattr(line.term, "value", line.term) if getattr(line, "term", None) else None,
        "due_date": line.due_date.isoformat() if line.due_date else None,
        "due_after_days": line.due_after_days,
    } for line in lines]


def ensure_default_plan(year: dict, program: str = Program.LMS.value,
                        actor_id: int | None = None) -> dict | None:
    """
    The two-term plan every year starts with, created once per year and program.

    Made explicit rather than left as the biller's implicit fallback, so the office can see
    the terms under Instalment plans, change the split from 50/50, and see the due dates -
    "why is there no plan?" was the first question the empty tab produced. Returns the
    existing plan when one is already there for the year, so it is safe to call on every
    year creation and every structure creation.
    """
    wanted_program = str(program).upper()
    for plan in firestore_instalment_plans.list_all():
        if str(plan.get("academic_year_id")) == str(year["id"]) \
                and (plan.get("program") or Program.LMS.value) == wanted_program:
            return plan

    terms = admission_service.terms_of(year)
    if not terms:
        return None

    from app.schemas.finance import InstalmentLine, InstalmentPlanCreate

    share = round(100.0 / len(terms), 2)
    lines = [
        InstalmentLine(label=term["name"], term=term["key"],
                       percent=share if index < len(terms) - 1 else round(100.0 - share * (len(terms) - 1), 2))
        for index, term in enumerate(terms)
    ]
    plan = create_plan(InstalmentPlanCreate(
        name="Two terms", academic_year_id=int(year["id"]), instalments=lines,
        program=wanted_program, is_default=True,
    ), actor_id)
    logger.info("Created the default two-term instalment plan for year %s (%s).",
                year["id"], year.get("name"))
    return plan


def create_plan(payload, actor_id: int | None = None) -> dict:
    admission_service.require_year(payload.academic_year_id)
    if payload.structure_id is not None:
        require_structure(payload.structure_id)

    lines = _stored_instalments(payload.instalments)
    validate_instalments(lines)

    plan_id = firestore_instalment_plans.get_next_numeric_id()
    document = {
        "name": payload.name.strip(),
        "academic_year_id": int(payload.academic_year_id),
        "instalments": lines,
        "class_ids": [int(c) for c in payload.class_ids or []],
        "admission_category_ids": [int(c) for c in payload.admission_category_ids or []],
        "structure_id": int(payload.structure_id) if payload.structure_id is not None else None,
        "program": getattr(payload.program, "value", payload.program),
        "grace_days": payload.grace_days,
        "is_default": bool(payload.is_default),
        "is_active": bool(payload.is_active),
        "created_by": actor_id,
        "created_at": _now(),
    }
    firestore_instalment_plans.add_document(str(plan_id), document)
    document["id"] = plan_id
    return document


def update_plan(plan: dict, payload, actor_id: int | None = None) -> dict:
    updates = payload.model_dump(exclude_unset=True, exclude_none=True)
    if "instalments" in updates:
        lines = _stored_instalments(payload.instalments)
        validate_instalments(lines)
        updates["instalments"] = lines
    for field in ("class_ids", "admission_category_ids"):
        if field in updates:
            updates[field] = [int(v) for v in updates[field]]

    updates["updated_at"] = _now()
    updates["updated_by"] = actor_id
    firestore_instalment_plans.add_document(str(plan["id"]), updates)
    return {**plan, **updates}


def delete_plan(plan: dict) -> None:
    firestore_instalment_plans.delete_document(str(plan["id"]))


def list_plans(program: str | None = None, academic_year_id=None,
               include_inactive: bool = False) -> list[dict]:
    plans = firestore_instalment_plans.list_all()
    if program:
        wanted = str(program).upper()
        plans = [p for p in plans if (p.get("program") or Program.LMS.value) == wanted]
    if academic_year_id is not None:
        plans = [p for p in plans if str(p.get("academic_year_id")) == str(academic_year_id)]
    if not include_inactive:
        plans = [p for p in plans if p.get("is_active", True)]
    return sorted(plans, key=lambda p: str(p.get("name") or ""))


def require_rule(rule_id) -> dict:
    return require_document(firestore_discount_rules, rule_id, "Discount rule")


def create_rule(payload, actor_id: int | None = None) -> dict:
    if payload.academic_year_id is not None:
        admission_service.require_year(payload.academic_year_id)
    for head_id in payload.fee_head_ids or []:
        require_head(head_id)

    rule_id = firestore_discount_rules.get_next_numeric_id()
    document = {
        "name": payload.name.strip(),
        "basis": getattr(payload.basis, "value", payload.basis),
        "value_type": getattr(payload.value_type, "value", payload.value_type),
        "value": float(payload.value),
        "academic_year_id": (
            int(payload.academic_year_id) if payload.academic_year_id is not None else None
        ),
        "program": getattr(payload.program, "value", payload.program),
        "applies_from_nth": int(payload.applies_from_nth or 2),
        "min_count": payload.min_count,
        "admission_category_ids": [int(c) for c in payload.admission_category_ids or []],
        "class_ids": [int(c) for c in payload.class_ids or []],
        "student_ids": [int(s) for s in payload.student_ids or []],
        "fee_head_ids": [int(h) for h in payload.fee_head_ids or []],
        "max_amount": payload.max_amount,
        "is_stackable": bool(payload.is_stackable),
        "priority": int(payload.priority or 0),
        "valid_from": payload.valid_from.isoformat() if payload.valid_from else None,
        "valid_until": payload.valid_until.isoformat() if payload.valid_until else None,
        "is_active": bool(payload.is_active),
        "notes": payload.notes,
        "created_by": actor_id,
        "created_at": _now(),
    }
    firestore_discount_rules.add_document(str(rule_id), document)
    document["id"] = rule_id
    return document


def update_rule(rule: dict, payload, actor_id: int | None = None) -> dict:
    updates = payload.model_dump(exclude_unset=True, exclude_none=True)
    if "value_type" in updates:
        updates["value_type"] = getattr(updates["value_type"], "value", updates["value_type"])
    for field in ("admission_category_ids", "class_ids", "student_ids", "fee_head_ids"):
        if field in updates:
            updates[field] = [int(v) for v in updates[field]]
    for field in ("valid_from", "valid_until"):
        if field in updates and updates[field] is not None:
            updates[field] = updates[field].isoformat()

    updates["updated_at"] = _now()
    updates["updated_by"] = actor_id
    firestore_discount_rules.add_document(str(rule["id"]), updates)
    return {**rule, **updates}


def delete_rule(rule: dict) -> None:
    firestore_discount_rules.delete_document(str(rule["id"]))


def list_rules(program: str | None = None, academic_year_id=None,
               include_inactive: bool = False) -> list[dict]:
    rules = firestore_discount_rules.list_all()
    if program:
        wanted = str(program).upper()
        rules = [r for r in rules if (r.get("program") or Program.LMS.value) == wanted]
    if academic_year_id is not None:
        rules = [
            r for r in rules
            if r.get("academic_year_id") in (None, "", int(academic_year_id))
        ]
    if not include_inactive:
        rules = [r for r in rules if r.get("is_active", True)]
    return sorted(rules, key=lambda r: (-int(r.get("priority") or 0), str(r.get("name") or "")))
