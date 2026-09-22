"""
Fee administration, billing, and the payment page.

Three routers:

  * `/admin/finance/...` - the bursar's screens: heads, structures, instalment plans,
    discount rules, the charges-and-tax settings, and invoicing.
  * `/fees/...`          - a student's own fees.
  * `/parent/fees/...`   - the same, for a linked parent who is permitted to see them.

The last two are thin wrappers over `finance.compute_breakdown` and
`billing.present_invoice`, deliberately. The figure a parent sees and the figure the bursar
sees must come from the same function or they will eventually differ, and the parent will be
the one who notices.
"""

from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from app.api.v1.dependencies import (
    require_admin, require_roles, require_student,
)
from app.core.enums import DiscountBasis, PaymentIntentStatus, Program, UserRole
from app.core.firebase import firestore_fee_invoices, firestore_users, require_document
from app.schemas.finance import (
    CurrencySettingsOut, CurrencySettingsUpdate, FxStatusOut,
    DiscountRuleCreate, DiscountRuleOut, DiscountRuleUpdate,
    FeeBreakdownOut, FeeHeadCreate, FeeHeadOut, FeeHeadUpdate,
    FeeInvoiceOut, FeeStructureCreate, FeeStructureOut, FeeStructureUpdate,
    FinanceSettingsOut, FinanceSettingsUpdate, GatewayCallback,
    InstalmentPlanCreate, InstalmentPlanOut, InstalmentPlanUpdate,
    FeeCollect, FeeReceiptCreate, FeeReceiptOut,
    PaymentIntentCreate, PaymentIntentOut, PaymentRecord,
    WaiveInstalmentRequest,
)
from app.schemas.user import UserOut
from app.services import billing
from app.services import families as family_service
from app.services import finance
from app.services import finance_reports as reports
from app.services import receipts as receipt_service
from app.services.tuition.settings_store import (
    currency_settings, finance_settings, save_currency_settings, save_settings,
)

admin_router = APIRouter(prefix="/admin/finance", tags=["Finance (Admin)"])
student_router = APIRouter(prefix="/fees", tags=["Fees (Student)"])
parent_router = APIRouter(prefix="/parent/fees", tags=["Fees (Parent)"])

require_parent = require_roles([UserRole.PARENT, UserRole.ADMIN])


# ---------------------------------------------------------------------------------------
# Settings - the charges and tax page
# ---------------------------------------------------------------------------------------

@admin_router.get("/settings", response_model=FinanceSettingsOut)
def get_finance_settings(
    program: Program = Query(Program.LMS), _: UserOut = Depends(require_admin)
):
    """
    [Admin Only] The money configuration: currency, tax, late fees, convenience charge,
    rounding, invoice numbering, and whether a payment gateway is enabled.

    Everything here is editable without a redeploy, because these are decisions the bursar
    makes and changes. Defaults are inert - tax off, no late fee, no rounding - so a school
    that never opens this screen bills exactly the amounts typed into its fee structures.
    """
    return FinanceSettingsOut(**finance_settings(program.value))


