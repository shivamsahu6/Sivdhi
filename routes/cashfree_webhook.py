"""Cashfree Payment Gateway webhook endpoint.

Only signed, amount-matched Cashfree payment-success events can update local
payment state. The raw request body is used for signature verification.
"""
from __future__ import annotations

import hashlib
import json

from flask import Blueprint, current_app, jsonify, request

from database import get_db
from utils.cashfree_gateway import (
    CashfreeGatewayError,
    cashfree_webhook_settings,
    verify_cashfree_webhook_signature,
)
from utils.sivdhi_finance import ensure_finance_schema, money


cashfree_webhook_bp = Blueprint(
    "cashfree_webhook", __name__, url_prefix="/customer"
)


@cashfree_webhook_bp.route("/cashfree/webhook", methods=["POST"])
def receive_cashfree_webhook():
    raw_body = request.get_data(cache=True, as_text=False)
    if not raw_body or len(raw_body) > 1_000_000:
        return jsonify({"ok": False, "message": "Invalid webhook body."}), 400

    signature = request.headers.get("x-webhook-signature", "")
    timestamp = request.headers.get("x-webhook-timestamp", "")
    try:
        webhook_environment, _base_url, _app_id, secret_key = cashfree_webhook_settings()
    except CashfreeGatewayError:
        current_app.logger.exception("Cashfree webhook credentials are unavailable")
        return jsonify({"ok": False, "message": "Webhook verification is not configured."}), 503

    if not verify_cashfree_webhook_signature(
        signature, timestamp, raw_body, secret_key
    ):
        current_app.logger.warning("Rejected Cashfree webhook with invalid signature")
        return jsonify({"ok": False, "message": "Invalid webhook signature."}), 400

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return jsonify({"ok": False, "message": "Invalid webhook JSON."}), 400
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "message": "Invalid webhook payload."}), 400

    event_type = str(payload.get("type") or "UNKNOWN")[:100]
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    order_data = data.get("order") if isinstance(data.get("order"), dict) else {}
    payment_data = data.get("payment") if isinstance(data.get("payment"), dict) else {}
    order_id = str(order_data.get("order_id") or "").strip()
    payment_id = str(payment_data.get("cf_payment_id") or "").strip()
    payload_hash = hashlib.sha256(raw_body).hexdigest()
    event_key = (
        request.headers.get("x-idempotency-header")
        or request.headers.get("x-idempotency-key")
        or payload_hash
    ).strip()[:200]

    db = get_db()
    try:
        ensure_finance_schema(db)
        db.execute("BEGIN IMMEDIATE")

        prior = db.execute(
            """
            SELECT payload_sha256, processing_status
            FROM cashfree_webhook_events
            WHERE event_key=?
            """,
            (event_key,),
        ).fetchone()
        if prior:
            if str(prior["payload_sha256"]) != payload_hash:
                db.rollback()
                current_app.logger.error(
                    "Cashfree webhook idempotency key was reused with a different payload"
                )
                return jsonify({"ok": False, "message": "Webhook event key conflict."}), 409

            if str(prior["processing_status"] or "").upper() != "RECEIVED":
                db.rollback()
                return jsonify({"ok": True, "duplicate": True}), 200

            # Recover legacy receipts left at RECEIVED by the former
            # non-atomic balance-payment flow. Reprocess against current order
            # state; the payment/order checks below remain idempotent.
            db.execute(
                "DELETE FROM cashfree_webhook_events WHERE event_key=?",
                (event_key,),
            )

        db.execute(
            """
            INSERT INTO cashfree_webhook_events
                (event_key, event_type, order_id, cf_payment_id,
                 payload_sha256, processing_status)
            VALUES (?, ?, ?, ?, ?, 'RECEIVED')
            """,
            (event_key, event_type, order_id or None, payment_id or None, payload_hash),
        )

        if event_type != "PAYMENT_SUCCESS_WEBHOOK":
            db.execute(
                """
                UPDATE cashfree_webhook_events
                SET processing_status='IGNORED_EVENT', processed_at=CURRENT_TIMESTAMP
                WHERE event_key=?
                """,
                (event_key,),
            )
            db.commit()
            return jsonify({"ok": True, "ignored": True}), 200

        if (
            not order_id
            or not payment_id
            or str(payment_data.get("payment_status") or "").upper() != "SUCCESS"
        ):
            db.execute(
                """
                UPDATE cashfree_webhook_events
                SET processing_status='INVALID_SUCCESS_EVENT', processed_at=CURRENT_TIMESTAMP
                WHERE event_key=?
                """,
                (event_key,),
            )
            db.commit()
            return jsonify({"ok": True, "ignored": True}), 200

        order = db.execute(
            "SELECT * FROM cashfree_payment_orders WHERE order_id=? LIMIT 1",
            (order_id,),
        ).fetchone()
        if not order:
            db.execute(
                """
                UPDATE cashfree_webhook_events
                SET processing_status='UNKNOWN_ORDER', processed_at=CURRENT_TIMESTAMP
                WHERE event_key=?
                """,
                (event_key,),
            )
            db.commit()
            current_app.logger.warning("Cashfree webhook referenced unknown order %s", order_id)
            return jsonify({"ok": True, "ignored": True}), 200

        if str(order["environment"] or "").strip().lower() != webhook_environment:
            db.execute("""
                UPDATE cashfree_webhook_events
                SET processing_status='ENVIRONMENT_MISMATCH',
                    processed_at=CURRENT_TIMESTAMP
                WHERE event_key=?
            """, (event_key,))
            db.commit()
            current_app.logger.error(
                "Cashfree webhook environment did not match local order %s",
                order_id,
            )
            return jsonify({"ok": True, "reconciliation_required": True}), 200

        payment_order_id = str(payment_data.get("order_id") or "").strip()
        if payment_order_id and payment_order_id != order_id:
            db.execute("""
                UPDATE cashfree_webhook_events
                SET processing_status='PAYMENT_ORDER_ID_MISMATCH',
                    processed_at=CURRENT_TIMESTAMP
                WHERE event_key=?
            """, (event_key,))
            db.commit()
            current_app.logger.error(
                "Cashfree payment/order ID mismatch for local order %s",
                order_id,
            )
            return jsonify({"ok": True, "reconciliation_required": True}), 200

        try:
            expected_amount = money(order["amount"])
            order_amount = money(order_data.get("order_amount"))
            payment_amount = money(payment_data.get("payment_amount"))
        except (TypeError, ValueError):
            expected_amount = order_amount = payment_amount = -1

        order_currency = str(order_data.get("order_currency") or "").upper()
        payment_currency = str(payment_data.get("payment_currency") or "").upper()
        if (
            order_currency != "INR"
            or payment_currency != "INR"
            or str(order["currency"] or "").upper() != "INR"
            or abs(order_amount - expected_amount) > 0.004
            or abs(payment_amount - expected_amount) > 0.004
        ):
            db.execute(
                """
                UPDATE cashfree_webhook_events
                SET processing_status='AMOUNT_OR_CURRENCY_MISMATCH',
                    processed_at=CURRENT_TIMESTAMP
                WHERE event_key=?
                """,
                (event_key,),
            )
            db.commit()
            current_app.logger.error(
                "Cashfree webhook amount/currency mismatch for order %s; manual provider reconciliation required",
                order_id,
            )
            return jsonify({"ok": True, "reconciliation_required": True}), 200

        # Handle retries for an already verified order before checking
        # cross-order capture reuse. The same payment ID on this same booked
        # order is a legitimate at-least-once webhook retry.
        current_status = str(order["status"] or "").upper()
        if current_status in {"SUCCESS", "BOOKED"}:
            if str(order["cf_payment_id"] or "") == payment_id:
                event_status = "DUPLICATE_PAYMENT_SUCCESS"
            else:
                event_status = "MULTIPLE_SUCCESSFUL_PAYMENTS"
                current_app.logger.error(
                    "Multiple successful Cashfree payments reported for order %s; manual reconciliation required",
                    order_id,
                )
            db.execute(
                """
                UPDATE cashfree_webhook_events
                SET processing_status=?, processed_at=CURRENT_TIMESTAMP
                WHERE event_key=?
                """,
                (event_status, event_key),
            )
            db.commit()
            return jsonify({
                "ok": True,
                "duplicate": event_status == "DUPLICATE_PAYMENT_SUCCESS",
                "reconciliation_required": event_status == "MULTIPLE_SUCCESSFUL_PAYMENTS",
            }), 200

        if current_status != "PENDING":
            db.execute(
                """
                UPDATE cashfree_webhook_events
                SET processing_status='ORDER_NOT_PAYABLE', processed_at=CURRENT_TIMESTAMP
                WHERE event_key=?
                """,
                (event_key,),
            )
            db.commit()
            return jsonify({"ok": True, "ignored": True}), 200

        payment_type = str(order["payment_type"] or "advance").strip().lower()

        # A capture ID must not be attached to a second local order or payment.
        existing_capture = db.execute(
            """
            SELECT order_id FROM cashfree_payment_orders
            WHERE cf_payment_id=? AND order_id<>? LIMIT 1
            """,
            (payment_id, order_id),
        ).fetchone()
        existing_payment_record = db.execute("""
            SELECT payment_id FROM customer_payment_records
            WHERE payment_id=?
              AND LOWER(COALESCE(method,''))='cashfree'
            LIMIT 1
        """, (payment_id,)).fetchone()

        if existing_capture or existing_payment_record:
            # Quarantine the still-pending order without copying a capture ID
            # which belongs to a different payment. This makes it visible in
            # Admin's PAID_NO_WORKER reconciliation queue and prevents reuse.
            db.execute("""
                UPDATE cashfree_payment_orders
                SET status='PAID_NO_WORKER', cf_payment_id=NULL,
                    payment_method=?, updated_at=CURRENT_TIMESTAMP
                WHERE order_id=? AND status='PENDING'
            """, (
                str(payment_data.get("payment_group") or "online")[:40],
                order_id,
            ))
            db.execute(
                """
                UPDATE cashfree_webhook_events
                SET processing_status='PAYMENT_ID_ALREADY_LINKED',
                    processed_at=CURRENT_TIMESTAMP
                WHERE event_key=?
                """,
                (event_key,),
            )
            db.commit()
            current_app.logger.error(
                "Cashfree payment ID %s was reported against more than one local payment; order %s quarantined",
                payment_id,
                order_id,
            )
            return jsonify({"ok": True, "reconciliation_required": True}), 200

        verified = {
            "cf_payment_id": payment_id,
            "payment_group": str(payment_data.get("payment_group") or "online")[:40],
            "payment_amount": f"{payment_amount:.2f}",
        }
        payment_type = str(order["payment_type"] or "advance").strip().lower()
        if payment_type == "balance":
            # The helper owns the booking/payment transaction. It also commits
            # the event receipt, making retries safe if Cashfree resends it.
            from routes.customer import _finalize_cashfree_balance_payment

            outcome = _finalize_cashfree_balance_payment(
                db,
                order,
                verified,
                int(order["customer_id"]),
                webhook_event_key=event_key,
            )
            return jsonify({"ok": True, "balance_result": outcome}), 200

        updated = db.execute(
            """
            UPDATE cashfree_payment_orders
            SET status='SUCCESS', cf_payment_id=?, payment_method=?,
                verified_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
            WHERE order_id=? AND status='PENDING'
            """,
            (payment_id, verified["payment_group"], order_id),
        )
        if updated.rowcount != 1:
            db.rollback()
            return jsonify({"ok": False, "message": "Payment order changed; retry webhook."}), 503

        db.execute(
            """
            UPDATE cashfree_webhook_events
            SET processing_status='ADVANCE_PAYMENT_VERIFIED',
                processed_at=CURRENT_TIMESTAMP
            WHERE event_key=?
            """,
            (event_key,),
        )
        db.commit()
        return jsonify({"ok": True, "payment_verified": True}), 200

    except Exception:
        db.rollback()
        current_app.logger.exception("Cashfree webhook processing failed")
        return jsonify({"ok": False, "message": "Webhook processing failed; provider may retry."}), 500
    finally:
        db.close()
