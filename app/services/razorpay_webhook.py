"""
Razorpay's webhook: one URL for every event the school's account sends.

School fees (Orders and Checkout) and the admission fee (Payment Links) share one Razorpay
account, and Razorpay signs every event with the one webhook secret. So the signature is
checked once here and the event handed to whichever side it concerns. A payment link makes
an order of its own, so its `order.paid` also arrives; billing finds no fee payment for that
order and ignores it.
"""

import json

from fastapi import HTTPException

from app.core import razorpay
from app.services import admission_pipeline, billing

FEE_EVENTS = frozenset({"order.paid", "payment.captured", "payment.authorized"})


def handle(body: bytes, signature: str | None) -> dict:
    if not razorpay.verify_webhook(body, signature):
        raise HTTPException(status_code=400, detail="Invalid signature")
    try:
        event = json.loads(body.decode("utf-8"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid body") from exc
    name = event.get("event") or ""
    if name == "payment_link.paid":
        return admission_pipeline.apply_payment_link_paid(event)
    if name in FEE_EVENTS:
        return billing.apply_razorpay_event(event)
    return {"ok": True, "ignored": name}
