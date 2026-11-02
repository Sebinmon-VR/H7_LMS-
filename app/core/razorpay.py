"""
Razorpay: Payment Links for the admission fee, Orders and Checkout for school fees.

Three calls and a signature check, which is all the admission pipeline needs: make a link
for an amount, read a link back to see whether it was paid, and trust a webhook only when
Razorpay signed it. Payment Links rather than Checkout because a link is a URL that can go
in an email and be paid from any phone, with no page of ours in between.

Without RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET nothing here is called: the office pastes a
payment link of its own (or bank details) and the family sends proof instead.
"""

import hashlib
import hmac
import logging

import httpx

from app.core.config import settings

logger = logging.getLogger("razorpay")

API = "https://api.razorpay.com/v1"
TIMEOUT_SECONDS = 15.0

# Razorpay counts money in the currency's smallest unit. Three-decimal currencies are the
# exception worth knowing about in the Gulf; everything else on this system has two.
_THREE_DECIMALS = frozenset({"BHD", "JOD", "KWD", "OMR", "TND"})

# Razorpay refuses an order under 100 of the smallest unit (INR 1.00).
MIN_ORDER_MINOR = 100


class RazorpayError(Exception):
    """Razorpay refused or could not be reached. The message is safe to show an admin."""


class RazorpayKeysRejected(RazorpayError):
    """
    Razorpay answered 401: the key id and secret on the server are no longer valid.

    Test keys are replaced often, so this is the usual way online payment stops working:
    every call fails the same way until the current keys are set and the server restarts.
    """


def is_configured() -> bool:
    return settings.razorpay_configured


def _auth() -> tuple[str, str]:
    return settings.RAZORPAY_KEY_ID.strip(), settings.RAZORPAY_KEY_SECRET.strip()


def _unit(currency: str) -> int:
    return 1000 if (currency or "").upper() in _THREE_DECIMALS else 100


def to_minor(amount: float, currency: str) -> int:
    return int(round(float(amount) * _unit(currency)))


def from_minor(amount, currency: str) -> float:
    return round(int(amount or 0) / _unit(currency), 3)


def _call(method: str, path: str, json: dict | None = None) -> dict:
    try:
        response = httpx.request(method, f"{API}{path}", json=json, auth=_auth(), timeout=TIMEOUT_SECONDS)
    except httpx.HTTPError as exc:
        raise RazorpayError(f"Razorpay could not be reached: {exc}") from exc
    if response.status_code == 401:
        raise RazorpayKeysRejected(
            "Razorpay turned down the school's keys. They have changed or expired: put the "
            "current Razorpay keys in the server settings."
        )
    if response.status_code >= 400:
        try:
            error = response.json().get("error") or {}
            message = error.get("description") or response.text
        except ValueError:
            message = response.text
        raise RazorpayError(f"Razorpay refused the request: {message}")
    return response.json()


def create_payment_link(*, amount: float, currency: str, description: str, reference_id: str,
                        customer_name: str | None, customer_email: str | None,
                        customer_phone: str | None, callback_url: str | None,
                        notes: dict | None = None, expire_by: int | None = None) -> dict:
    """
    A new link. Razorpay's own email and SMS are switched off: the school's letter carries the
    link, and a second message from a payment company confuses families.

    `reference_id` must be unique per Razorpay account, so a re-requested fee needs a new one.
    """
    customer = {k: v for k, v in {
        "name": customer_name, "email": customer_email, "contact": customer_phone,
    }.items() if v}
    body = {
        "amount": to_minor(amount, currency),
        "currency": currency.upper(),
        "accept_partial": False,
        "description": description[:2048],
        "reference_id": reference_id[:40],
        "customer": customer,
        "notify": {"sms": False, "email": False},
        "reminder_enable": False,
        "notes": {k: str(v)[:256] for k, v in (notes or {}).items()},
    }
    if callback_url:
        body["callback_url"] = callback_url
        body["callback_method"] = "get"
    if expire_by:
        body["expire_by"] = int(expire_by)
    return _call("POST", "/payment_links", body)


