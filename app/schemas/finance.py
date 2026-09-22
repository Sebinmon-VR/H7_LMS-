"""
Request and response shapes for school fees, invoices and payments.

`FeeBreakdownOut` is the one to read first: it is what the payment page renders, and it is
deliberately self-explaining. Every figure on it can be traced to the line and the rule that
produced it, because a fee screen that shows only a total generates a phone call for every
parent who expected a different number.
"""

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.enums import (
    AcademicTerm, ChargeKind, DiscountBasis, DiscountValueType, FeeFrequency, InvoiceStatus,
    PaymentIntentStatus, PaymentMethod, Program,
    GatewayMethod,
)


# ---------------------------------------------------------------------------------------
# Fee heads
# ---------------------------------------------------------------------------------------

class FeeHeadCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120, description='e.g. "Tuition Fee"')
    code: str = Field(..., min_length=1, max_length=30)
    kind: ChargeKind = ChargeKind.FEE
    frequency: FeeFrequency = FeeFrequency.ANNUAL
    default_amount: float = Field(0.0, ge=0)

    is_taxable: bool = False
    tax_percent: float | None = Field(
        None, ge=0, le=100,
        description="Overrides the program's default rate for this head only.",
    )
    is_discountable: bool = Field(
        True, description="Turn off for statutory charges the school only passes on."
    )
    is_admission_charge: bool = Field(
        False, description="Charged once at joining; waived by categories that say so."
    )

    program: Program = Program.LMS
    description: str | None = Field(None, max_length=1000)
    is_active: bool = True
    sort_order: int = 0


class FeeHeadUpdate(BaseModel):
    """Partial update; omitted fields are left unchanged."""
    name: str | None = Field(None, min_length=1, max_length=120)
    code: str | None = Field(None, min_length=1, max_length=30)
    kind: ChargeKind | None = None
    frequency: FeeFrequency | None = None
    default_amount: float | None = Field(None, ge=0)
    is_taxable: bool | None = None
    tax_percent: float | None = Field(None, ge=0, le=100)
    is_discountable: bool | None = None
    is_admission_charge: bool | None = None
    description: str | None = Field(None, max_length=1000)
    is_active: bool | None = None
    sort_order: int | None = None


class FeeHeadOut(BaseModel):
    id: int
    name: str
    code: str
    kind: ChargeKind
    frequency: FeeFrequency
    default_amount: float
    is_taxable: bool
    tax_percent: float | None = None
    is_discountable: bool = True
    is_admission_charge: bool = False
    program: str
    description: str | None = None
    is_active: bool = True
    sort_order: int = 0
    created_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------------------
# Fee structures
# ---------------------------------------------------------------------------------------

class FeeStructureItem(BaseModel):
    fee_head_id: int
    amount: float = Field(..., ge=0)
    # Overrides the head's own frequency and name for this structure only, which is how one
    # "Transport" head serves twelve routes at different prices.
    frequency: FeeFrequency | None = None
    label: str | None = Field(None, max_length=120)


class FeeStructureCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=150)
    academic_year_id: int
    items: list[FeeStructureItem] = Field(..., min_length=1)

    class_ids: list[int] = Field(
        default_factory=list, description="Empty means every class."
    )
    admission_category_ids: list[int] = Field(
        default_factory=list, description="Empty means every category."
    )
    program: Program = Program.LMS
    currency: str | None = Field(None, max_length=8)
    priority: int = Field(0, description="Breaks a tie between equally specific structures.")
    is_active: bool = True
    notes: str | None = Field(None, max_length=2000)


class FeeStructureUpdate(BaseModel):
    """Partial update; omitted fields are left unchanged."""
    name: str | None = Field(None, min_length=1, max_length=150)
    items: list[FeeStructureItem] | None = None
    class_ids: list[int] | None = None
    admission_category_ids: list[int] | None = None
    currency: str | None = Field(None, max_length=8)
    priority: int | None = None
    is_active: bool | None = None
    notes: str | None = Field(None, max_length=2000)


