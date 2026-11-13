"""Cashfree Payment Gateway server-side adapter for Sivdhi.

Only the server handles credentials and verifies payment status. The browser
receives a payment_session_id, never the Cashfree secret key.
"""
from __future__ import annotations

import os
import re
from decimal import Decimal, InvalidOperation

import requests


API_VERSION = os.getenv("CASHFREE_API_VERSION", "2025-01-01").strip()


class CashfreeGatewayError(RuntimeError):
    """Safe, user-displayable Cashfree integration error."""


def cashfree_settings():
    environment = os.getenv("CASHFREE_ENV", "sandbox").strip().lower()
    app_id = os.getenv("CASHFREE_APP_ID", "").strip()
    secret_key = os.getenv("CASHFREE_SECRET_KEY", "").strip()
    if environment not in {"sandbox", "production"}:
        raise CashfreeGatewayError("Cashfree environment must be sandbox or production.")
    if not app_id or not secret_key:
        raise CashfreeGatewayError("Cashfree API credentials are not configured on the server.")
    base_url = "https://sandbox.cashfree.com/pg" if environment == "sandbox" else "https://api.cashfree.com/pg"
    return environment, base_url, app_id, secret_key


def cashfree_enabled() -> bool:
    if os.getenv("SIVDHI_PAYMENT_MODE", "").strip().lower() != "cashfree":
        return False
    try:
        cashfree_settings()
        return True
    except CashfreeGatewayError:
        return False


def _headers(app_id: str, secret_key: str) -> dict[str, str]:
    return {
        "x-client-id": app_id,
        "x-client-secret": secret_key,
        "x-api-version": API_VERSION,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _response_json(response):
    try:
        payload = response.json()
    except (ValueError, AttributeError):
        payload = {}
    if not response.ok:
        # Do not log or expose request headers, which contain credentials.
        message = payload.get("message") if isinstance(payload, dict) else None
        raise CashfreeGatewayError(
            f"Cashfree request failed (HTTP {response.status_code})."
            + (f" {str(message)[:180]}" if message else "")
        )
    if not isinstance(payload, (dict, list)):
        raise CashfreeGatewayError("Cashfree returned an invalid response.")
    return payload


def create_cashfree_order(order_id: str, amount: float, customer: dict, return_url: str) -> dict:
    environment, base_url, app_id, secret_key = cashfree_settings()
    phone = re.sub(r"\D", "", str(customer.get("mobile") or ""))
    if len(phone) == 12 and phone.startswith("91"):
        phone = phone[2:]
    if len(phone) != 10:
        raise CashfreeGatewayError("Please update your 10-digit mobile number before paying online.")
    try:
        order_amount = Decimal(str(amount)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        raise CashfreeGatewayError("Invalid booking advance amount.")
    if order_amount <= 0:
        raise CashfreeGatewayError("Booking advance must be greater than zero.")

    customer_details = {
        "customer_id": str(customer.get("id")),
        "customer_name": str(customer.get("fullname") or customer.get("name") or "Sivdhi Customer")[:100],
        "customer_phone": phone,
    }
    email = str(customer.get("email") or "").strip()
    if email and "@" in email and len(email) <= 254:
        customer_details["customer_email"] = email

    payload = {
        "order_id": order_id,
        "order_amount": float(order_amount),
        "order_currency": "INR",
        "customer_details": customer_details,
        "order_meta": {"return_url": return_url},
        "order_note": "Sivdhi booking advance",
    }
    try:
        response = requests.post(
            f"{base_url}/orders",
            json=payload,
            headers=_headers(app_id, secret_key),
            timeout=(5, 25),
        )
    except requests.RequestException as exc:
        raise CashfreeGatewayError("Could not connect to Cashfree. Please try again.") from exc
    result = _response_json(response)
    returned_order_id = str(result.get("order_id") or "").strip()
    if returned_order_id != str(order_id):
        raise CashfreeGatewayError("Cashfree returned an unexpected order reference.")
    session_id = str(result.get("payment_session_id") or "").strip()
    if not session_id:
        raise CashfreeGatewayError("Cashfree did not return a payment session.")
    return {
        "payment_session_id": session_id,
        "environment": environment,
        "order_id": returned_order_id,
    }


def verify_cashfree_order_payment(order_id: str, expected_amount: float) -> dict | None:
    """Return one captured SUCCESS payment only when order, amount and currency match."""
    _, base_url, app_id, secret_key = cashfree_settings()
    try:
        response = requests.get(
            f"{base_url}/orders/{order_id}/payments",
            headers=_headers(app_id, secret_key),
            timeout=(5, 25),
        )
    except requests.RequestException as exc:
        raise CashfreeGatewayError("Could not verify the Cashfree payment. Please retry verification.") from exc
    payments = _response_json(response)
    if not isinstance(payments, list):
        raise CashfreeGatewayError("Cashfree returned an invalid payment status response.")

    expected = Decimal(str(expected_amount)).quantize(Decimal("0.01"))
    successful = {}

    for payment in payments:
        if not isinstance(payment, dict):
            continue
        if str(payment.get("order_id") or "") != str(order_id):
            continue
        if str(payment.get("payment_status") or "").upper() != "SUCCESS":
            continue
        if str(payment.get("payment_currency") or "").upper() != "INR":
            continue
        try:
            paid = Decimal(str(payment.get("payment_amount"))).quantize(Decimal("0.01"))
        except (InvalidOperation, TypeError, ValueError):
            continue
        if paid != expected:
            continue
        cf_payment_id = str(payment.get("cf_payment_id") or "").strip()
        if not cf_payment_id:
            continue

        successful[cf_payment_id] = {
            "cf_payment_id": cf_payment_id,
            "payment_group": str(payment.get("payment_group") or "online")[:40],
            "payment_amount": str(paid),
        }

    if len(successful) > 1:
        raise CashfreeGatewayError(
            "Cashfree reported multiple successful payments for this order. Manual reconciliation is required."
        )
    if not successful:
        return None
    return next(iter(successful.values()))