def fetch_payment_link(link_id: str) -> dict:
    return _call("GET", f"/payment_links/{link_id}")


def cancel_payment_link(link_id: str) -> None:
    """Best effort: an old link must not take money for a fee that was re-issued."""
    try:
        _call("POST", f"/payment_links/{link_id}/cancel")
    except RazorpayError as exc:
        logger.info("Payment link %s not cancelled: %s", link_id, exc)


def key_id() -> str:
    """The public key, which the browser's Checkout needs. Never the secret."""
    return _auth()[0]


def is_test_mode() -> bool:
    """Test keys (rzp_test_...) move no real money: Razorpay's test cards and UPI ids work."""
    return key_id().startswith("rzp_test_")


def create_order(*, amount: float, currency: str, receipt: str, notes: dict | None = None) -> dict:
    """
    An order for school fees paid through Checkout on our own page.

    Fees use Orders and Checkout rather than a link: the payer is already signed in on the
    fees page, and Checkout returns a signed result this server verifies before crediting.
    """
    return _call("POST", "/orders", {
        "amount": to_minor(amount, currency),
        "currency": currency.upper(),
        "receipt": receipt[:40],
        "notes": {k: str(v)[:256] for k, v in (notes or {}).items() if v not in (None, "")},
    })


def verify_payment_signature(order_id: str, payment_id: str, signature: str | None) -> bool:
    """Checkout's signature: HMAC-SHA256 of "order_id|payment_id" with the key secret."""
    secret = _auth()[1]
    if not secret or not signature or not order_id or not payment_id:
        return False
    expected = hmac.new(secret.encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.strip())


def fetch_payment(payment_id: str) -> dict:
    return _call("GET", f"/payments/{payment_id}")


def fetch_order_payments(order_id: str) -> list[dict]:
    """Every attempt on an order, failed ones included: a payer may retry inside Checkout."""
    return _call("GET", f"/orders/{order_id}/payments").get("items") or []


def capture_payment(payment_id: str, amount: float, currency: str) -> dict:
    """For an account that does not capture automatically: claim an authorised payment."""
    return _call("POST", f"/payments/{payment_id}/capture", {
        "amount": to_minor(amount, currency), "currency": currency.upper(),
    })


def describe() -> dict:
    """The settings, for the admin's Integrations page. The key id is public; never the secret."""
    configured = is_configured()
    return {
        "configured": configured,
        "key_id": key_id() or None,
        "test_mode": is_test_mode() if configured else None,
        "webhook_secret_set": bool((settings.RAZORPAY_WEBHOOK_SECRET or "").strip()),
        # The one URL to give Razorpay, for both fee orders and admission payment links.
        "webhook_path": f"{settings.API_V1_STR}/payments/razorpay/webhook",
        "problems": [] if configured else
        ["No Razorpay keys are set (RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET), so online payment is off."],
    }


def check_access() -> dict:
    """
    Tries the keys with the smallest read Razorpay offers: one order from the list.

    The way to confirm a key change took effect, since test keys are replaced often and a
    stale pair only shows up when somebody tries to pay.
    """
    described = describe()
    if not described["configured"]:
        return {**described, "ok": False, "detail": described["problems"][0]}
    try:
        _call("GET", "/orders?count=1")
    except RazorpayError as exc:
        return {**described, "ok": False, "detail": str(exc)}
    mode = "Test mode: no real money moves." if described["test_mode"] else "Live mode: payments are real."
    return {**described, "ok": True, "detail": f"Razorpay accepted the keys. {mode}"}


def verify_webhook(body: bytes, signature: str | None) -> bool:
    secret = (settings.RAZORPAY_WEBHOOK_SECRET or "").strip()
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.strip())