class FeeStructureOut(BaseModel):
    id: int
    name: str
    academic_year_id: int
    academic_year_name: str | None = None
    items: list[dict[str, Any]] = Field(default_factory=list)
    class_ids: list[int] = Field(default_factory=list)
    class_names: list[str] = Field(default_factory=list)
    admission_category_ids: list[int] = Field(default_factory=list)
    admission_category_names: list[str] = Field(default_factory=list)
    program: str
    currency: str
    priority: int = 0
    is_active: bool = True
    notes: str | None = None
    # The structure's own total, before any student-specific concession.
    gross_total: float = 0.0
    created_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------------------
# Instalment plans
# ---------------------------------------------------------------------------------------

class InstalmentLine(BaseModel):
    label: str = Field(..., min_length=1, max_length=80, description='e.g. "Term 1"')
    percent: float | None = Field(None, ge=0, le=100)
    amount: float | None = Field(None, ge=0)
    # Ties the instalment to one of the year's terms: it then falls due at that term's
    # start plus the configured due-days, whatever dates the year is later edited to, and
    # a one-time charge such as the admission fee lands whole in the Term 1 instalment.
    term: AcademicTerm | None = Field(None, description="TERM_1 or TERM_2")
    due_date: date | None = None
    due_after_days: int | None = Field(
        None, ge=0,
        description="Days after the year starts. Used when due_date is absent, so one plan "
                    "serves every year.",
    )

    @model_validator(mode="after")
    def _one_value_and_one_date(self):
        if self.percent is None and self.amount is None:
            raise ValueError("Each instalment needs either 'percent' or 'amount'.")
        if self.percent is not None and self.amount is not None:
            raise ValueError("Give an instalment either 'percent' or 'amount', not both.")
        if self.due_date is None and self.due_after_days is None and self.term is None:
            raise ValueError("Each instalment needs a 'term', a 'due_date' or 'due_after_days'.")
        return self


class InstalmentPlanCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=150)
    academic_year_id: int
    instalments: list[InstalmentLine] = Field(..., min_length=1)

    class_ids: list[int] = Field(default_factory=list)
    admission_category_ids: list[int] = Field(default_factory=list)
    structure_id: int | None = Field(
        None, description="Tie the plan to one structure; omit to match by class/category."
    )
    program: Program = Program.LMS
    grace_days: int | None = Field(None, ge=0)
    is_default: bool = False
    is_active: bool = True

    @model_validator(mode="after")
    def _percentages_total_100(self):
        """
        Refuses a plan that does not collect the whole fee.

        Checked here as well as in the service because this is the cheapest place to catch
        it. A plan totalling 90% under-bills every student on it, silently, for a year.
        """
        percents = [i.percent for i in self.instalments if i.percent is not None]
        if percents and abs(sum(percents) - 100.0) > 0.01:
            raise ValueError(
                f"Instalment percentages total {sum(percents):g}%, not 100%."
            )
        return self


class InstalmentPlanUpdate(BaseModel):
    """Partial update; omitted fields are left unchanged."""
    name: str | None = Field(None, min_length=1, max_length=150)
    instalments: list[InstalmentLine] | None = None
    class_ids: list[int] | None = None
    admission_category_ids: list[int] | None = None
    structure_id: int | None = None
    grace_days: int | None = Field(None, ge=0)
    is_default: bool | None = None
    is_active: bool | None = None


class InstalmentPlanOut(BaseModel):
    id: int
    name: str
    academic_year_id: int
    instalments: list[dict[str, Any]] = Field(default_factory=list)
    class_ids: list[int] = Field(default_factory=list)
    admission_category_ids: list[int] = Field(default_factory=list)
    structure_id: int | None = None
    program: str
    grace_days: int | None = None
    is_default: bool = False
    is_active: bool = True
    created_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------------------
# Discount rules
# ---------------------------------------------------------------------------------------

class DiscountRuleCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=150,
                      description='e.g. "Second child concession"')
    basis: DiscountBasis
    value_type: DiscountValueType = DiscountValueType.PERCENT
    value: float = Field(..., ge=0)

    academic_year_id: int | None = None
    program: Program = Program.LMS

    applies_from_nth: int = Field(
        2, ge=1,
        description="SIBLING: from the nth child. MULTI_REGISTRATION: from the nth subject.",
    )
    min_count: int | None = Field(None, ge=1)
    admission_category_ids: list[int] = Field(default_factory=list)
    class_ids: list[int] = Field(default_factory=list)
    student_ids: list[int] = Field(
        default_factory=list, description="Required for SCHOLARSHIP and MANUAL."
    )
    fee_head_ids: list[int] = Field(
        default_factory=list, description="Empty means every discountable head."
    )

    max_amount: float | None = Field(None, ge=0, description="Caps the concession in currency.")
    is_stackable: bool = Field(
        False,
        description="Off means it competes with other rules and only the best applies.",
    )
    priority: int = 0
    valid_from: date | None = None
    valid_until: date | None = None
    is_active: bool = True
    notes: str | None = Field(None, max_length=2000)

    @model_validator(mode="after")
    def _basis_has_what_it_needs(self):
        if self.basis in (DiscountBasis.SCHOLARSHIP, DiscountBasis.MANUAL) and not self.student_ids:
            raise ValueError(
                f"A {self.basis.value} discount must name the students it is awarded to "
                "in 'student_ids'."
            )
        if self.basis == DiscountBasis.CATEGORY and not self.admission_category_ids:
            raise ValueError(
                "A CATEGORY discount must name the categories it applies to."
            )
        if self.value_type == DiscountValueType.PERCENT and self.value > 100:
            raise ValueError("A percentage discount cannot exceed 100%.")
        if self.valid_until and self.valid_from and self.valid_until < self.valid_from:
            raise ValueError("valid_until must fall on or after valid_from.")
        return self


class DiscountRuleUpdate(BaseModel):
    """Partial update; omitted fields are left unchanged."""
    name: str | None = Field(None, min_length=1, max_length=150)
    value_type: DiscountValueType | None = None
    value: float | None = Field(None, ge=0)
    applies_from_nth: int | None = Field(None, ge=1)
    min_count: int | None = Field(None, ge=1)
    admission_category_ids: list[int] | None = None
    class_ids: list[int] | None = None
    student_ids: list[int] | None = None
    fee_head_ids: list[int] | None = None
    max_amount: float | None = Field(None, ge=0)
    is_stackable: bool | None = None
    priority: int | None = None
    valid_from: date | None = None
    valid_until: date | None = None
    is_active: bool | None = None
    notes: str | None = Field(None, max_length=2000)


class DiscountRuleOut(BaseModel):
    id: int
    name: str
    basis: DiscountBasis
    value_type: DiscountValueType
    value: float
    academic_year_id: int | None = None
    program: str
    applies_from_nth: int = 2
    min_count: int | None = None
    admission_category_ids: list[int] = Field(default_factory=list)
    class_ids: list[int] = Field(default_factory=list)
    student_ids: list[int] = Field(default_factory=list)
    fee_head_ids: list[int] = Field(default_factory=list)
    max_amount: float | None = None
    is_stackable: bool = False
    priority: int = 0
    valid_from: date | None = None
    valid_until: date | None = None
    is_active: bool = True
    notes: str | None = None
    created_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------------------
# The breakdown - what the payment page renders
# ---------------------------------------------------------------------------------------

class BreakdownLine(BaseModel):
    fee_head_id: int
    name: str
    code: str | None = None
    kind: str
    frequency: str | None = None
    amount: float
    taxable: bool = False
    discountable: bool = True
    tax_percent: float | None = None
    discount_amount: float = 0.0
    tax_amount: float = 0.0
    net_amount: float = 0.0


class AppliedDiscount(BaseModel):
    rule_id: int | None = None
    name: str | None = None
    basis: str | None = None
    value_type: str | None = None
    value: float | None = None
    amount: float = 0.0
    stacked: bool = False


