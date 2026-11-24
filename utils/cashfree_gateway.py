"""Cashfree Payment Gateway server-side adapter for Sivdhi.

Only the server handles credentials and verifies payment status. The browser
receives a payment_session_id, never the Cashfree secret key.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import uuid
from decimal import Decimal, InvalidOperation
from urllib.parse import quote, urlsplit

import requests


API_VERSION = os.getenv("CASHFREE_API_VERSION", "2025-01-01").strip()


class CashfreeGatewayError(RuntimeError):
    """Safe, user-displayable Cashfree integration error."""

    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = bool(retryable)


def cashfree_settings(expected_environment: str | None = None):
    environment = os.getenv("CASHFREE_ENV", "sandbox").strip().lower()
    app_id = os.getenv("CASHFREE_APP_ID", "").strip()
    secret_key = os.getenv("CASHFREE_SECRET_KEY", "").strip()
    if environment not in {"sandbox", "production"}:
        raise CashfreeGatewayError("Cashfree environment must be sandbox or production.")
    if expected_environment is not None and str(expected_environment).strip().lower() != environment:
        raise CashfreeGatewayError("Cashfree order environment does not match the configured credentials. Restore the matching environment to verify this payment.")
    app_environment = os.getenv("APP_ENV", os.getenv("FLASK_ENV", "development")).strip().lower()
    live_enabled = os.getenv("CASHFREE_LIVE_PAYMENTS_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}
    if environment == "production" and (app_environment != "production" or not live_enabled):
        raise CashfreeGatewayError("Live Cashfree payments are disabled. Production mode and explicit live-payment approval are both required.")
    sandbox_prod_allowed = os.getenv("CASHFREE_ALLOW_SANDBOX_IN_PRODUCTION", "0").strip().lower() in {"1", "true", "yes", "on"}
    if app_environment == "production" and environment == "sandbox" and not sandbox_prod_allowed:
        raise CashfreeGatewayError("Cashfree Sandbox is disabled on the production Sivdhi server unless its explicit test-only override is enabled.")
    if not app_id or not secret_key:
        raise CashfreeGatewayError("Cashfree API credentials are not configured on the server.")
    base_url = "https://sandbox.cashfree.com/pg" if environment == "sandbox" else "https://api.cashfree.com/pg"
    return environment, base_url, app_id, secret_key


def cashfree_webhook_settings(expected_environment: str | None = None):
    """Load credentials for signed webhook verification without enabling checkout.

    Production webhooks require APP_ENV=production and production credentials,
    but they must remain verifiable while live checkout is explicitly disabled.
    """
    environment = os.getenv("CASHFREE_ENV", "sandbox").strip().lower()
    app_id = os.getenv("CASHFREE_APP_ID", "").strip()
    secret_key = os.getenv("CASHFREE_SECRET_KEY", "").strip()

    if environment not in {"sandbox", "production"}:
        raise CashfreeGatewayError("Cashfree environment must be sandbox or production.")

    if expected_environment is not None and str(expected_environment).strip().lower() != environment:
        raise CashfreeGatewayError(
            "Cashfree webhook environment does not match the configured credentials."
        )

    app_environment = os.getenv(
        "APP_ENV", os.getenv("FLASK_ENV", "development")
    ).strip().lower()

    if environment == "production" and app_environment != "production":
        raise CashfreeGatewayError(
            "Production Cashfree webhook credentials require production mode."
        )

    sandbox_prod_allowed = os.getenv(
        "CASHFREE_ALLOW_SANDBOX_IN_PRODUCTION", "0"
    ).strip().lower() in {"1", "true", "yes", "on"}

    if app_environment == "production" and environment == "sandbox" and not sandbox_prod_allowed:
        raise CashfreeGatewayError(
            "Cashfree Sandbox webhooks are disabled on the production Sivdhi server."
        )

    if not app_id or not secret_key:
        raise CashfreeGatewayError(
            "Cashfree webhook credentials are not configured on the server."
        )

    base_url = (
        "https://sandbox.cashfree.com/pg"
        if environment == "sandbox"
        else "https://api.cashfree.com/pg"
    )
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
            + (f" {str(message)[:180]}" if message else ""),
            retryable=(int(response.status_code) == 429 or int(response.status_code) >= 500),
        )
    if not isinstance(payload, (dict, list)):
        raise CashfreeGatewayError("Cashfree returned an invalid response.")
    return payload


def create_cashfree_order(
    order_id: str,
    amount: float,
    customer: dict,
    return_url: str,
    order_note: str = "Sivdhi booking advance",
) -> dict:
    environment, base_url, app_id, secret_key = cashfree_settings()
    phone = re.sub(r"\D", "", str(customer.get("mobile") or ""))
    if len(phone) == 12 and phone.startswith("91"):
        phone = phone[2:]
    if len(phone) != 10:
        raise CashfreeGatewayError("Please update your 10-digit mobile number before paying online.")
    try:
        order_amount = Decimal(str(amount)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        raise CashfreeGatewayError("Invalid Cashfree payment amount.")
    if order_amount < Decimal("1.00"):
        raise CashfreeGatewayError("Cashfree online payments require a minimum amount of Rs 1.00.")

    return_parts = urlsplit(str(return_url or ""))
    if return_parts.scheme not in {"https", "http"} or not return_parts.netloc:
        raise CashfreeGatewayError("A valid public Cashfree return URL is required.")
    app_environment = os.getenv("APP_ENV", os.getenv("FLASK_ENV", "development")).strip().lower()
    if app_environment == "production" and return_parts.scheme != "https":
        raise CashfreeGatewayError("The Cashfree return URL must use HTTPS in production.")
    notify_url = os.getenv("SIVDHI_CASHFREE_WEBHOOK_URL", "").strip()
    if not notify_url:
        notify_url = f"{return_parts.scheme}://{return_parts.netloc}/customer/cashfree/webhook"
    notify_parts = urlsplit(notify_url)
    if notify_parts.scheme not in {"https", "http"} or not notify_parts.netloc:
        raise CashfreeGatewayError("A valid Cashfree webhook URL is required.")
    if os.getenv("APP_ENV", os.getenv("FLASK_ENV", "development")).strip().lower() == "production" and notify_parts.scheme != "https":
        raise CashfreeGatewayError("The Cashfree webhook URL must use HTTPS in production.")

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
        "order_meta": {"return_url": return_url, "notify_url": notify_url},
        "order_note": str(order_note or "Sivdhi booking advance")[:80],
    }
    try:
        response = requests.post(
            f"{base_url}/orders",
            json=payload,
            headers={
                **_headers(app_id, secret_key),
                "x-idempotency-key": str(uuid.uuid5(uuid.NAMESPACE_URL, f"sivdhi-cashfree-create-order:{order_id}")),
            },
            timeout=(5, 25),
        )
    except requests.RequestException as exc:
        # The provider may have created the order before the connection timed
        # out. Keep the same local order ID so a retry reuses the same UUID key.
        raise CashfreeGatewayError(
            "Cashfree connection timed out or failed. Retry this same checkout; do not start a second payment.",
            retryable=True,
        ) from exc
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





def get_cashfree_checkout_session(
    order_id: str,
    expected_amount: float,
    environment: str,
) -> dict:
    """Fetch a current checkout session for an existing, safely retryable order.

    Never creates a second Cashfree order. Refuses checkout when the order is
    not ACTIVE, its amount/currency differs, or any payment is pending/successful.
    """
    environment, base_url, app_id, secret_key = cashfree_settings(
        expected_environment=environment
    )
    order_id = str(order_id or "").strip()
    if not order_id or len(order_id) > 50:
        raise CashfreeGatewayError("Invalid Cashfree order reference.")

    headers = _headers(app_id, secret_key)
    encoded_order_id = quote(order_id, safe="")

    try:
        response = requests.get(
            f"{base_url}/orders/{encoded_order_id}",
            headers=headers,
            timeout=(5, 15),
        )
    except requests.RequestException as exc:
        raise CashfreeGatewayError(
            "Could not refresh the Cashfree checkout session. Please retry.",
            retryable=True,
        ) from exc

    order = _response_json(response)
    if not isinstance(order, dict) or str(order.get("order_id") or "") != order_id:
        raise CashfreeGatewayError("Cashfree returned an unexpected order reference.")

    if str(order.get("order_status") or "").upper() != "ACTIVE":
        raise CashfreeGatewayError(
            "This payment order is no longer active. Refresh your booking status before paying again."
        )

    if str(order.get("order_currency") or "").upper() != "INR":
        raise CashfreeGatewayError("Cashfree order currency does not match INR.")

    try:
        actual_amount = Decimal(str(order.get("order_amount"))).quantize(
            Decimal("0.01")
        )
        expected = Decimal(str(expected_amount)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        raise CashfreeGatewayError("Cashfree returned an invalid order amount.")

    if actual_amount != expected:
        raise CashfreeGatewayError(
            "Cashfree order amount does not match the booking. Contact support before retrying."
        )

    session_id = str(order.get("payment_session_id") or "").strip()
    if not session_id:
        raise CashfreeGatewayError("Cashfree did not return a current payment session.")

    # A failed or not-yet-attempted transaction can be retried on the same
    # order. Pending/successful or unknown states must never be re-checkouted.
    try:
        payments_response = requests.get(
            f"{base_url}/orders/{encoded_order_id}/payments",
            headers=headers,
            timeout=(5, 15),
        )
    except requests.RequestException as exc:
        raise CashfreeGatewayError(
            "Could not confirm the previous Cashfree payment attempt. Do not retry yet.",
            retryable=True,
        ) from exc

    payments = _response_json(payments_response)
    if not isinstance(payments, list):
        raise CashfreeGatewayError("Cashfree returned an invalid payment status response.")

    retryable_statuses = {"FAILED", "NOT_ATTEMPTED", "USER_DROPPED"}
    for payment in payments:
        if not isinstance(payment, dict):
            raise CashfreeGatewayError("Cashfree returned an invalid payment attempt.")

        payment_order_id = str(payment.get("order_id") or "").strip()
        if payment_order_id != order_id:
            raise CashfreeGatewayError(
                "Cashfree returned a payment attempt for an unexpected or missing order reference."
            )

        payment_status = str(payment.get("payment_status") or "").strip().upper()
        if payment_status not in retryable_statuses:
            raise CashfreeGatewayError(
                "A previous payment attempt is pending, successful, or needs verification. "
                "Refresh your booking status; do not pay again."
            )

    return {
        "order_id": order_id,
        "payment_session_id": session_id,
        "environment": environment,
    }



def create_cashfree_refund(
    order_id: str,
    refund_id: str,
    amount: float,
    environment: str,
    idempotency_key: str,
    refund_note: str = "Sivdhi booking cancellation",
) -> dict:
    """Create a provider refund using a persisted, stable idempotency key."""
    import uuid
    from urllib.parse import quote

    _, base_url, app_id, secret_key = cashfree_settings(
        expected_environment=environment
    )

    order_id = str(order_id or "").strip()
    refund_id = str(refund_id or "").strip()

    if not order_id or not re.fullmatch(r"[A-Za-z0-9]{8,40}", refund_id):
        raise CashfreeGatewayError("Invalid Cashfree order or refund reference.")

    try:
        key = str(uuid.UUID(str(idempotency_key)))
    except (ValueError, TypeError, AttributeError):
        raise CashfreeGatewayError("Invalid Cashfree refund idempotency key.")

    try:
        refund_amount = Decimal(str(amount)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        raise CashfreeGatewayError("Invalid Cashfree refund amount.")

    if refund_amount <= 0:
        raise CashfreeGatewayError("Cashfree refund amount must be positive.")

    payload = {
        "refund_amount": float(refund_amount),
        "refund_id": refund_id,
        "refund_note": str(refund_note or "Sivdhi booking cancellation")[:80],
        "refund_speed": "STANDARD",
    }

    try:
        response = requests.post(
            f"{base_url}/orders/{quote(order_id, safe='')}/refunds",
            json=payload,
            headers={
                **_headers(app_id, secret_key),
                "x-idempotency-key": key,
            },
            timeout=(5, 25),
        )
    except requests.RequestException as exc:
        raise CashfreeGatewayError(
            "Cashfree refund request outcome is uncertain; retry with the same idempotency key.",
            retryable=True,
        ) from exc

    result = _response_json(response)
    if not isinstance(result, dict):
        raise CashfreeGatewayError("Cashfree returned an invalid refund response.")

    returned_order = str(result.get("order_id") or order_id)
    returned_refund = str(result.get("refund_id") or refund_id)

    if returned_order != order_id or returned_refund != refund_id:
        raise CashfreeGatewayError(
            "Cashfree refund response did not match the requested references."
        )

    return result


def fetch_cashfree_refund(
    order_id: str,
    refund_id: str,
    environment: str,
) -> dict | None:
    """Fetch one refund by merchant refund_id. Return None only for HTTP 404."""
    from urllib.parse import quote

    _, base_url, app_id, secret_key = cashfree_settings(
        expected_environment=environment
    )

    try:
        response = requests.get(
            f"{base_url}/orders/{quote(str(order_id), safe='')}/refunds/"
            f"{quote(str(refund_id), safe='')}",
            headers=_headers(app_id, secret_key),
            timeout=(5, 25),
        )
    except requests.RequestException as exc:
        raise CashfreeGatewayError(
            "Could not fetch Cashfree refund status.",
            retryable=True,
        ) from exc

    if int(response.status_code) == 404:
        return None

    result = _response_json(response)
    if not isinstance(result, dict):
        raise CashfreeGatewayError("Cashfree returned an invalid refund status response.")
    return result


def verify_cashfree_webhook_signature(
    signature: str, timestamp: str, raw_body: bytes, secret_key: str
) -> bool:
    """Verify Cashfree's HMAC-SHA256 signature against the exact raw body."""
    if not signature or not timestamp or not secret_key or not isinstance(raw_body, bytes):
        return False
    signed_payload = timestamp.encode("utf-8") + raw_body
    expected = base64.b64encode(
        hmac.new(secret_key.encode("utf-8"), signed_payload, hashlib.sha256).digest()
    ).decode("ascii")
    return hmac.compare_digest(expected, signature.strip())


def verify_cashfree_order_payment(
    order_id: str, expected_amount: float, environment: str | None = None
) -> dict | None:
    """Verify against credentials for the environment stored with the order."""
    _, base_url, app_id, secret_key = cashfree_settings(expected_environment=environment)
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

        # Count every successful payment for this order before checking the
        # expected amount/currency. A second captured payment must never be
        # hidden merely because its amount or currency differs.
        cf_payment_id = str(payment.get("cf_payment_id") or "").strip()
        if not cf_payment_id:
            continue
        successful[cf_payment_id] = payment

    if len(successful) > 1:
        raise CashfreeGatewayError(
            "Cashfree reported multiple successful payments for this order. Manual reconciliation is required."
        )
    if not successful:
        return None

    payment = next(iter(successful.values()))
    if str(payment.get("payment_currency") or "").upper() != "INR":
        return None
    try:
        paid = Decimal(str(payment.get("payment_amount"))).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if paid != expected:
        return None

    return {
        "cf_payment_id": next(iter(successful)),
        "payment_group": str(payment.get("payment_group") or "online")[:40],
        "payment_amount": str(paid),
    }