@admin_router.put("/settings", response_model=FinanceSettingsOut)
def update_finance_settings(
    payload: FinanceSettingsUpdate,
    program: Program = Query(Program.LMS),
    admin: UserOut = Depends(require_admin),
):
    """
    [Admin Only] Update the charges and tax settings. Partial: omitted fields are unchanged.

    Changing tax or rounding affects every **draft** invoice the next time it is regenerated.
    Issued invoices are frozen and keep the figures they were issued with, which is the point
    of issuing them.
    """
    updates = payload.model_dump(exclude_unset=True, exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No settings provided to update")

    save_settings(program.value, updates, admin.id)
    return FinanceSettingsOut(**finance_settings(program.value))


# ---------------------------------------------------------------------------------------
# Currencies
# ---------------------------------------------------------------------------------------

@admin_router.get("/currencies", response_model=CurrencySettingsOut)
def get_currencies(
    program: Program = Query(Program.LMS), _: UserOut = Depends(require_admin)
):
    """
    [Admin Only] Every currency this program offers, with its rate and its own charge rules.

    A currency is a rate **and a set of charges**, not just a multiplier: a domestic INR
    transfer carries a gateway percentage an AED card payment does not, and the tax position
    is rarely the same. Each currency overrides only the charge keys it needs; the rest fall
    through to the program's finance settings, so a school running one currency never fills
    any of this in.

    `available` is what the payer's switcher should offer - enabled currencies that actually
    carry a rate. A currency with no rate still appears under `currencies` so you can
    configure it, but is never offered, because showing a price of zero is worse than not
    offering the currency.

    `rate_status` says how each rate was obtained on this response: `base`, `manual`, `live`,
    `cached`, `stale`, or `fallback_manual` when the provider was unreachable and the
    school's own rate was used instead.
    """
    return CurrencySettingsOut(**currency_settings(program.value))


@admin_router.put("/currencies", response_model=CurrencySettingsOut)
def update_currencies(
    payload: CurrencySettingsUpdate,
    program: Program = Query(Program.LMS),
    admin: UserOut = Depends(require_admin),
):
    """
    [Admin Only] Set the base currency, the rates, and the per-currency charges.

    Merged per currency, so editing the AED rate does not require resending the INR charge
    rules you were not looking at. This is where "AED has fewer charges than INR" is
    expressed - set each currency's own `tax_percent`, `convenience_percent` and the rest:

        { "base_currency": "INR",
          "currencies": {
            "INR": { "tax_enabled": true, "tax_percent": 18, "convenience_percent": 2 },
            "AED": { "rate_source": "LIVE", "rate_from_base": 0.044,
                     "tax_enabled": true, "tax_percent": 5, "convenience_percent": 0.5 } } }

    `rate_source: "LIVE"` fetches from the exchange-rate provider; keep `rate_from_base` set
    anyway, because that is what a live fetch falls back to when the provider is down.

    Changing rates or charges affects **draft** invoices on their next regeneration. Issued
    invoices keep the rate they were issued at - a bill that re-converts at today's rate is a
    bill whose total changes after it was sent.
    """
    return CurrencySettingsOut(**save_currency_settings(
        program.value,
        payload.base_currency,
        {code: cfg.model_dump(exclude_unset=True, exclude_none=True)
         for code, cfg in (payload.currencies or {}).items()},
        admin.id,
    ))


@admin_router.get("/currencies/rates", response_model=FxStatusOut)
def fx_status(_: UserOut = Depends(require_admin)):
    """
    [Admin Only] What the live-rate cache holds, and when it was last refreshed.

    Rates are cached rather than fetched per request: a fee page opened by thirty parents at
    once must not make thirty outbound calls.
    """
    from app.core import fx

    return FxStatusOut(**fx.cache_status())


@admin_router.post("/currencies/rates/refresh")
def refresh_fx(
    program: Program = Query(Program.LMS), _: UserOut = Depends(require_admin)
):
    """
    [Admin Only] Force a live rate refresh now, ignoring the cache.

    Returns the rate resolved for every currency afterwards, with its status - so a refresh
    that failed reports `fallback_manual` rather than looking like it worked.
    """
    from app.core import fx

    fx.clear_cache()
    config = currency_settings(program.value)
    return {
        "base_currency": config["base_currency"],
        "refreshed_at": fx.cache_status(),
        "rates": {
            code: {
                "rate": entry["rate_from_base"],
                "source": entry["rate_source"],
                "status": entry["rate_status"],
                "fetched_at": entry["rate_fetched_at"],
                "detail": entry.get("rate_detail"),
            }
            for code, entry in config["currencies"].items()
        },
    }


# ---------------------------------------------------------------------------------------
# Fee heads
# ---------------------------------------------------------------------------------------

@admin_router.post("/heads", response_model=FeeHeadOut, status_code=status.HTTP_201_CREATED)
def create_head(payload: FeeHeadCreate, admin: UserOut = Depends(require_admin)):
    """
    [Admin Only] Add something the school can charge for.

    A catalogue entry, not an amount anybody owes - the binding amount is the one on the fee
    structure. `is_taxable` decides whether the head contributes to the tax base, and
    `is_discountable` should be off for statutory charges the school only passes on.
    """
    return FeeHeadOut(**finance.create_head(payload, admin.id))


@admin_router.get("/heads", response_model=List[FeeHeadOut])
def list_heads(
    program: Optional[Program] = Query(None),
    include_inactive: bool = Query(False),
    _: UserOut = Depends(require_admin),
):
    """[Admin Only] The fee catalogue, in display order."""
    return [FeeHeadOut(**h) for h in finance.list_heads(
        program.value if program else None, include_inactive
    )]


@admin_router.put("/heads/{head_id}", response_model=FeeHeadOut)
def update_head(head_id: int, payload: FeeHeadUpdate, admin: UserOut = Depends(require_admin)):
    """[Admin Only] Update a fee head. Partial: omitted fields are left unchanged."""
    return FeeHeadOut(**finance.update_head(finance.require_head(head_id), payload, admin.id))


@admin_router.delete("/heads/{head_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_head(head_id: int, _: UserOut = Depends(require_admin)):
    """
    [Admin Only] Delete a fee head.

    Refused while any structure charges it - those structures would keep an id that resolves
    to nothing and quietly drop a line from every bill. Deactivate instead.
    """
    finance.delete_head(finance.require_head(head_id))


# ---------------------------------------------------------------------------------------
# Fee structures
# ---------------------------------------------------------------------------------------

@admin_router.post("/structures", response_model=FeeStructureOut,
                   status_code=status.HTTP_201_CREATED)
def create_structure(payload: FeeStructureCreate, admin: UserOut = Depends(require_admin)):
    """
    [Admin Only] Set what a cohort is charged for a year.

    Leave `class_ids` and `admission_category_ids` empty for the year's default structure,
    which is all a school charging everybody the same ever needs. Add a narrower structure to
    override it for one class or category - the most specific match wins, so the default does
    not need editing or deleting.
    """
    return FeeStructureOut(
        **finance.present_structure(finance.create_structure(payload, admin.id))
    )


@admin_router.get("/structures", response_model=List[FeeStructureOut])
def list_structures(
    program: Optional[Program] = Query(None),
    academic_year_id: Optional[int] = Query(None),
    include_inactive: bool = Query(False),
    _: UserOut = Depends(require_admin),
):
    """[Admin Only] Fee structures, with their totals and resolved scope names."""
    return [
        FeeStructureOut(**finance.present_structure(s))
        for s in finance.list_structures(
            program.value if program else None, academic_year_id, include_inactive
        )
    ]


@admin_router.get("/structures/{structure_id}", response_model=FeeStructureOut)
def get_structure(structure_id: int, _: UserOut = Depends(require_admin)):
    """[Admin Only] One fee structure."""
    return FeeStructureOut(**finance.present_structure(finance.require_structure(structure_id)))


@admin_router.put("/structures/{structure_id}", response_model=FeeStructureOut)
def update_structure(
    structure_id: int, payload: FeeStructureUpdate, admin: UserOut = Depends(require_admin)
):
    """
    [Admin Only] Update a structure. Partial: omitted fields are left unchanged.

    Affects draft invoices on their next regeneration. Issued invoices keep their own frozen
    copy of the lines and are untouched.
    """
    structure = finance.require_structure(structure_id)
    return FeeStructureOut(
        **finance.present_structure(finance.update_structure(structure, payload, admin.id))
    )


@admin_router.delete("/structures/{structure_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_structure(structure_id: int, _: UserOut = Depends(require_admin)):
    """[Admin Only] Delete a structure. Refused once invoices have been built from it."""
    finance.delete_structure(finance.require_structure(structure_id))


# ---------------------------------------------------------------------------------------
# Instalment plans
# ---------------------------------------------------------------------------------------

@admin_router.post("/plans", response_model=InstalmentPlanOut,
                   status_code=status.HTTP_201_CREATED)
def create_plan(payload: InstalmentPlanCreate, admin: UserOut = Depends(require_admin)):
    """
    [Admin Only] Set when a structure's total falls due.

    Lines carry either a `percent` or a fixed `amount`, not both - there is no single correct
    order in which to apply "40% and 5,000". Percentages must total 100%, checked here
    because a plan totalling 90% under-bills every student on it, silently, for a year.

    Use `due_after_days` rather than `due_date` to make one plan serve every year: the date is
    computed from the academic year's start.
    """
    return InstalmentPlanOut(**finance.create_plan(payload, admin.id))


@admin_router.get("/plans", response_model=List[InstalmentPlanOut])
def list_plans(
    program: Optional[Program] = Query(None),
    academic_year_id: Optional[int] = Query(None),
    include_inactive: bool = Query(False),
    _: UserOut = Depends(require_admin),
):
    """[Admin Only] Instalment plans."""
    return [InstalmentPlanOut(**p) for p in finance.list_plans(
        program.value if program else None, academic_year_id, include_inactive
    )]


@admin_router.put("/plans/{plan_id}", response_model=InstalmentPlanOut)
def update_plan(
    plan_id: int, payload: InstalmentPlanUpdate, admin: UserOut = Depends(require_admin)
):
    """[Admin Only] Update an instalment plan. Percentages are re-checked to total 100%."""
    return InstalmentPlanOut(**finance.update_plan(finance.require_plan(plan_id), payload, admin.id))


@admin_router.delete("/plans/{plan_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_plan(plan_id: int, _: UserOut = Depends(require_admin)):
    """[Admin Only] Delete an instalment plan."""
    finance.delete_plan(finance.require_plan(plan_id))


# ---------------------------------------------------------------------------------------
# Discount rules
# ---------------------------------------------------------------------------------------

@admin_router.post("/discounts", response_model=DiscountRuleOut,
                   status_code=status.HTTP_201_CREATED)
def create_discount(payload: DiscountRuleCreate, admin: UserOut = Depends(require_admin)):
    """
    [Admin Only] Create a concession.

    The two the brief asks for:

    * **Sibling** - `basis: SIBLING`, `applies_from_nth: 2` gives the second child onward a
      concession while the eldest pays full fee. Counted over the student's recorded
      household, so the family must be linked under `/admin/families/groups` first.
    * **Two concurrent registrations** - `basis: MULTI_REGISTRATION`, `applies_from_nth: 2`.
      Counted over the enrollments one student holds across both products, which is the
      "same time 2 Reg." case.

    `is_stackable` is off by default: rules compete and only the best-valued one applies. A
    student who is a staff ward, a second child and an early payer would otherwise combine
    three concessions into a negative bill. Turn it on deliberately, and use `max_amount` to
    cap what any single rule can be worth.
    """
    return DiscountRuleOut(**finance.create_rule(payload, admin.id))


@admin_router.get("/discounts", response_model=List[DiscountRuleOut])
def list_discounts(
    program: Optional[Program] = Query(None),
    academic_year_id: Optional[int] = Query(None),
    include_inactive: bool = Query(False),
    _: UserOut = Depends(require_admin),
):
    """[Admin Only] Discount rules, highest priority first."""
    return [DiscountRuleOut(**r) for r in finance.list_rules(
        program.value if program else None, academic_year_id, include_inactive
    )]


@admin_router.put("/discounts/{rule_id}", response_model=DiscountRuleOut)
def update_discount(
    rule_id: int, payload: DiscountRuleUpdate, admin: UserOut = Depends(require_admin)
):
    """[Admin Only] Update a discount rule. Partial: omitted fields are left unchanged."""
    return DiscountRuleOut(**finance.update_rule(finance.require_rule(rule_id), payload, admin.id))


@admin_router.delete("/discounts/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_discount(rule_id: int, _: UserOut = Depends(require_admin)):
    """[Admin Only] Delete a discount rule. Issued invoices keep the concession they were given."""
    finance.delete_rule(finance.require_rule(rule_id))


# ---------------------------------------------------------------------------------------
# Previewing and invoicing
# ---------------------------------------------------------------------------------------

@admin_router.get("/students/{student_id}/breakdown", response_model=FeeBreakdownOut)
def preview_breakdown(
    student_id: int,
    academic_year_id: Optional[int] = Query(None, description="Defaults to the current year."),
    currency: Optional[str] = Query(
        None, description="Show in this currency, e.g. AED. Defaults to the base currency."
    ),
    program: Program = Query(Program.LMS),
    _: UserOut = Depends(require_admin),
):
    """
    [Admin Only] What this student would be charged, fully derived and without billing them.

    The same computation the payment page and the invoice generator use. Read
    `discounts_not_applied` when a concession is missing - it says why each rule did not
    fire, e.g. "household has 1 child; rule needs 2", rather than leaving you to guess.
    """
    student = require_document(firestore_users, student_id, "Student")
    return FeeBreakdownOut(
        **finance.compute_breakdown(
            student, academic_year_id, program.value, currency=currency
        )
    )


@admin_router.post("/students/{student_id}/invoice", response_model=FeeInvoiceOut,
                   status_code=status.HTTP_201_CREATED)
def generate_invoice(
    student_id: int,
    academic_year_id: Optional[int] = Query(None),
    currency: Optional[str] = Query(
        None, description="Show in this currency, e.g. AED. Defaults to the base currency."
    ),
    program: Program = Query(Program.LMS),
    admin: UserOut = Depends(require_admin),
):
    """
    [Admin Only] Build or rebuild a student's draft invoice from the current fee rules.

    One invoice per student per year: regenerating overwrites the draft rather than leaving
    a second bill behind. Payments already recorded are preserved across a rebuild.

    Refused once the invoice has been issued - cancel and reissue if the fees have genuinely
    changed, so there is a trail.
    """
    invoice = billing.generate_invoice(
        student_id, academic_year_id, admin.id, program.value, currency=currency
    )
    return FeeInvoiceOut(**billing.present_invoice(invoice, program.value))


@admin_router.get("/invoices", response_model=List[FeeInvoiceOut])
def list_invoices(
    academic_year_id: Optional[int] = Query(None),
    status_filter: Optional[str] = Query(None, alias="status"),
    student_id: Optional[int] = Query(None),
    overdue_only: bool = Query(False, description="Only invoices with an overdue instalment."),
    program: Program = Query(Program.LMS),
    _: UserOut = Depends(require_admin),
):
    """[Admin Only] Invoices, with balances and overdue state resolved against today."""
    invoices = firestore_fee_invoices.list_all()

    if academic_year_id is not None:
        invoices = [i for i in invoices
                    if str(i.get("academic_year_id")) == str(academic_year_id)]
    if status_filter:
        invoices = [i for i in invoices if i.get("status") == status_filter.upper()]
    if student_id is not None:
        invoices = [i for i in invoices if str(i.get("student_id")) == str(student_id)]

    presented = [billing.present_invoice(i, program.value) for i in invoices]
    if overdue_only:
        presented = [i for i in presented if i["is_overdue"]]

    presented.sort(key=lambda i: str(i.get("created_at") or ""), reverse=True)
    return [FeeInvoiceOut(**i) for i in presented]


@admin_router.get("/invoices/{invoice_id}", response_model=FeeInvoiceOut)
def get_invoice(
    invoice_id: str, program: Program = Query(Program.LMS),
    _: UserOut = Depends(require_admin),
):
    """[Admin Only] One invoice in full, with its payment history."""
    return FeeInvoiceOut(
        **billing.present_invoice(billing.require_invoice(invoice_id), program.value)
    )


@admin_router.post("/invoices/{invoice_id}/issue", response_model=FeeInvoiceOut)
def issue_invoice(
    invoice_id: str, program: Program = Query(Program.LMS),
    admin: UserOut = Depends(require_admin),
):
    """
    [Admin Only] Issue an invoice: assign its number and freeze its figures.

    After this the totals stop moving, whatever happens to the fee structure or the tax rate
    afterwards. That is the point - a bill that changes after it was sent is not a bill.
    """
    invoice = billing.require_invoice(invoice_id)
    issued = billing.issue_invoice(invoice, admin.id, program.value)
    return FeeInvoiceOut(**billing.present_invoice(issued, program.value))


@admin_router.post("/invoices/{invoice_id}/cancel", response_model=FeeInvoiceOut)
def cancel_invoice(
    invoice_id: str,
    reason: Optional[str] = Query(None),
    program: Program = Query(Program.LMS),
    admin: UserOut = Depends(require_admin),
):
    """
    [Admin Only] Cancel an invoice, keeping it visible.

    Refused once money has been taken against it: the school would be holding funds against
    nothing. Refund or adjust the payments first.
    """
    invoice = billing.require_invoice(invoice_id)
    cancelled = billing.cancel_invoice(invoice, reason, admin.id)
    return FeeInvoiceOut(**billing.present_invoice(cancelled, program.value))


@admin_router.post("/invoices/{invoice_id}/payments", response_model=FeeInvoiceOut)
def record_payment(
    invoice_id: str, payload: PaymentRecord,
    program: Program = Query(Program.LMS),
    admin: UserOut = Depends(require_admin),
):
    """
    [Admin Only] Record money received - cash, cheque, transfer, card or an adjustment.

    Name an `instalment_label` to apply it to one instalment; omit it and the payment settles
    the oldest unpaid instalments first, which is what a school means by "paying off the
    fees" and what keeps the late-fee calculation honest.

    Overpayment is refused. Taking more than was billed almost always means the bill is wrong
    or a zero was typed twice, and catching that at entry is far cheaper than at
    reconciliation.
    """
    invoice = billing.require_invoice(invoice_id)
    updated = billing.record_payment(invoice, payload, admin.id)
    return FeeInvoiceOut(**billing.present_invoice(updated, program.value))


@admin_router.post("/students/{student_id}/collect", response_model=FeeInvoiceOut)
def collect_fee(
    student_id: int, payload: FeeCollect,
    program: Program = Query(Program.LMS),
    admin: UserOut = Depends(require_admin),
):
    """
    [Admin Only] Collect a fee at the counter - one call from "student pays" to "recorded".

    The year's invoice is built and issued if the student has not been billed yet, then the
    payment is recorded on it exactly as `POST /invoices/{id}/payments` would. Name an
    `instalment_label` (e.g. "Term 1") to apply the money to one instalment; omit it to
    settle oldest-first. 400 when no fee structure applies to the student or the amount is
    more than they owe.
    """
    updated = billing.collect_payment(student_id, payload, admin.id, program.value)
    return FeeInvoiceOut(**billing.present_invoice(updated, program.value))


@admin_router.post("/invoices/{invoice_id}/waive", response_model=FeeInvoiceOut)
def waive_instalment(
    invoice_id: str, payload: WaiveInstalmentRequest,
    program: Program = Query(Program.LMS),
    admin: UserOut = Depends(require_admin),
):
    """
    [Admin Only] Write off one instalment.

    Reduces the invoice total rather than merely marking the line settled: the parent is
    genuinely not being asked for it, and an arrears report that still counts it is wrong.
    The reason is mandatory - a waiver with no stated cause is indistinguishable from a
    mistake when somebody audits it next year.
    """
    invoice = billing.require_invoice(invoice_id)
    updated = billing.waive_instalment(invoice, payload.label, payload.reason, admin.id)
    return FeeInvoiceOut(**billing.present_invoice(updated, program.value))


@admin_router.post("/invoices/{invoice_id}/late-fees", response_model=FeeInvoiceOut)
def apply_late_fees(
    invoice_id: str, program: Program = Query(Program.LMS),
    _: UserOut = Depends(require_admin),
):
    """
    [Admin Only] Charge the configured late fee on every overdue instalment.

    Idempotent per instalment - a line already carrying a late fee is skipped - so running it
    twice in a day does not charge twice. A no-op when late fees are disabled in settings.
    """
    invoice = billing.require_invoice(invoice_id)
    updated = billing.apply_late_fees(invoice, program.value)
    return FeeInvoiceOut(**billing.present_invoice(updated, program.value))


# ---------------------------------------------------------------------------------------
# The gateway seam
# ---------------------------------------------------------------------------------------

@admin_router.post("/invoices/{invoice_id}/intents", response_model=PaymentIntentOut,
                   status_code=status.HTTP_201_CREATED)
def create_intent(
    invoice_id: str, payload: PaymentIntentCreate,
    program: Program = Query(Program.LMS),
    admin: UserOut = Depends(require_admin),
):
    """
    [Admin Only] Start a gateway payment.

    **No provider is wired yet.** The intent is recorded with an idempotency key and the
    amount is validated against the outstanding balance, but `checkout_url` comes back null
    and the status stays CREATED until an adapter is written. The payment page can already
    render the amount and a disabled "pay online" button from `gateway_enabled`.
    """
    invoice = billing.require_invoice(invoice_id)
    intent = billing.create_intent(
        invoice, payload.amount, admin.id, payload.instalment_label, program.value
    )
    return PaymentIntentOut(**intent)


@admin_router.post("/intents/{intent_id}/callback", response_model=PaymentIntentOut)
def gateway_callback(
    intent_id: int, payload: GatewayCallback, _: UserOut = Depends(require_admin),
):
    """
    [Admin Only] Apply a provider's result to an intent, crediting the invoice on success.

    Admin-guarded for now because no provider is wired; a real webhook endpoint will
    authenticate by provider signature instead and call the same service function.

    Safe to deliver more than once: an intent already in a terminal state is returned
    unchanged rather than re-applied, so a retried webhook credits exactly one payment. That
    guarantee is the one part of a payment integration that cannot be added afterwards.
    """
    intent = billing.require_intent(intent_id)
    settled = billing.settle_intent(
        intent,
        getattr(payload.status, "value", payload.status),
        payload.provider_reference,
        payload.payload,
        payload.failure_reason,
    )
    return PaymentIntentOut(**settled)


@admin_router.get("/invoices/{invoice_id}/intents", response_model=List[PaymentIntentOut])
def list_intents(invoice_id: str, _: UserOut = Depends(require_admin)):
    """
    [Admin Only] Every gateway attempt against an invoice, including the failed ones.

    Separate from the invoice's `payments` list on purpose: a payment is money the school
    has, while an intent is somebody pressing a button, and most of those fail or are
    abandoned. Failed attempts must never reach a receipt.
    """
    return [PaymentIntentOut(**i) for i in billing.intents_for_invoice(invoice_id)]


# ---------------------------------------------------------------------------------------
# Receipts: money with no invoice on this system
#
# Fees paid before the fee module existed - the admission fee of every student who was
# already on the roll - entered afterwards as opening balances, so the collections report
# carries them. Never a substitute for an invoice payment: money against a bill goes
# through /invoices/{id}/payments or /students/{id}/collect.
# ---------------------------------------------------------------------------------------

@admin_router.post("/receipts", response_model=FeeReceiptOut, status_code=status.HTTP_201_CREATED)
def record_receipt(
    payload: FeeReceiptCreate,
    program: Program = Query(Program.LMS),
    admin: UserOut = Depends(require_admin),
):
    """
    [Admin Only] Records money received with no invoice on this system - typically an
    admission fee paid before the system was in use. It appears in the collections report
    with `source` = `OPENING_BALANCE`. It does not change what the student owes.
    """
    receipt = receipt_service.record_receipt(payload, admin.id, program.value)
    return receipt_service.present_receipt(receipt)


@admin_router.get("/receipts", response_model=List[FeeReceiptOut])
def list_receipts(
    student_id: Optional[int] = Query(None),
    academic_year_id: Optional[int] = Query(None),
    fee_head_id: Optional[int] = Query(None),
    program: Program = Query(Program.LMS),
    _: UserOut = Depends(require_admin),
):
    """[Admin Only] Receipts, newest first, optionally for one student, year or fee head."""
    return [
        receipt_service.present_receipt(r)
        for r in receipt_service.list_receipts(program.value, student_id, academic_year_id, fee_head_id)
    ]


@admin_router.delete("/receipts/{receipt_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_receipt(receipt_id: int, _: UserOut = Depends(require_admin)):
    """[Admin Only] Removes a receipt entered in error. There is no reversal: it is erased."""
    receipt_service.delete_receipt(receipt_service.require_receipt(receipt_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------------------
# Reports: the roll, the dues, the collections
#
# Computed on request from the invoices and the roster, never stored, so they cannot
# disagree with the invoice screen. Each has a CSV twin under /reports/export.
# ---------------------------------------------------------------------------------------

@admin_router.get("/reports/students")
def fee_roll_report(
    academic_year_id: Optional[int] = Query(None, description="Defaults to the current year."),
    class_id: Optional[int] = Query(None),
    status_filter: Optional[str] = Query(
        None, alias="status",
        description="NOT_BILLED | DRAFT | ISSUED | PARTIALLY_PAID | PAID | OVERDUE | DUE",
    ),
    as_of: Optional[date] = Query(None, description="Overdue state as of this date."),
    program: Program = Query(Program.LMS),
    _: UserOut = Depends(require_admin),
):
    """
    [Admin Only] Every student in the year with where their bill stands.

    Unbilled students are listed too, with `expected_total` - what the current rules would
    charge them - so nobody is missing from the list just because no invoice exists yet.
    `amount_due` is the figure to chase either way. `summary` is computed from exactly the
    rows returned, so the totals at the top always agree with the list beneath.
    """
    return reports.student_roll(
        academic_year_id, program.value, class_id, status_filter, as_of,
    )


@admin_router.get("/reports/dues")
def fee_dues_report(
    academic_year_id: Optional[int] = Query(None),
    class_id: Optional[int] = Query(None),
    as_of: Optional[date] = Query(None),
    program: Program = Query(Program.LMS),
    _: UserOut = Depends(require_admin),
):
    """
    [Admin Only] Whoever still owes, largest overdue balance first, with the balance aged
    into not-yet-due, 0-30, 31-60, 61-90 and 90+ days. The list to ring round from.
    """
    return reports.dues_report(academic_year_id, program.value, class_id, as_of)


@admin_router.get("/reports/collections")
def fee_collections_report(
    from_date: date = Query(..., description="First day of the period."),
    to_date: date = Query(..., description="Last day of the period."),
    academic_year_id: Optional[int] = Query(None, description="Any year when omitted."),
    class_id: Optional[int] = Query(None),
    method: Optional[str] = Query(None, description="CASH | BANK_TRANSFER | CHEQUE | CARD | UPI | ONLINE | ADJUSTMENT | OTHER"),
    head_id: Optional[int] = Query(
        None, description="Only payments that settled this fee head, e.g. the admission fee.",
    ),
    program: Program = Query(Program.LMS),
    _: UserOut = Depends(require_admin),
):
    """
    [Admin Only] Every payment received in the period, newest first, with totals by method,
    by day, by class and by fee head. The receipts side of the ledger, for reconciling
    against the cash book and the bank.

    Each payment is split by what it settled - `heads` on the row, `by_head`,
    `admission_fees` and `other_fees` on the summary - by replaying the invoice's own
    allocation, so the admission fees collected can be read apart from the tuition.
    """
    return reports.collections_report(
        from_date, to_date, academic_year_id, program.value, class_id, method, head_id,
    )


@admin_router.get("/reports/export")
def fee_reports_export(
    view: str = Query("students", description="students | dues | collections"),
    academic_year_id: Optional[int] = Query(None),
    class_id: Optional[int] = Query(None),
    status_filter: Optional[str] = Query(None, alias="status"),
    as_of: Optional[date] = Query(None),
    from_date: Optional[date] = Query(None),
    to_date: Optional[date] = Query(None),
    method: Optional[str] = Query(None),
    head_id: Optional[int] = Query(None),
    program: Program = Query(Program.LMS),
    _: UserOut = Depends(require_admin),
):
    """
    [Admin Only] The same three reports as CSV, with the same filters.

    UTF-8 with a BOM so Excel on Windows renders names correctly rather than mangling them.
    """
    choice = (view or "students").strip().lower()
    if choice == "collections":
        if not from_date or not to_date:
            raise HTTPException(status_code=400, detail="from_date and to_date are required.")
        report = reports.collections_report(
            from_date, to_date, academic_year_id, program.value, class_id, method, head_id,
        )
        body = reports.collections_csv(report)
        name = f"fee-collections-{from_date}-to-{to_date}.csv"
    elif choice == "dues":
        report = reports.dues_report(academic_year_id, program.value, class_id, as_of)
        body = reports.roll_csv(report)
        name = f"fee-dues-{report['as_of']}.csv"
    elif choice == "students":
        report = reports.student_roll(academic_year_id, program.value, class_id, status_filter, as_of)
        body = reports.roll_csv(report)
        name = f"fee-students-{report['as_of']}.csv"
    else:
        raise HTTPException(status_code=400, detail="view must be one of: students, dues, collections.")

    return Response(
        content="\ufeff" + body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


# ---------------------------------------------------------------------------------------
# Student and parent views
# ---------------------------------------------------------------------------------------

@student_router.get("/me", response_model=FeeBreakdownOut)
def my_fees(
    academic_year_id: Optional[int] = Query(None),
    currency: Optional[str] = Query(
        None, description="Show in this currency, e.g. AED. Defaults to the base currency."
    ),
    program: Program = Query(Program.LMS),
    student: UserOut = Depends(require_student),
):
    """
    [Student] What I owe: fees, charges, tax, concessions and the instalment schedule.

    **The payment page's data**, from the same function the bursar's preview and the invoice
    generator use.

    Pass `currency` to switch - `available_currencies` on the response is what the switcher
    should offer. Switching is not just a conversion: each currency carries its own tax and
    convenience charge, so the total changes by more than the exchange rate. `exchange_rate`
    and `base_currency` are returned so the page can show the conversion rather than leaving
    a payer wondering why the number moved.
    """
    profile = require_document(firestore_users, student.id, "Student")
    return FeeBreakdownOut(
        **finance.compute_breakdown(
            profile, academic_year_id, program.value, currency=currency
        )
    )


@student_router.get("/me/invoices", response_model=List[FeeInvoiceOut])
def my_invoices(
    program: Program = Query(Program.LMS), student: UserOut = Depends(require_student)
):
    """[Student] My invoices, newest year first."""
    return [
        FeeInvoiceOut(**billing.present_invoice(i, program.value))
        for i in billing.invoices_for_student(student.id)
    ]


# ---------------------------------------------------------------------------------------
# The checkout: a student, or a parent, starting a payment on an invoice of their own
#
# The admin intent endpoint above is the office's; these are the payer's. Same service
# function, one extra rule - the invoice has to be theirs - and a 404 rather than a 403 for
# one that is not, so an invoice id cannot be probed for existence.
# ---------------------------------------------------------------------------------------

def _invoice_of(invoice_id, student_id) -> dict:
    invoice = billing.require_invoice(invoice_id)
    if int(invoice.get("student_id") or -1) != int(student_id):
        raise HTTPException(status_code=404, detail=f"Invoice {invoice_id} not found")
    return invoice


@student_router.post("/me/invoices/{invoice_id}/intents", response_model=PaymentIntentOut,
                     status_code=status.HTTP_201_CREATED)
def start_my_payment(
    invoice_id: str, payload: PaymentIntentCreate,
    program: Program = Query(Program.LMS),
    student: UserOut = Depends(require_student),
):
    """
    [Student] Start paying one of my invoices - the checkout page's "Pay" button.

    `method` is what the payer chose. For UPI, card, net banking or a wallet the intent
    waits for a gateway adapter and `checkout_url` says where to send them (null while no
    provider is connected, with `detail` saying so). For a bank transfer or a payment at the
    office the response is the `reference` to quote; the office records the money and the
    invoice updates. Refused for a draft, a cancelled or a settled invoice, and for more than
    is outstanding.
    """
    invoice = _invoice_of(invoice_id, student.id)
    intent = billing.create_intent(
        invoice, payload.amount, student.id, payload.instalment_label, program.value,
        getattr(payload.method, "value", payload.method),
    )
    return PaymentIntentOut(**intent)


@student_router.get("/me/invoices/{invoice_id}/intents", response_model=List[PaymentIntentOut])
def my_payment_attempts(invoice_id: str, student: UserOut = Depends(require_student)):
    """[Student] Every payment I have started on this invoice, newest first, failures included."""
    _invoice_of(invoice_id, student.id)
    return [PaymentIntentOut(**i) for i in billing.intents_for_invoice(invoice_id)]


@parent_router.get("/children/{student_id}", response_model=FeeBreakdownOut)
def child_fees(
    student_id: int,
    academic_year_id: Optional[int] = Query(None),
    currency: Optional[str] = Query(
        None, description="Show in this currency, e.g. AED. Defaults to the base currency."
    ),
    program: Program = Query(Program.LMS),
    parent: UserOut = Depends(require_parent),
):
    """
    [Parent] A child's fee breakdown.

    Gated on the link's `may_view_fees` flag, which is off unless an admin granted it - the
    one aspect a non-paying guardian has no business reading.
    """
    family_service.assert_may_view(parent, student_id, "fees")
    profile = require_document(firestore_users, student_id, "Student")
    return FeeBreakdownOut(
        **finance.compute_breakdown(
            profile, academic_year_id, program.value, currency=currency
        )
    )


@parent_router.get("/children/{student_id}/invoices", response_model=List[FeeInvoiceOut])
def child_invoices(
    student_id: int,
    program: Program = Query(Program.LMS),
    parent: UserOut = Depends(require_parent),
):
    """[Parent] A child's invoices and payment history."""
    family_service.assert_may_view(parent, student_id, "fees")
    return [
        FeeInvoiceOut(**billing.present_invoice(i, program.value))
        for i in billing.invoices_for_student(student_id)
    ]


@parent_router.post("/children/{student_id}/invoices/{invoice_id}/intents",
                    response_model=PaymentIntentOut, status_code=status.HTTP_201_CREATED)
def start_child_payment(
    student_id: int, invoice_id: str, payload: PaymentIntentCreate,
    program: Program = Query(Program.LMS),
    parent: UserOut = Depends(require_parent),
):
    """
    [Parent] Start paying a child's school invoice. Gated on the link's `may_view_fees`,
    like every other fee read; the rules are those of the student's own checkout.
    """
    family_service.assert_may_view(parent, student_id, "fees")
    invoice = _invoice_of(invoice_id, student_id)
    intent = billing.create_intent(
        invoice, payload.amount, parent.id, payload.instalment_label, program.value,
        getattr(payload.method, "value", payload.method),
    )
    return PaymentIntentOut(**intent)


@parent_router.get("/children/{student_id}/invoices/{invoice_id}/intents",
                   response_model=List[PaymentIntentOut])
def child_payment_attempts(
    student_id: int, invoice_id: str, parent: UserOut = Depends(require_parent),
):
    """[Parent] Every payment started on a child's school invoice, newest first."""
    family_service.assert_may_view(parent, student_id, "fees")
    _invoice_of(invoice_id, student_id)
    return [PaymentIntentOut(**i) for i in billing.intents_for_invoice(invoice_id)]


# ---------------------------------------------------------------------------------------
# Tuition fees, for a parent
#
# The tuition programme bills its own way - a count of conducted classes rather than a fee
# structure - so it has its own breakdown. What a parent needs is identical either way, which
# is why it sits on the same router behind the same `may_view_fees` gate rather than on the
# tuition routers, where a parent has no other business.
# ---------------------------------------------------------------------------------------

@parent_router.get("/tuition/children/{student_id}")
def child_tuition_fees(
    student_id: int,
    period_start: date = Query(..., description="First day of the billing period."),
    period_end: date = Query(..., description="Last day of the billing period."),
    currency: Optional[str] = Query(
        None, description="Show in this currency, e.g. AED."
    ),
    parent: UserOut = Depends(require_parent),
):
    """
    [Parent] A child's tuition fees for a period - the payment page.

    Gated on the link's `may_view_fees`, exactly as the school fee view is. Each line shows
    the classes it was priced from, then the tax and convenience charge at the chosen
    currency's own rates.
    """
    family_service.assert_may_view(parent, student_id, "fees")

    from app.services.tuition import fees as tuition_fees

    return tuition_fees.compute_breakdown(
        student_id, period_start, period_end, currency=currency
    )


@parent_router.get("/tuition/children/{student_id}/invoices")
def child_tuition_invoices(
    student_id: int,
    parent: UserOut = Depends(require_parent),
):
    """
    [Parent] A child's issued tuition invoices, newest period first.

    Drafts are excluded: a draft is the office's working copy, re-priced every time it is
    regenerated, and showing one invites a family to pay a figure that may still change.
    """
    family_service.assert_may_view(parent, student_id, "fees")

    from app.core.enums import InvoiceStatus
    from app.services.tuition import fees as tuition_fees

    invoices = [
        tuition_fees.present_invoice(invoice)
        for invoice in tuition_fees.list_invoices(student_id=student_id)
        if invoice.get("status") != InvoiceStatus.DRAFT.value
    ]
    invoices.sort(key=lambda i: str(i.get("period_start") or ""), reverse=True)
    return invoices


@parent_router.post("/tuition/children/{student_id}/invoices/{invoice_id}/intents",
                    response_model=PaymentIntentOut, status_code=status.HTTP_201_CREATED)
def start_child_tuition_payment(
    student_id: int, invoice_id: str, payload: PaymentIntentCreate,
    parent: UserOut = Depends(require_parent),
):
    """[Parent] Start paying a child's tuition invoice. Same rules as the student's checkout."""
    from app.services.tuition import fees as tuition_fees

    family_service.assert_may_view(parent, student_id, "fees")
    invoice = tuition_fees.require_invoice(invoice_id)
    if int(invoice.get("student_id") or -1) != int(student_id):
        raise HTTPException(status_code=404, detail=f"Invoice {invoice_id} not found")
    intent = billing.create_intent(
        invoice, payload.amount, parent.id, None, Program.TUITION.value,
        getattr(payload.method, "value", payload.method),
    )
    return PaymentIntentOut(**intent)


@parent_router.get("/tuition/children/{student_id}/invoices/{invoice_id}/intents",
                   response_model=List[PaymentIntentOut])
def child_tuition_payment_attempts(
    student_id: int, invoice_id: str, parent: UserOut = Depends(require_parent),
):
    """[Parent] Every payment started on a child's tuition invoice, newest first."""
    from app.services.tuition import fees as tuition_fees

    family_service.assert_may_view(parent, student_id, "fees")
    invoice = tuition_fees.require_invoice(invoice_id)
    if int(invoice.get("student_id") or -1) != int(student_id):
        raise HTTPException(status_code=404, detail=f"Invoice {invoice_id} not found")
    return [PaymentIntentOut(**i) for i in billing.intents_for_invoice(invoice_id)]