class SkippedDiscount(BaseModel):
    """
    A rule that did not fire, and why.

    Returned so an admin asking "why is this family not getting the sibling discount?" gets
    "household has 1 child; rule needs 2" instead of an empty list.
    """
    rule_id: int | None = None
    name: str | None = None
    basis: str | None = None
    reason: str | None = None


class InstalmentOut(BaseModel):
    label: str
    term: str | None = None
    # Which fee contributed what to this instalment: [{"name", "amount"}]. Present on the
    # default term split, so a bill can say "Term 1: tuition 15,000 + admission 10,000".
    components: list[dict[str, Any]] = Field(default_factory=list)
    due_date: date | None = None
    amount: float
    amount_paid: float = 0.0
    outstanding: float | None = None
    status: str
    is_overdue: bool = False
    days_overdue: int = 0
    late_fee_amount: float | None = None
    waived_reason: str | None = None


class UnmatchedStructure(BaseModel):
    """
    A fee structure that exists but does not apply to this student, and why.

    Rendered on the fee screen when nothing matched. Without it, "no fee structure applies"
    is a dead end - and the usual cause, a student whose account predates the admissions
    module and so carries no academic year, is invisible from that screen.
    """
    structure_id: int | None = None
    name: str | None = None
    reasons: list[str] = Field(default_factory=list)


class FeeBreakdownOut(BaseModel):
    """
    Everything a student owes, fully derived. The payment page's data.

    The totals relate to each other as:

        subtotal - discount_total + tax_total + convenience_total
          (+/- rounding_adjustment) = total_amount

    `charge_total` is the part of `subtotal` that is not teaching fees, shown separately so
    the page can list "Fees" and "Charges" as sections without recomputing anything.
    """
    student_id: int
    student_name: str | None = None
    academic_year_id: int

    # --- currency ---------------------------------------------------------------------
    # `currency` is what these figures are in. `base_currency` is what the fee structure was
    # entered in, and `exchange_rate` is how one became the other - both returned so a payer
    # switching currency can see the conversion rather than wondering why the number moved.
    # `available_currencies` is what the switcher should offer.
    currency: str
    currency_symbol: str | None = None
    base_currency: str | None = None
    exchange_rate: float = 1.0
    available_currencies: list[str] = Field(default_factory=list)
    # The switcher: every offered currency with its own charges, base marked `recommended`.
    recommended_currency: str | None = None
    currency_options: list[CurrencyOptionOut] = Field(default_factory=list)

    structure_id: int | None = None
    structure_name: str | None = None
    instalment_plan_id: int | None = None
    instalment_plan_name: str | None = None

    line_items: list[BreakdownLine] = Field(default_factory=list)
    discounts: list[AppliedDiscount] = Field(default_factory=list)
    discounts_not_applied: list[SkippedDiscount] = Field(default_factory=list)
    instalments: list[InstalmentOut] = Field(default_factory=list)

    subtotal: float = 0.0
    discount_total: float = 0.0
    taxable_base: float = 0.0
    tax_total: float = 0.0
    tax_label: str = "Tax"
    charge_total: float = 0.0
    convenience_total: float = 0.0
    total_amount: float = 0.0
    rounding_adjustment: float = 0.0

    gateway_enabled: bool = False
    gateway_provider: str | None = None
    # Populated only when nothing matched. Show these on the empty state.
    structures_not_applied: list[UnmatchedStructure] = Field(default_factory=list)
    detail: str | None = None


# ---------------------------------------------------------------------------------------
# Invoices and payments
# ---------------------------------------------------------------------------------------

