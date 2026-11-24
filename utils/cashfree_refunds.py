"""Idempotent automatic Cashfree refunds for cancelled Sivdhi bookings."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from database import get_db
from utils.cashfree_gateway import (
    CashfreeGatewayError,
    create_cashfree_refund,
    fetch_cashfree_refund,
)
from utils.sivdhi_finance import (
    add_finance_ledger_entry,
    ensure_finance_schema,
    money,
)

LOGGER = logging.getLogger(__name__)


def _utc_sql_time() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _booking_is_cancelled(booking) -> bool:
    if not booking:
        return False
    keys = set(booking.keys())
    states = {
        str(booking[key] or "").strip().lower()
        for key in ("status", "booking_status")
        if key in keys and str(booking[key] or "").strip()
    }
    return bool(states) and states.issubset({"cancelled", "canceled"})


def queue_cashfree_refunds_for_booking(
    conn,
    booking_ref: str,
    reason: str,
) -> dict:
    """Queue full refunds only for verified, booked, real Production captures.

    Sandbox, missing-payment, and manually tracked refunds are never sent to
    the live provider automatically. Those cases remain visible for review.
    """
    ensure_finance_schema(conn)

    booking = conn.execute("""
        SELECT id, booking_id, customer_id, status, booking_status,
               advance_paid_amount, balance_paid_amount, payment_status
        FROM bookings
        WHERE CAST(booking_id AS TEXT)=CAST(? AS TEXT)
        LIMIT 1
    """, (str(booking_ref),)).fetchone()

    if not booking or not _booking_is_cancelled(booking):
        return {"queued": 0, "amount": 0.0, "review": 1}

    orders = conn.execute("""
        SELECT order_id, customer_id, amount, currency, status,
               environment, cf_payment_id, payment_type
        FROM cashfree_payment_orders
        WHERE CAST(booking_id AS TEXT)=CAST(? AS TEXT)
          AND customer_id=?
          AND status='BOOKED'
        ORDER BY created_at, order_id
    """, (str(booking["booking_id"]), booking["customer_id"])).fetchall()

    queued = 0
    queued_amount = 0.0
    review = 0

    for order in orders:
        environment = str(order["environment"] or "").strip().lower()
        payment_id = str(order["cf_payment_id"] or "").strip()
        payment_type = str(order["payment_type"] or "advance").strip().lower()
        order_amount = money(order["amount"])

        # Live automatic refunds must never use Sandbox or unknown credentials.
        if environment != "production" or not payment_id or order_amount <= 0:
            review += 1
            continue

        if str(order["currency"] or "").upper() != "INR":
            review += 1
            continue

        payment = conn.execute("""
            SELECT customer_id, amount, method, status, payment_type, is_demo
            FROM customer_payment_records
            WHERE CAST(booking_id AS TEXT)=CAST(? AS TEXT)
              AND payment_id=?
            LIMIT 1
        """, (str(booking["booking_id"]), payment_id)).fetchone()

        if (
            not payment
            or str(payment["customer_id"]) != str(booking["customer_id"])
            or str(payment["method"] or "").lower() != "cashfree"
            or str(payment["status"] or "").upper() not in {"PAID", "SUCCESS"}
            or int(payment["is_demo"] or 0) != 0
            or str(payment["payment_type"] or "advance").lower() != payment_type
            or abs(money(payment["amount"]) - order_amount) > 0.004
        ):
            review += 1
            continue

        # Existing manual reconciliation means a human may already have
        # initiated a provider refund. Never create an automatic second one.
        manual = conn.execute("""
            SELECT 1 FROM cashfree_manual_refunds
            WHERE order_id=? LIMIT 1
        """, (order["order_id"],)).fetchone()

        if manual:
            review += 1
            continue

        already = conn.execute("""
            SELECT status FROM cashfree_auto_refunds
            WHERE order_id=? LIMIT 1
        """, (order["order_id"],)).fetchone()

        if already:
            existing_status = str(already["status"] or "").strip().upper()
            if existing_status in {"QUEUED", "CREATING", "PENDING", "POLLING"}:
                queued += 1
                queued_amount += order_amount
            else:
                review += 1
            continue

        refund_id = "SIVDHIREF" + uuid.uuid4().hex[:20]
        idempotency_key = str(uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"sivdhi-cashfree-refund:{order['order_id']}:{refund_id}",
        ))

        cursor = conn.execute("""
            INSERT OR IGNORE INTO cashfree_auto_refunds
                (order_id, booking_id, customer_id, payment_type, payment_id,
                 refund_id, idempotency_key, amount, environment, status,
                 next_attempt_at, reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'QUEUED', CURRENT_TIMESTAMP, ?)
        """, (
            order["order_id"], booking["booking_id"], booking["customer_id"],
            payment_type, payment_id, refund_id, idempotency_key,
            order_amount, environment, str(reason or "")[:500],
        ))

        if cursor.rowcount == 1:
            queued += 1
            queued_amount += order_amount

    if queued:
        conn.execute("""
            UPDATE bookings
            SET payment_status='REFUND_PENDING',
                refund_status='pending_cashfree',
                advance_payment_status=CASE
                    WHEN EXISTS (
                        SELECT 1 FROM cashfree_auto_refunds r
                        WHERE r.booking_id=bookings.booking_id
                          AND r.payment_type='advance'
                          AND r.status<>'SUCCESS'
                    ) THEN 'REFUND_PENDING'
                    ELSE advance_payment_status
                END,
                balance_payment_status=CASE
                    WHEN EXISTS (
                        SELECT 1 FROM cashfree_auto_refunds r
                        WHERE r.booking_id=bookings.booking_id
                          AND r.payment_type='balance'
                          AND r.status<>'SUCCESS'
                    ) THEN 'REFUND_PENDING'
                    ELSE balance_payment_status
                END,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=?
        """, (booking["id"],))

    return {
        "queued": queued,
        "amount": round(queued_amount, 2),
        "review": review,
        "reason": str(reason or "")[:500],
    }


def _job_error(job_id: int, message: str, *, retryable: bool, creating: bool) -> None:
    db = get_db()
    try:
        status = ("CREATING" if creating else "PENDING") if retryable else "REVIEW_REQUIRED"
        db.execute("""
            UPDATE cashfree_auto_refunds
            SET status=?, last_error=?, next_attempt_at=datetime('now','+60 seconds'),
                updated_at=CURRENT_TIMESTAMP
            WHERE id=? AND status IN ('CREATING','POLLING')
        """, (status, str(message)[:500], job_id))
        db.commit()
    finally:
        db.close()


def _provider_result(job_id: int, payload: dict) -> None:
    db = get_db()
    try:
        ensure_finance_schema(db)
        db.execute("BEGIN IMMEDIATE")

        job = db.execute("""
            SELECT * FROM cashfree_auto_refunds WHERE id=? LIMIT 1
        """, (job_id,)).fetchone()

        if not job or str(job["status"]).upper() == "SUCCESS":
            db.rollback()
            return

        order = db.execute("""
            SELECT * FROM cashfree_payment_orders WHERE order_id=? LIMIT 1
        """, (job["order_id"],)).fetchone()

        if not order:
            db.execute("""
                UPDATE cashfree_auto_refunds
                SET status='REVIEW_REQUIRED', last_error='Linked Cashfree order is missing.',
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=?
            """, (job_id,))
            db.commit()
            return

        returned_order = str(payload.get("order_id") or "")
        returned_refund = str(payload.get("refund_id") or "")
        returned_amount = money(payload.get("refund_amount"))
        returned_currency = str(payload.get("refund_currency") or "").strip().upper()
        returned_payment = str(payload.get("cf_payment_id") or "")
        status = str(payload.get("refund_status") or "").strip().upper()

        # Revalidate the original capture against both local records at the
        # moment of refund settlement. A provider refund response alone must
        # never attach money to a different booking/payment.
        booking = db.execute("""
            SELECT id, booking_id, customer_id, status, booking_status,
                   advance_paid_amount, balance_paid_amount, refund_amount
            FROM bookings
            WHERE CAST(booking_id AS TEXT)=CAST(? AS TEXT)
            LIMIT 1
        """, (str(job["booking_id"]),)).fetchone()

        payment = db.execute("""
            SELECT customer_id, amount, method, status, payment_type, is_demo
            FROM customer_payment_records
            WHERE CAST(booking_id AS TEXT)=CAST(? AS TEXT)
              AND payment_id=?
            LIMIT 1
        """, (str(job["booking_id"]), str(job["payment_id"]))).fetchone()

        valid = (
            returned_order == str(job["order_id"])
            and returned_refund == str(job["refund_id"])
            and abs(returned_amount - money(job["amount"])) <= 0.004
            and returned_currency == "INR"
            and returned_payment == str(job["payment_id"])
            and _booking_is_cancelled(booking)
            and str(booking["booking_id"]) == str(job["booking_id"])
            and str(booking["customer_id"]) == str(job["customer_id"])
            and str(job["environment"] or "").strip().lower() == "production"
            and str(order["environment"] or "").strip().lower() == "production"
            and str(order["order_id"]) == str(job["order_id"])
            and str(order["booking_id"] or "") == str(job["booking_id"])
            and str(order["customer_id"]) == str(job["customer_id"])
            and str(order["cf_payment_id"] or "") == str(job["payment_id"])
            and str(order["payment_type"] or "advance").lower()
                == str(job["payment_type"] or "advance").lower()
            and payment is not None
            and str(payment["customer_id"]) == str(job["customer_id"])
            and str(payment["method"] or "").lower() == "cashfree"
            and str(payment["status"] or "").upper() in {"PAID", "SUCCESS"}
            and int(payment["is_demo"] or 0) == 0
            and str(payment["payment_type"] or "advance").lower()
                == str(job["payment_type"] or "advance").lower()
            and abs(money(payment["amount"]) - money(job["amount"])) <= 0.004
        )

        if not valid:
            db.execute("""
                UPDATE cashfree_auto_refunds
                SET status='REVIEW_REQUIRED',
                    provider_status=?,
                    last_error='Cashfree refund identity, environment, payment, currency or amount did not match.',
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=?
            """, (status[:80], job_id))
            db.commit()
            return

        cf_refund_id = str(payload.get("cf_refund_id") or "").strip()
        refund_arn = str(payload.get("refund_arn") or "").strip()

        if cf_refund_id:
            duplicate_provider_refund = db.execute("""
                SELECT 1
                FROM cashfree_auto_refunds
                WHERE cf_refund_id=? AND id<>?
                LIMIT 1
            """, (cf_refund_id, job_id)).fetchone()

            if duplicate_provider_refund:
                db.execute("""
                    UPDATE cashfree_auto_refunds
                    SET status='REVIEW_REQUIRED',
                        provider_status=?,
                        last_error='Cashfree Refund ID is already associated with another automatic refund.',
                        updated_at=CURRENT_TIMESTAMP
                    WHERE id=?
                """, (status[:80], job_id))
                db.commit()
                return

        if status in {
            "PENDING", "IN_PROGRESS", "PROCESSING",
            "PENDING_APPROVAL", "ONHOLD",
        }:
            previous_notice = job["refund_initiated_notified_at"]
            poll_delay = (
                "+5 minutes"
                if status in {"PENDING_APPROVAL", "ONHOLD"}
                else "+60 seconds"
            )
            db.execute("""
                UPDATE cashfree_auto_refunds
                SET status='PENDING', provider_status=?,
                    cf_refund_id=?, refund_arn=?,
                    next_attempt_at=datetime('now', ?),
                    refund_initiated_notified_at=COALESCE(
                        refund_initiated_notified_at, CURRENT_TIMESTAMP
                    ),
                    last_error=NULL, updated_at=CURRENT_TIMESTAMP
                WHERE id=?
            """, (
                status, cf_refund_id or None, refund_arn or None,
                poll_delay, job_id,
            ))

            if not previous_notice and job["customer_id"]:
                db.execute("""
                    INSERT INTO notifications
                        (user_type,user_id,title,message,is_read,created_at)
                    VALUES ('customer',?, 'Refund Initiated', ?,0,CURRENT_TIMESTAMP)
                """, (
                    str(job["customer_id"]),
                    f"Your ₹{money(job['amount']):.2f} refund for booking "
                    f"{job['booking_id']} is being processed. "
                    f"Reference: {cf_refund_id or job['refund_id']}.",
                ))
            db.commit()
            return

        if status in {"FAILED", "CANCELLED", "REVERSED", "REJECTED"}:
            db.execute("""
                UPDATE cashfree_auto_refunds
                SET status='FAILED', provider_status=?,
                    cf_refund_id=?, refund_arn=?,
                    last_error='Cashfree reported a failed, rejected or cancelled refund.',
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=?
            """, (status, cf_refund_id or None, refund_arn or None, job_id))
            db.execute("""
                UPDATE bookings
                SET refund_status='pending_admin', updated_at=CURRENT_TIMESTAMP
                WHERE CAST(booking_id AS TEXT)=CAST(? AS TEXT)
                  AND NOT EXISTS (
                      SELECT 1 FROM cashfree_auto_refunds r
                      WHERE CAST(r.booking_id AS TEXT)=CAST(bookings.booking_id AS TEXT)
                        AND r.status IN ('QUEUED','CREATING','PENDING','POLLING')
                  )
            """, (str(job["booking_id"]),))
            if job["customer_id"]:
                db.execute("""
                    INSERT INTO notifications
                        (user_type,user_id,title,message,is_read,created_at)
                    VALUES ('customer',?, 'Refund Failed', ?,0,CURRENT_TIMESTAMP)
                """, (
                    str(job["customer_id"]),
                    f"Your ₹{money(job['amount']):.2f} refund for booking "
                    f"{job['booking_id']} could not be completed. "
                    f"Reference: {cf_refund_id or job['refund_id']}.",
                ))
            db.commit()
            return

        if status != "SUCCESS":
            db.execute("""
                UPDATE cashfree_auto_refunds
                SET status='REVIEW_REQUIRED', provider_status=?,
                    last_error='Unrecognized Cashfree refund status.',
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=?
            """, (status[:80], job_id))
            db.commit()
            return

        # Only this transition records the refund financially. The row lock and
        # SUCCESS guard prevent duplicate ledger entries/notifications.
        if str(order["status"]).upper() != "BOOKED":
            db.execute("""
                UPDATE cashfree_auto_refunds
                SET status='REVIEW_REQUIRED',
                    last_error='Cashfree order is not in the expected BOOKED state.',
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=?
            """, (job_id,))
            db.commit()
            return

        amount = money(job["amount"])
        previous_refunds = money(booking["refund_amount"])
        total_refunds = round(previous_refunds + amount, 2)
        total_paid = round(
            money(booking["advance_paid_amount"])
            + money(booking["balance_paid_amount"]), 2
        )
        if total_paid <= 0 or total_refunds > total_paid + 0.004:
            db.execute("""
                UPDATE cashfree_auto_refunds
                SET status='REVIEW_REQUIRED',
                    last_error='Refund total exceeds the recorded successful payments.',
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=?
            """, (job_id,))
            db.commit()
            return

        fully_refunded = total_refunds >= total_paid - 0.004
        refund_status = "refunded" if fully_refunded else "partially_refunded"
        payment_status = "REFUNDED" if fully_refunded else "PARTIALLY_REFUNDED"
        payment_type = str(job["payment_type"]).lower()

        db.execute("""
            UPDATE cashfree_auto_refunds
            SET status='SUCCESS', provider_status='SUCCESS',
                cf_refund_id=?, refund_arn=?,
                last_error=NULL, completed_at=CURRENT_TIMESTAMP,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=? AND status<>'SUCCESS'
        """, (cf_refund_id or None, refund_arn or None, job_id))

        db.execute("""
            UPDATE cashfree_payment_orders
            SET status='REFUNDED', updated_at=CURRENT_TIMESTAMP
            WHERE order_id=? AND status='BOOKED'
        """, (job["order_id"],))

        db.execute("""
            UPDATE bookings
            SET refund_status=?, refund_amount=?,
                payment_status=?,
                advance_payment_status=CASE
                    WHEN ?='advance' THEN 'REFUNDED'
                    ELSE advance_payment_status END,
                balance_payment_status=CASE
                    WHEN ?='balance' THEN 'REFUNDED'
                    ELSE balance_payment_status END,
                refund_reference=?, refund_processed_at=CURRENT_TIMESTAMP,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=?
        """, (
            refund_status, total_refunds, payment_status,
            payment_type, payment_type,
            cf_refund_id or str(job["refund_id"]), booking["id"],
        ))

        add_finance_ledger_entry(
            db, str(job["booking_id"]), "customer_refund",
            amount, "refunded", "live",
            cf_refund_id or str(job["refund_id"]),
            "Cashfree automatic refund verified successfully through the provider API.",
        )

        if booking["customer_id"]:
            message = (
                f"Your ₹{amount:.2f} refund for booking {job['booking_id']} "
                f"is successful. Reference: {cf_refund_id or job['refund_id']}."
            )
            db.execute("""
                INSERT INTO notifications
                    (user_type,user_id,title,message,is_read,created_at)
                VALUES ('customer',?,?,?,0,CURRENT_TIMESTAMP)
            """, (
                str(booking["customer_id"]),
                "Refund Successful",
                message,
            ))

        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def process_cashfree_refunds_once(limit: int = 10) -> dict:
    """Submit queued refunds and poll pending provider refunds safely."""
    db = get_db()
    claimed = []

    try:
        ensure_finance_schema(db)
        db.commit()
        db.execute("BEGIN IMMEDIATE")

        rows = db.execute("""
            SELECT * FROM cashfree_auto_refunds
            WHERE status='QUEUED'
               OR (
                    status IN ('CREATING','PENDING','POLLING')
                    AND next_attempt_at<=CURRENT_TIMESTAMP
               )
            ORDER BY created_at, id
            LIMIT ?
        """, (max(1, min(int(limit), 25)),)).fetchall()

        for row in rows:
            old_status = str(row["status"]).upper()
            if old_status in {"QUEUED", "CREATING"}:
                operation = "create_or_recover"
                new_status = "CREATING"
            else:
                operation = "poll"
                new_status = "POLLING"

            db.execute("""
                UPDATE cashfree_auto_refunds
                SET status=?, attempts=attempts+1,
                    next_attempt_at=datetime('now','+3 minutes'),
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=?
            """, (new_status, row["id"]))

            claimed.append({
                "id": row["id"],
                "order_id": row["order_id"],
                "refund_id": row["refund_id"],
                "idempotency_key": row["idempotency_key"],
                "amount": row["amount"],
                "environment": row["environment"],
                "reason": row["reason"] if "reason" in row.keys() else "",
                "operation": operation,
                "payment_id": row["payment_id"],
            })

        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    report = {"checked": 0, "initiated": 0, "successful": 0, "pending": 0, "errors": 0}

    for job in claimed:
        report["checked"] += 1
        try:
            provider = None

            if job["operation"] == "create_or_recover":
                # A previous process may have timed out after Cashfree accepted
                # the create request. Fetch first, then retry POST only with
                # exactly the same refund ID and idempotency key.
                if job["operation"] == "create_or_recover":
                    provider = fetch_cashfree_refund(
                        job["order_id"], job["refund_id"], job["environment"]
                    )

                if provider is None:
                    provider = create_cashfree_refund(
                        job["order_id"],
                        job["refund_id"],
                        job["amount"],
                        job["environment"],
                        job["idempotency_key"],
                        "Sivdhi cancelled booking refund",
                    )
                    report["initiated"] += 1
            else:
                provider = fetch_cashfree_refund(
                    job["order_id"], job["refund_id"], job["environment"]
                )

            if provider is None:
                if job["operation"] == "poll":
                    _job_error(
                        job["id"],
                        "Cashfree refund is not visible yet; next status check is scheduled.",
                        retryable=True,
                        creating=False,
                    )
                    report["pending"] += 1
                    continue
                raise CashfreeGatewayError(
                    "Cashfree refund was not found after the create/recovery attempt.",
                    retryable=True,
                )

            before = str(provider.get("refund_status") or "").upper()
            _provider_result(job["id"], provider)

            after_db = get_db()
            try:
                state_row = after_db.execute(
                    "SELECT status FROM cashfree_auto_refunds WHERE id=?",
                    (job["id"],),
                ).fetchone()
                final_state = str(state_row["status"] or "").upper() if state_row else ""
            finally:
                after_db.close()

            if final_state == "SUCCESS":
                report["successful"] += 1
            elif final_state in {"PENDING", "POLLING"}:
                report["pending"] += 1
            elif final_state in {"FAILED", "REVIEW_REQUIRED"}:
                report["errors"] += 1
            elif before not in {"SUCCESS", "PENDING", "IN_PROGRESS", "PROCESSING"}:
                report["errors"] += 1

        except CashfreeGatewayError as exc:
            _job_error(
                job["id"],
                str(exc),
                retryable=exc.retryable,
                creating=job["operation"] == "create_or_recover",
            )
            report["errors"] += 1
            LOGGER.warning(
                "Cashfree automatic refund job %s needs retry/review: %s",
                job["id"], exc,
            )
        except Exception:
            _job_error(
                job["id"],
                "Unexpected refund processor error; see monitor log.",
                retryable=True,
                creating=job["operation"] == "create_or_recover",
            )
            report["errors"] += 1
            LOGGER.exception("Cashfree automatic refund job %s failed", job["id"])

    return report
