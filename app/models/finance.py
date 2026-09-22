"""
School finance: what is charged, who it is charged to, and what has been paid.

The tuition programme already bills, and it bills by counting classes that happened
(`app.models.tuition`). The school cannot work that way. A school agrees a structure in
advance - so much a year, in these instalments - and the bill exists whether or not anybody
turned up. That is why this is a second set of models rather than a `program` field on the
tuition ones: an invoice built from a count and an invoice built from a schedule share a
total and nothing else.

The chain is deliberately four links long:

    FeeHead      what can be charged at all      ("Tuition Fee", "Transport")
    FeeStructure what this cohort is charged     (Class 7, 2025-26: these heads, these amounts)
    Instalment   when it falls due               (four dates, with amounts that sum to the total)
    FeeInvoice   what one student actually owes   (structure resolved, discounts applied, frozen)

Collapsing any two of them breaks something real. Heads and structures are separate because
"Transport" is one thing the school charges and twelve different amounts depending on the
route. Structures and instalments are separate because the same fee is collected monthly for
one family and annually for another. Structures and invoices are separate because an invoice
must not change when next year's fees are set - it is a record of a debt, not a view over the
current price list.
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from app.core.enums import (
    ChargeKind, DiscountBasis, DiscountValueType, FeeFrequency,
    InstalmentStatus, InvoiceStatus, PaymentIntentStatus,
)


@dataclass(slots=True)
class FeeHead:
    """
    One thing the school can charge for.

    A catalogue entry, not an amount owed by anyone. `default_amount` is a convenience for
    the structure editor; the amount that binds is always the one on the structure line,
    because the same head is charged differently to different classes and reading the
    default at invoice time would silently re-price every bill when somebody edited the
    catalogue.

    `is_taxable` drives the tax base. A head marked taxable contributes to the amount tax is
    computed on; one that is not is excluded, which is how a school charges tax on transport
    but not on tuition without writing two invoice paths.
    """
    name: str
    code: str

    kind: ChargeKind = ChargeKind.FEE
    frequency: FeeFrequency = FeeFrequency.ANNUAL
    default_amount: float = 0.0

    is_taxable: bool = False
    # Null falls back to the program's default rate, so a school with one tax rate sets it
    # once in settings rather than on every head.
    tax_percent: float | None = None

    # Whether a concession may be applied to this head at all. Off for statutory charges a
    # school collects and passes on - an exam board's entry fee is not the school's to
    # discount, and a sibling rule that quietly did so would leave a shortfall nobody
    # budgeted for.
    is_discountable: bool = True
    # Charged once at admission rather than every year. Waived by an admission category
    # carrying `waives_admission_charge`.
    is_admission_charge: bool = False

    program: str = "LMS"
    description: str | None = None
    is_active: bool = True
    sort_order: int = 0

    created_by: int | None = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime | None = None
    id: int | None = None


@dataclass(slots=True)
class FeeStructure:
    """
    What one cohort is charged for one year.

    Scoped by class and by admission category, both optional and both lists. An empty list
    means "any": a structure with no classes and no categories is the year's default, which
    is what a small school wants and what stops the feature demanding twelve near-identical
    documents on day one.

    Resolution is most-specific-first (`app.services.finance.resolve_structure`): a structure
    naming both the student's class and their category beats one naming only the class, which
    beats the default. Ties are broken by `priority`, then by the newer document - an
    arbitrary rule, but a stated one, which is better than whichever Firestore returned first.

    `items` holds the lines: [{"fee_head_id", "amount", "frequency", "label"}]. Stored inline
    rather than as their own collection because a structure is always read whole - nothing
    ever asks for one line of it - and inlining makes reading a student's fees one round trip
    rather than one per head.
    """
    name: str
    academic_year_id: int

    class_ids: list[int] = field(default_factory=list)
    admission_category_ids: list[int] = field(default_factory=list)

    items: list[dict[str, Any]] = field(default_factory=list)

    program: str = "LMS"
    currency: str = "INR"
    # Breaks a tie between two structures of equal specificity. Higher wins.
    priority: int = 0
    is_active: bool = True
    notes: str | None = None

    created_by: int | None = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime | None = None
    id: int | None = None


@dataclass(slots=True)
class InstalmentPlan:
    """
    When a structure's total falls due.

    Separate from the structure because the same fees are collected on different schedules
    for different families - monthly for most, annually for the ones who pay up front - and
    duplicating the whole structure per schedule is how the two drift apart.

    `instalments` is [{"label", "due_date", "percent", "amount"}]. A line carries *either* a
    percent or an amount; percent is the normal case because it survives a change to the
    structure's total, while a fixed amount is for the school that collects exactly 5,000 in
    April whatever else happens. `app.services.finance` validates that percentages sum to 100
    - an unvalidated plan silently under-bills, and nobody notices until the year is over.
    """
    name: str
    academic_year_id: int

    instalments: list[dict[str, Any]] = field(default_factory=list)

    # Scope, resolved exactly like a structure's. Empty means any.
    class_ids: list[int] = field(default_factory=list)
    admission_category_ids: list[int] = field(default_factory=list)
    structure_id: int | None = None

    program: str = "LMS"
    # Days after an instalment's due date before a late fee is charged. Null uses the
    # program's setting.
    grace_days: int | None = None
    is_default: bool = False
    is_active: bool = True

    created_by: int | None = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime | None = None
    id: int | None = None


@dataclass(slots=True)
class DiscountRule:
    """
    A concession, and the conditions under which it applies by itself.

    The two the brief names are `SIBLING` - the nth child of a household pays less - and
    `MULTI_REGISTRATION` - one student holding several concurrent enrollments. They are
    different counts over different things, which is exactly why `basis` exists rather than
    a single "discount percent" on the structure.

    `applies_from_nth` is what makes a sibling rule expressible: "from the 2nd child" leaves
    the eldest at full fee, which is how every school states it. For MULTI_REGISTRATION the
    same field counts subjects rather than children.

    `is_stackable` decides whether this rule may combine with others. Off by default, and
    deliberately so: a staff ward who is also a second child and also paid early should not
    reach a negative bill, and the safe default is "best single rule wins" with stacking as
    something an admin turns on knowing what they are doing.
    """
    name: str
    basis: DiscountBasis = DiscountBasis.MANUAL
    value_type: DiscountValueType = DiscountValueType.PERCENT
    value: float = 0.0

    academic_year_id: int | None = None
    program: str = "LMS"

    # Conditions. All of the ones that are set must hold.
    applies_from_nth: int = 2          # 2 = from the second child/subject onward.
    min_count: int | None = None       # Minimum household size or enrollment count.
    admission_category_ids: list[int] = field(default_factory=list)
    class_ids: list[int] = field(default_factory=list)
    student_ids: list[int] = field(default_factory=list)   # SCHOLARSHIP and MANUAL.
    fee_head_ids: list[int] = field(default_factory=list)  # Empty = every discountable head.

    # Caps the concession in currency, whatever the percentage works out to. The thing that
    # stops "20% off" becoming unbounded on a large bill.
    max_amount: float | None = None
    is_stackable: bool = False
    priority: int = 0

    valid_from: date | None = None
    valid_until: date | None = None
    is_active: bool = True
    notes: str | None = None

    created_by: int | None = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime | None = None
    id: int | None = None


@dataclass(slots=True)
class FeeInvoice:
    """
    What one student owes, frozen at the moment it was issued.

    A DRAFT is recomputed from the structure on every regeneration. Once ISSUED the numbers
    stop moving, for the same reason the tuition invoice does: a bill that changes after it
    was sent is not a bill, and a parent holding a printout that no longer matches the system
    is a dispute the school always loses.

    `line_items` carries the full derivation - every head, its amount, the tax computed on it
    and the concessions applied - so any figure on the bill can be explained back to the rule
    that produced it. That is the difference between a fee module and a number.

    `instalments` is the schedule with payment state per line. Held on the invoice rather
    than as its own collection because an instalment has no meaning apart from its bill, and
    a separate collection would need a transaction to keep the two totals agreeing.
    """
    student_id: int
    academic_year_id: int

    structure_id: int | None = None
    instalment_plan_id: int | None = None
    status: InvoiceStatus = InvoiceStatus.DRAFT
    currency: str = "INR"

    # [{"fee_head_id", "name", "kind", "frequency", "amount", "taxable", "tax_percent",
    #   "tax_amount", "discount_amount", "net_amount"}]
    line_items: list[dict[str, Any]] = field(default_factory=list)
    # [{"rule_id", "name", "basis", "value_type", "value", "amount", "reason"}]
    discounts: list[dict[str, Any]] = field(default_factory=list)
    # [{"label", "due_date", "amount", "amount_paid", "status", "waived_reason"}]
    instalments: list[dict[str, Any]] = field(default_factory=list)
    # [{"amount", "paid_at", "method", "reference", "recorded_by", "instalment_label",
    #   "intent_id", "note"}]
    payments: list[dict[str, Any]] = field(default_factory=list)

    # The breakdown the payment page renders. Every one of these is derived and re-derived
    # rather than edited: `subtotal` is the heads, `discount_total` what came off,
    # `tax_total` what was computed on the discounted taxable base, `charge_total` the
    # non-fee extras, and `total_amount` the sum a parent is asked for.
    subtotal: float = 0.0
    discount_total: float = 0.0
    taxable_base: float = 0.0
    tax_total: float = 0.0
    charge_total: float = 0.0
    late_fee_total: float = 0.0
    total_amount: float = 0.0
    amount_paid: float = 0.0

    issued_at: datetime | None = None
    due_date: date | None = None
    invoice_number: str | None = None
    notes: str | None = None

    generated_by: int | None = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime | None = None
    id: int | None = None


@dataclass(slots=True)
class PaymentIntent:
    """
    An attempt to pay a bill through a gateway.

    No provider is wired yet - the brief is to show fees, charges and taxes now and integrate
    a gateway later - so this exists to make that integration a matter of writing an adapter
    rather than reshaping the data. It records what was attempted, for how much, against
    which invoice, and what the provider said, in states every provider agrees on
    (`PaymentIntentStatus`).

    It is kept apart from the `payments` list on the invoice on purpose. A payment is money
    the school has; an intent is a customer pressing a button, and most of them fail,
    abandon, or arrive twice. Writing intents into the payments list would put failed
    attempts on a receipt.

    `provider_reference` is what reconciliation joins on, and `idempotency_key` is what stops
    a retried webhook crediting the same payment twice - the one guarantee a payment
    integration cannot be added later.
    """
    invoice_id: int
    student_id: int
    amount: float
    currency: str = "INR"

    status: PaymentIntentStatus = PaymentIntentStatus.CREATED
    # "stripe", "razorpay", "telr" - free text, because the adapter names itself and this
    # field must not need editing when one is added.
    provider: str | None = None
    provider_reference: str | None = None
    # Where the payer is sent. Null until an adapter fills it in.
    checkout_url: str | None = None

    idempotency_key: str | None = None
    instalment_label: str | None = None
    # Whatever the provider last sent back, kept verbatim for support and reconciliation.
    provider_payload: dict[str, Any] = field(default_factory=dict)
    failure_reason: str | None = None

    initiated_by: int | None = None
    completed_at: datetime | None = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime | None = None
    id: int | None = None


@dataclass
class FeeReceipt:
    """
    Money received with no invoice on this system.

    An invoice is what the rules say a student owes for a year, one per student per year;
    a receipt is a statement that money arrived, standing on its own. The one kind that
    exists today is the OPENING_BALANCE: a fee - the admission fee, typically - paid before
    the fee module was in use, entered afterwards so the collections report carries it.
    The head's name and admission flag are copied on at the time, so the entry still reads
    correctly after the price list changes. A receipt never alters what a student owes.
    """
    program: str = "LMS"
    student_id: int | None = None
    academic_year_id: int | None = None
    fee_head_id: int | None = None
    head_name: str | None = None
    is_admission_charge: bool = False
    amount: float = 0.0
    currency: str = "INR"
    paid_at: datetime | None = None
    method: str = "OTHER"
    reference: str | None = None
    note: str | None = None
    source: str = "OPENING_BALANCE"
    recorded_by: int | None = None
    recorded_at: datetime = field(default_factory=datetime.utcnow)
    id: int | None = None