class FeeInvoiceOut(BaseModel):
    id: str
    invoice_number: str | None = None
    student_id: int
    student_name: str | None = None
    admission_number: str | None = None
    academic_year_id: int
    academic_year_name: str | None = None
    structure_id: int | None = None
    instalment_plan_id: int | None = None
    status: InvoiceStatus
    currency: str
    currency_symbol: str | None = None
    base_currency: str | None = None
    # Frozen when the invoice is issued. A bill that re-converts at today's rate is a bill
    # whose total changes after it was sent.
    exchange_rate: float = 1.0
    available_currencies: list[str] = Field(default_factory=list)

    line_items: list[BreakdownLine] = Field(default_factory=list)
    discounts: list[AppliedDiscount] = Field(default_factory=list)
    instalments: list[InstalmentOut] = Field(default_factory=list)
    payments: list[dict[str, Any]] = Field(default_factory=list)

    subtotal: float = 0.0
    discount_total: float = 0.0
    taxable_base: float = 0.0
    tax_total: float = 0.0
    tax_label: str = "Tax"
    charge_total: float = 0.0
    convenience_total: float = 0.0
    late_fee_total: float = 0.0
    total_amount: float = 0.0
    amount_paid: float = 0.0
    amount_outstanding: float = 0.0
    is_overdue: bool = False

    issued_at: datetime | None = None
    due_date: date | None = None
    notes: str | None = None
    gateway_enabled: bool = False
    gateway_provider: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class PaymentRecord(BaseModel):
    amount: float = Field(..., gt=0)
    method: PaymentMethod = PaymentMethod.CASH
    reference: str | None = Field(
        None, max_length=120, description="Cheque number, UTR, receipt number."
    )
    paid_at: datetime | None = Field(None, description="Defaults to now.")
    instalment_label: str | None = Field(
        None, description="Apply to one instalment; omit to settle oldest-first."
    )
    note: str | None = Field(None, max_length=1000)


class FeeCollect(PaymentRecord):
    """
    `POST /admin/finance/students/{id}/collect` - the counter clerk's one step.

    A payment against a student, not an invoice: the year's invoice is built and issued if
    it does not exist yet, then the money is recorded on it. Same fields as a payment, plus
    which year - the current one unless said otherwise.
    """
    academic_year_id: int | None = Field(None, description="Defaults to the current year.")


class FeeReceiptCreate(BaseModel):
    """
    `POST /admin/finance/receipts` - money received with no invoice on this system.

    For fees paid before the fee module existed, the admission fee above all: the students
    already on the roll paid it at a counter years ago, and the collections report should
    say so. One fee head, one amount, one date. Recording it changes nothing about what the
    student owes - their category already says the charge does not apply to them.
    """
    student_id: int
    fee_head_id: int
    amount: float = Field(..., gt=0)
    paid_at: datetime | date = Field(..., description="When the money was received.")
    method: PaymentMethod = PaymentMethod.OTHER
    reference: str | None = Field(None, max_length=120)
    note: str | None = Field(None, max_length=1000)
    academic_year_id: int | None = Field(
        None, description="The year the fee belonged to. Defaults to the student's admission year.",
    )


class FeeReceiptOut(BaseModel):
    id: int
    program: str
    student_id: int
    student_name: str | None = None
    admission_number: str | None = None
    academic_year_id: int | None = None
    fee_head_id: int
    head_name: str | None = None
    is_admission_charge: bool = False
    amount: float
    currency: str
    paid_at: datetime
    method: str
    reference: str | None = None
    note: str | None = None
    # OPENING_BALANCE: received before this system, entered afterwards.
    source: str
    recorded_by: int | None = None
    recorded_at: datetime | None = None


class WaiveInstalmentRequest(BaseModel):
    label: str = Field(..., description="The instalment to write off.")
    reason: str = Field(
        ..., min_length=1, max_length=1000,
        description="Mandatory. A waiver with no stated cause cannot be audited.",
    )


class PaymentIntentCreate(BaseModel):
    amount: float = Field(..., gt=0)
    instalment_label: str | None = None
    # What the payer chose on the checkout page. A hint to the gateway adapter for the
    # online methods; for BANK_TRANSFER and OFFICE it is the record that a reference was
    # handed out for an offline payment the office should expect.
    method: GatewayMethod | None = Field(
        None, description="UPI | CARD | NET_BANKING | WALLET | BANK_TRANSFER | OFFICE",
    )


class PaymentIntentOut(BaseModel):
    id: int
    invoice_id: str
    student_id: int
    amount: float
    currency: str
    status: PaymentIntentStatus
    provider: str | None = None
    provider_reference: str | None = None
    checkout_url: str | None = None
    idempotency_key: str | None = None
    instalment_label: str | None = None
    requested_method: str | None = None
    # Which biller the invoice belongs to - LMS or TUITION - so a callback credits the right
    # collection. Frozen on the intent rather than re-derived from the invoice id's shape.
    program: str | None = None
    invoice_number: str | None = None
    # What the payer quotes at the office or on a transfer: "PAY-<id>".
    reference: str | None = None
    failure_reason: str | None = None
    completed_at: datetime | None = None
    created_at: datetime | None = None
    detail: str | None = None

    model_config = ConfigDict(from_attributes=True)


class GatewayCallback(BaseModel):
    """
    What a payment provider's webhook maps onto.

    Provider-neutral on purpose: no gateway is wired, and the adapter written later
    translates its own vocabulary into these fields rather than this shape being rewritten to
    match whichever provider is chosen.
    """
    status: PaymentIntentStatus
    provider_reference: str | None = None
    failure_reason: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------------------
# Settings - the charges and tax page
# ---------------------------------------------------------------------------------------

class FinanceSettingsOut(BaseModel):
    program: str
    currency: str
    tax_enabled: bool
    tax_percent: float
    tax_label: str
    tax_inclusive: bool
    late_fee_enabled: bool
    late_fee_percent: float
    late_fee_amount: float
    late_fee_grace_days: int
    convenience_percent: float
    convenience_amount: float
    invoice_prefix: str
    invoice_due_days: int
    rounding: str
    gateway_enabled: bool
    gateway_provider: str | None = None


class FinanceSettingsUpdate(BaseModel):
    """
    Partial update; omitted fields keep their current value.

    Sending `null` does not clear a setting - a form that defaults its untouched inputs to
    null would otherwise reset the school's tax rate every time somebody edited the late fee.
    """
    currency: str | None = Field(None, max_length=8)
    tax_enabled: bool | None = None
    tax_percent: float | None = Field(None, ge=0, le=100)
    tax_label: str | None = Field(None, max_length=40)
    tax_inclusive: bool | None = None
    late_fee_enabled: bool | None = None
    late_fee_percent: float | None = Field(None, ge=0, le=100)
    late_fee_amount: float | None = Field(None, ge=0)
    late_fee_grace_days: int | None = Field(None, ge=0)
    convenience_percent: float | None = Field(None, ge=0, le=100)
    convenience_amount: float | None = Field(None, ge=0)
    invoice_prefix: str | None = Field(None, max_length=10)
    invoice_due_days: int | None = Field(None, ge=0)
    rounding: str | None = Field(None, description="NONE, NEAREST, UP or DOWN")
    gateway_enabled: bool | None = None
    gateway_provider: str | None = Field(None, max_length=40)


# ---------------------------------------------------------------------------------------
# Currency settings
# ---------------------------------------------------------------------------------------

class CurrencyOptionOut(BaseModel):
    """
    One row of the payer's currency switcher.

    Not just a code: each currency carries its own tax and surcharge, and a payer choosing
    between INR and AED should see that AED adds VAT and a card fee *before* choosing it.
    `recommended` marks the base currency - the one the fees were set in and the only one
    with no conversion - so the page can preselect it and label it as suggested.

    `total_amount` and its parts are present on a fee breakdown, where the same classes have
    been priced in every offered currency; absent on the settings screen.
    """
    code: str
    symbol: str | None = None
    is_base: bool = False
    recommended: bool = False
    rate_from_base: float = 1.0
    rate_status: str | None = None
    tax_enabled: bool = False
    tax_percent: float = 0.0
    tax_label: str = "Tax"
    tax_inclusive: bool = False
    convenience_percent: float = 0.0
    convenience_amount: float = 0.0
    rounding: str = "NONE"
    # "GST 18% + 2% convenience" - ready to print beside the option.
    charges_summary: str | None = None
    subtotal: float | None = None
    tax_total: float | None = None
    convenience_total: float | None = None
    total_amount: float | None = None


class CurrencyConfigOut(BaseModel):
    """
    One currency the school offers, with its rate and its own charge rules.

    `overrides` names the keys this currency sets for itself; everything else fell through to
    the program's finance settings. The admin screen uses it to show which fields are
    currency-specific rather than inherited.
    """
    code: str
    symbol: str | None = None
    is_base: bool = False
    enabled: bool = True
    # Units of this currency per one unit of the base. The base is always exactly 1.
    rate_from_base: float = 1.0
    # The school's own configured rate, kept even under LIVE because it is what the live
    # path falls back to when the provider is unreachable.
    manual_rate: float = 0.0
    rate_source: str = "MANUAL"
    # How `rate_from_base` was actually obtained on this response: "base", "manual", "live",
    # "cached", "stale" or "fallback_manual". Surface it - a page claiming a live rate while
    # serving a fallback is worse than one that says which it used.
    rate_status: str = "manual"
    rate_fetched_at: datetime | None = None
    rate_detail: str | None = None

    tax_enabled: bool = False
    tax_percent: float = 0.0
    tax_label: str = "Tax"
    tax_inclusive: bool = False
    late_fee_enabled: bool = False
    late_fee_percent: float = 0.0
    late_fee_amount: float = 0.0
    late_fee_grace_days: int = 7
    convenience_percent: float = 0.0
    convenience_amount: float = 0.0
    rounding: str = "NONE"

    overrides: list[str] = Field(default_factory=list)


class FxStatusOut(BaseModel):
    """What the live-rate cache currently holds. For the admin's rates screen."""
    enabled: bool
    provider: str
    cache_minutes: int
    entries: list[dict[str, Any]] = Field(default_factory=list)


class CurrencySettingsOut(BaseModel):
    program: str
    base_currency: str
    currencies: dict[str, CurrencyConfigOut] = Field(default_factory=dict)
    # Only the currencies a payer may actually pick: enabled, and carrying a rate. A currency
    # with no rate is listed above so it can be configured, but never offered - showing a
    # price of zero is worse than not offering the currency.
    available: list[str] = Field(default_factory=list)
    recommended_currency: str | None = None
    options: list[CurrencyOptionOut] = Field(default_factory=list)


class CurrencyConfigUpdate(BaseModel):
    """
    Partial update for one currency. Omitted fields keep their current value; a field left
    unset falls through to the program's finance settings rather than to zero.
    """
    enabled: bool | None = None
    symbol: str | None = Field(None, max_length=8)
    rate_from_base: float | None = Field(
        None, ge=0,
        description="Units of this currency per one unit of the base. Used directly under "
                    "MANUAL, and as the fallback under LIVE.",
    )
    rate_source: str | None = Field(
        None,
        description="MANUAL uses the rate above. LIVE fetches from the exchange-rate "
                    "provider and falls back to it when unreachable.",
    )

    tax_enabled: bool | None = None
    tax_percent: float | None = Field(None, ge=0, le=100)
    tax_label: str | None = Field(None, max_length=40)
    tax_inclusive: bool | None = None
    late_fee_enabled: bool | None = None
    late_fee_percent: float | None = Field(None, ge=0, le=100)
    late_fee_amount: float | None = Field(None, ge=0)
    late_fee_grace_days: int | None = Field(None, ge=0)
    convenience_percent: float | None = Field(None, ge=0, le=100)
    convenience_amount: float | None = Field(None, ge=0)
    rounding: str | None = Field(None, description="NONE, NEAREST, UP or DOWN")


class CurrencySettingsUpdate(BaseModel):
    base_currency: str | None = Field(
        None, max_length=8,
        description="The currency fee structures are entered in. Changing it does not "
                    "re-price anything already entered.",
    )
    currencies: dict[str, CurrencyConfigUpdate] = Field(
        default_factory=dict,
        description='Keyed by code, e.g. {"AED": {"rate_from_base": 0.044, '
                    '"convenience_percent": 0.5}}',
    )
