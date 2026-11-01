"""Sivdhi booking-assignment response monitor.

This module provides a DB-backed, idempotent reassignment flow for both explicit
rejects and assignments that receive no worker response. It uses SQLite's
BEGIN IMMEDIATE and unique event rows to avoid duplicate reassignment when more
than one app process checks the same booking. For larger production workloads,
run the monitor as its own process and move the app database to PostgreSQL.
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, date, time as clock_time, timedelta
from zoneinfo import ZoneInfo

from database import get_db
from utils.push_notifications import process_push_outbox_once
from utils.sivdhi_finance import (
    ensure_finance_schema,
    find_partner_for_worker,
)
from utils.email_sender import send_email

LOGGER = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")

_MONITOR_THREAD: threading.Thread | None = None
_MONITOR_STOP = threading.Event()


def _bounded_int(
    name: str,
    default: int,
    minimum: int = 1,
    maximum: int = 1440,
) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default

    return max(minimum, min(maximum, value))


def reminder_minutes() -> int:
    return _bounded_int(
        "SIVDHI_ASSIGNMENT_REMINDER_MINUTES",
        5,
        1,
        120,
    )


def reassign_minutes() -> int:
    # Keep the reassignment threshold strictly after the reminder threshold.
    return max(
        reminder_minutes() + 1,
        _bounded_int(
            "SIVDHI_ASSIGNMENT_REASSIGN_MINUTES",
            15,
            2,
            240,
        ),
    )


def ensure_assignment_monitor_schema(conn) -> None:
    """Create additive/idempotent monitor schema and required booking fields."""
    ensure_finance_schema(conn)

    names = {
        row[1]
        for row in conn.execute(
            "PRAGMA table_info(bookings)"
        ).fetchall()
    }

    if "partner_acknowledged_at" not in names:
        conn.execute(
            "ALTER TABLE bookings "
            "ADD COLUMN partner_acknowledged_at TEXT"
        )

    if "assignment_retry_after" not in names:
        conn.execute(
            "ALTER TABLE bookings "
            "ADD COLUMN assignment_retry_after TEXT"
        )

    if "assignment_response_seconds" not in names:
        conn.execute(
            "ALTER TABLE bookings "
            "ADD COLUMN assignment_response_seconds INTEGER"
        )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS booking_assignment_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            booking_db_id INTEGER NOT NULL,
            worker_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            assignment_started_at TEXT NOT NULL DEFAULT '',
            details TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(
                booking_db_id,
                worker_id,
                event_type,
                assignment_started_at
            )
        )
        """
    )

    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_assignment_events_booking
        ON booking_assignment_events(booking_db_id, created_at)
        """
    )
    conn.execute("""
        CREATE TABLE IF NOT EXISTS notification_email_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recipient TEXT NOT NULL,
            subject TEXT NOT NULL,
            body TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            sent_at TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_email_outbox_pending ON notification_email_outbox(status, id)")

    # Live offers let newly registered/activated workers join a booking while
    # preserving the booking's original 60-minute deadline.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS booking_assignment_offers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            booking_db_id INTEGER NOT NULL,
            worker_id INTEGER NOT NULL,
            partner_id TEXT,
            status TEXT NOT NULL DEFAULT 'offered',
            offered_at TEXT NOT NULL,
            allocation_started_at TEXT NOT NULL DEFAULT '',
            response_deadline TEXT NOT NULL,
            responded_at TEXT,
            UNIQUE(booking_db_id, worker_id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_assignment_offers_active ON booking_assignment_offers(booking_db_id, status, response_deadline)")
    offer_columns = {row[1] for row in conn.execute("PRAGMA table_info(booking_assignment_offers)").fetchall()}
    if "allocation_started_at" not in offer_columns:
        conn.execute("ALTER TABLE booking_assignment_offers ADD COLUMN allocation_started_at TEXT NOT NULL DEFAULT ''")

    # Older customer schemas predate the worker/partner email_notification
    # preference. Add it before creating the trigger: otherwise SQLite can
    # create the trigger but fail every customer notification INSERT at runtime
    # with "no such column: email_notification".
    customer_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(customers)").fetchall()
    } if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='customers' LIMIT 1"
    ).fetchone() else set()
    if customer_columns and "email_notification" not in customer_columns:
        conn.execute(
            "ALTER TABLE customers ADD COLUMN email_notification INTEGER DEFAULT 1"
        )

    # Recreate this trigger on startup so existing databases receive any
    # compatibility fixes in the trigger body, not just brand-new databases.
    conn.execute("DROP TRIGGER IF EXISTS trg_sivdhi_notification_email_outbox")

    # Every customer/worker/partner in-app notification also becomes a
    # transactional email event. Resolve internal/public IDs where those
    # columns exist; legacy schemas remain supported.
    has_notifications = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='notifications' LIMIT 1"
    ).fetchone()
    if has_notifications:
        selects = []
        table_names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        customer_cols = {row[1] for row in conn.execute("PRAGMA table_info(customers)").fetchall()} if "customers" in table_names else set()
        worker_cols = {row[1] for row in conn.execute("PRAGMA table_info(workers)").fetchall()} if "workers" in table_names else set()
        partner_cols = {row[1] for row in conn.execute("PRAGMA table_info(partners)").fetchall()} if "partners" in table_names else set()
        email_expr = "'Sivdhi: ' || SUBSTR(COALESCE(NEW.title,'Account update'),1,150)"
        body_expr = "SUBSTR(COALESCE(NEW.message,''),1,12000)"
        if {"id", "email"}.issubset(customer_cols):
            selects.append(f"SELECT email, {email_expr}, {body_expr} FROM customers WHERE LOWER(NEW.user_type)='customer' AND CAST(id AS TEXT)=CAST(NEW.user_id AS TEXT) AND email IS NOT NULL AND TRIM(email)!=''")
        if {"id", "email"}.issubset(worker_cols):
            worker_match = "CAST(id AS TEXT)=CAST(NEW.user_id AS TEXT)"
            if "worker_id" in worker_cols:
                worker_match += " OR CAST(worker_id AS TEXT)=CAST(NEW.user_id AS TEXT)"
            selects.append(f"SELECT email, {email_expr}, {body_expr} FROM workers WHERE LOWER(NEW.user_type)='worker' AND ({worker_match}) AND email IS NOT NULL AND TRIM(email)!=''")
        if {"partner_id", "email"}.issubset(partner_cols):
            partner_match = "CAST(partner_id AS TEXT)=CAST(NEW.user_id AS TEXT)"
            if "id" in partner_cols:
                partner_match += " OR CAST(id AS TEXT)=CAST(NEW.user_id AS TEXT)"
            selects.append(f"SELECT email, {email_expr}, {body_expr} FROM partners WHERE LOWER(NEW.user_type)='partner' AND ({partner_match}) AND email IS NOT NULL AND TRIM(email)!=''")
        if selects:
            conn.execute(
                "CREATE TRIGGER trg_sivdhi_notification_email_outbox AFTER INSERT ON notifications "
                "WHEN LOWER(COALESCE(NEW.user_type,'')) IN ('customer','worker','partner') BEGIN "
                "INSERT INTO notification_email_outbox(recipient,subject,body) "
                + " UNION ALL ".join(selects) + "; END"
            )


def _now_ist() -> datetime:
    return datetime.now(IST)


def _parse_datetime(value) -> datetime | None:
    if not value:
        return None

    try:
        parsed = datetime.fromisoformat(
            str(value).strip().replace("Z", "+00:00")
        )
    except (TypeError, ValueError):
        return None

    # Legacy Sivdhi values without timezone are interpreted as India time.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=IST)

    return parsed.astimezone(IST)


def _assignment_clock_start(booking, now: datetime) -> datetime | None:
    assigned_at = _parse_datetime(
        booking["assigned_at"]
        if "assigned_at" in booking.keys()
        else None
    )

    if assigned_at is None:
        assigned_at = _parse_datetime(
            booking["created_at"]
            if "created_at" in booking.keys()
            else None
        )

    if assigned_at is None:
        return None

    mode = str(
        (
            booking["booking_type"]
            if "booking_type" in booking.keys()
            else None
        )
        or (
            booking["service_type"]
            if "service_type" in booking.keys()
            else None
        )
        or "normal"
    ).strip().lower().replace("_", "-")

    if mode in {"full-day", "fullday", "full day"}:
        raw_date = (
            (
                booking["preferred_date"]
                if "preferred_date" in booking.keys()
                else None
            )
            or (
                booking["booking_date"]
                if "booking_date" in booking.keys()
                else None
            )
        )

        try:
            service_date = date.fromisoformat(str(raw_date)[:10])
        except (TypeError, ValueError):
            return None

        # Full-Day response monitoring starts at 7:00 AM on the service date,
        # even if assignment was made the previous night.
        if service_date != now.date():
            return None

        start_gate = datetime.combine(
            service_date,
            clock_time(7, 0),
            tzinfo=IST,
        )

        return max(assigned_at, start_gate)

    return assigned_at


def _record_event_once(
    conn,
    booking_db_id,
    worker_id,
    event_type,
    assignment_started_at,
    details="",
) -> bool:
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO booking_assignment_events
            (
                booking_db_id,
                worker_id,
                event_type,
                assignment_started_at,
                details
            )
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            int(booking_db_id),
            str(worker_id),
            str(event_type),
            str(assignment_started_at or ""),
            str(details or "")[:500],
        ),
    )

    return cursor.rowcount == 1


def _notify(conn, user_type, user_id, title, message) -> None:
    if user_id is None or str(user_id).strip() == "":
        return

    conn.execute(
        """
        INSERT INTO notifications
            (user_type, user_id, title, message, is_read, created_at)
        VALUES (?, ?, ?, ?, 0, CURRENT_TIMESTAMP)
        """,
        (
            str(user_type),
            str(user_id),
            str(title)[:160],
            str(message)[:1000],
        ),
    )


def _queue_email(conn, user_type, user_id, subject, body) -> None:
    """Queue activity email transactionally; delivery is retried outside booking locks."""
    if user_id is None or str(user_id).strip() == "":
        return
    table, key = {
        "customer": ("customers", "id"),
        "worker": ("workers", "id"),
        "partner": ("partners", "partner_id"),
    }.get(str(user_type), (None, None))
    if not table:
        return
    try:
        row = conn.execute(f'SELECT email FROM "{table}" WHERE CAST("{key}" AS TEXT)=? LIMIT 1', (str(user_id),)).fetchone()
        email = str(row[0] or "").strip() if row else ""
        if not email or "@" not in email:
            return
        conn.execute(
            "INSERT INTO notification_email_outbox(recipient, subject, body) VALUES (?, ?, ?)",
            (email, str(subject)[:180], str(body)[:12000]),
        )
    except Exception:
        LOGGER.exception("Could not queue Sivdhi activity email for %s", user_type)


def _notify_assignment_event(conn, user_type, user_id, title, message) -> None:
    """Create an in-app assignment alert and ensure one email is queued as well.

    The notification trigger normally queues email when email notifications are
    enabled. For critical assignment timeouts, queue an email explicitly when
    the trigger did not create one, so the worker/partner still receives the
    required assignment alert.
    """
    table, key = {
        "customer": ("customers", "id"),
        "worker": ("workers", "id"),
        "partner": ("partners", "partner_id"),
    }.get(str(user_type), (None, None))
    email = ""
    before = 0
    if table and user_id is not None:
        try:
            recipient_row = conn.execute(
                f'SELECT email FROM "{table}" WHERE CAST("{key}" AS TEXT)=? LIMIT 1',
                (str(user_id),),
            ).fetchone()
            email = str(recipient_row[0] or "").strip() if recipient_row else ""
            if email and "@" in email:
                before_row = conn.execute(
                    "SELECT COUNT(*) FROM notification_email_outbox WHERE recipient=? AND body=?",
                    (email, str(message)[:12000]),
                ).fetchone()
                before = int(before_row[0] or 0) if before_row else 0
        except Exception:
            LOGGER.exception("Could not inspect assignment email outbox for %s", user_type)
            email = ""

    _notify(conn, user_type, user_id, title, message)

    if not email or "@" not in email:
        return
    try:
        after_row = conn.execute(
            "SELECT COUNT(*) FROM notification_email_outbox WHERE recipient=? AND body=?",
            (email, str(message)[:12000]),
        ).fetchone()
        after = int(after_row[0] or 0) if after_row else 0
        if after <= before:
            _queue_email(
                conn, user_type, user_id,
                "Sivdhi: " + str(title)[:150], message,
            )
    except Exception:
        LOGGER.exception("Could not ensure assignment email for %s", user_type)


def _deliver_email_outbox(limit: int = 20) -> int:
    """Deliver queued emails outside booking transactions; failures remain retryable."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, recipient, subject, body, attempts FROM notification_email_outbox "
            "WHERE status='pending' AND attempts < 5 ORDER BY id ASC LIMIT ?",
            (max(1, min(int(limit), 100)),),
        ).fetchall()
    finally:
        conn.close()
    sent = 0
    for row in rows:
        try:
            send_email(row["recipient"], row["subject"], row["body"])
            status, error = "sent", None
            sent += 1
        except Exception as exc:
            status, error = "pending", f"{type(exc).__name__}: {exc}"[:500]
        update = get_db()
        try:
            update.execute(
                "UPDATE notification_email_outbox SET status=?, attempts=attempts+1, last_error=?, "
                "sent_at=CASE WHEN ?='sent' THEN CURRENT_TIMESTAMP ELSE sent_at END WHERE id=? AND status='pending'",
                (status, error, status, row["id"]),
            )
            update.commit()
        finally:
            update.close()
    return sent


def _booking_age_minutes(booking, now: datetime) -> float | None:
    created = _parse_datetime(booking["created_at"] if "created_at" in booking.keys() else None)
    if created is None:
        created = _parse_datetime(booking["assigned_at"] if "assigned_at" in booking.keys() else None)
    if created is None:
        return None
    return max(0.0, (now - created).total_seconds() / 60.0)


def _finalize_unassigned_booking(conn, booking) -> None:
    """Stop matching at the one-hour deadline and record an honest refund state."""
    booking_db_id = int(booking["id"])
    booking_ref = str(booking["booking_id"] or booking_db_id)
    customer_id = booking["customer_id"]
    amount = 0.0
    paid_fields = [
        field for field in ("advance_paid_amount", "balance_paid_amount")
        if field in booking.keys()
    ]
    if paid_fields:
        try:
            amount = sum(max(0.0, float(booking[field] or 0)) for field in paid_fields)
        except (TypeError, ValueError):
            amount = 0.0
    if amount <= 0 and "payment_id" in booking.keys() and booking["payment_id"]:
        try:
            payment_row = conn.execute(
                "SELECT amount FROM payments WHERE transaction_id=? AND UPPER(COALESCE(status,'')) IN ('PAID','SUCCESS','COMPLETED','COLLECTED','DEMO_PAID') ORDER BY id DESC LIMIT 1",
                (str(booking["payment_id"]),),
            ).fetchone()
            amount = max(0.0, float(payment_row[0] or 0)) if payment_row else 0.0
        except Exception:
            amount = 0.0
    current_payment = str(booking["payment_status"] if "payment_status" in booking.keys() else "pending").upper()
    paid = current_payment in {"PAID", "SUCCESS", "DEMO_PAID", "PAID_DEMO", "COLLECTED", "COMPLETED", "PARTIALLY_PAID", "PARTIALLY_PAID_DEMO"} or amount > 0
    # Refunds are not automatically marked complete. Admin must transfer the
    # money to the customer's bank first, then explicitly record the refund.
    refund_status = "REFUND_PENDING" if paid else current_payment
    conn.execute(
        "UPDATE bookings SET booking_status='cancelled', status='cancelled', worker_id=NULL, assigned_worker_id=NULL, "
        "partner_id=NULL, assigned_partner_id=NULL, cancellation_reason=?, payment_status=?, "
        "refund_status=?, refund_amount=?, "
        "advance_payment_status=CASE WHEN ? THEN 'REFUND_PENDING' ELSE advance_payment_status END, "
        "updated_at=CURRENT_TIMESTAMP WHERE id=?",
        ("No eligible worker accepted within the 60-minute assignment window.", refund_status,
         "pending_admin" if paid and amount > 0 else "not_required", amount,
         int(paid), booking_db_id),
    )
    conn.execute(
        "UPDATE booking_assignment_offers SET status='cancelled', responded_at=? WHERE booking_db_id=? AND status='offered'",
        (_now_ist().isoformat(timespec="seconds"), booking_db_id),
    )
    current_worker_id = (
        booking["worker_id"] if "worker_id" in booking.keys() else None
    ) or (
        booking["assigned_worker_id"] if "assigned_worker_id" in booking.keys() else None
    )
    current_partner_id = (
        booking["assigned_partner_id"] if "assigned_partner_id" in booking.keys() else None
    ) or (
        booking["partner_id"] if "partner_id" in booking.keys() else None
    )
    if not current_partner_id and current_worker_id:
        try:
            current_partner_id = find_partner_for_worker(conn, int(current_worker_id))
        except (TypeError, ValueError):
            current_partner_id = None
    if current_worker_id:
        _notify_assignment_event(
            conn, "worker", current_worker_id, "Assignment Window Expired",
            f"You did not respond to booking {booking_ref} within your allocated response time. The booking has been closed and offered to no further worker.",
        )
    if current_partner_id:
        _notify_assignment_event(
            conn, "partner", current_partner_id, "Worker Did Not Respond",
            f"The worker assigned to booking {booking_ref} did not respond before the one-hour assignment window ended. The booking has been cancelled and the customer notified.",
        )

    message = (
        f"Your booking {booking_ref} has been cancelled because no worker accepted it within one hour. "
        + (f"A refund of ₹{amount:.2f} is pending Admin bank transfer. You will receive a separate confirmation after the transfer is completed." if paid and amount > 0 else
           "No paid amount was found, so no refund is due.")
    )
    _notify(conn, "customer", customer_id, "Booking Cancelled", message)


def _worker_attempt_status(
    conn,
    booking_db_id,
    worker_db_id,
    status,
    reason,
    distance_km=None,
) -> None:
    """Mark the canonical worker attempt rejected/unresponsive across schemas."""
    from routes.customer import (
        _ensure_worker_attempt_table,
        record_worker_attempt,
    )

    _ensure_worker_attempt_table(conn)

    columns = {
        row[1]
        for row in conn.execute(
            "PRAGMA table_info(booking_worker_attempts)"
        ).fetchall()
    }

    found = conn.execute(
        """
        SELECT id
        FROM booking_worker_attempts
        WHERE booking_id=? AND worker_id=?
        LIMIT 1
        """,
        (str(booking_db_id), int(worker_db_id)),
    ).fetchone()

    if not found:
        record_worker_attempt(
            conn,
            booking_db_id,
            worker_db_id,
            distance_km,
            status,
        )

        found = conn.execute(
            """
            SELECT id
            FROM booking_worker_attempts
            WHERE booking_id=? AND worker_id=?
            LIMIT 1
            """,
            (str(booking_db_id), int(worker_db_id)),
        ).fetchone()

    if not found:
        raise RuntimeError(
            "Could not record worker assignment attempt"
        )

    now = _now_ist().isoformat(timespec="seconds")

    values = {
        "status": status,
        "attempt_status": status,
        "reason": str(reason or "")[:500],
        "rejection_reason": str(reason or "")[:500],
        "updated_at": now,
    }

    fields = [key for key in values if key in columns]

    if fields:
        conn.execute(
            "UPDATE booking_worker_attempts SET "
            + ",".join(f'"{key}"=?' for key in fields)
            + " WHERE id=?",
            [values[key] for key in fields] + [found[0]],
        )


def reassign_booking(
    conn,
    booking,
    old_worker_db_id=None,
    reason="Assigned worker did not respond.",
    attempt_status="no_response",
    notify_no_candidate=True,
) -> dict:
    """Reassign one active booking to the next eligible worker transactionally.

    Caller must hold a write transaction, preferably BEGIN IMMEDIATE, and must
    commit or rollback after this function returns. Notifications are written in
    the same transaction as the assignment to prevent half-completed handoffs.
    """
    from routes.customer import (
        find_nearest_worker,
        get_booking_attempted_worker_ids,
        record_worker_attempt,
    )

    booking_db_id = int(booking["id"])

    customer_id = (
        booking["customer_id"]
        if "customer_id" in booking.keys()
        else None
    )

    current_worker = old_worker_db_id

    if current_worker is None:
        current_worker = (
            booking["worker_id"]
            if "worker_id" in booking.keys()
            else None
        )

    if current_worker is None:
        current_worker = (
            booking["assigned_worker_id"]
            if "assigned_worker_id" in booking.keys()
            else None
        )

    try:
        current_worker = (
            int(current_worker)
            if current_worker is not None
            else None
        )
    except (TypeError, ValueError):
        current_worker = None

    if current_worker is not None:
        _worker_attempt_status(
            conn,
            booking_db_id,
            current_worker,
            attempt_status,
            reason,
            None,
        )

    excluded = get_booking_attempted_worker_ids(
        conn,
        booking_db_id,
    )

    if current_worker is not None:
        excluded.add(str(current_worker))

    candidate = find_nearest_worker(
        conn,
        booking["category"],
        (
            booking["latitude"]
            if "latitude" in booking.keys()
            else None
        ),
        (
            booking["longitude"]
            if "longitude" in booking.keys()
            else None
        ),
        excluded,
        exclude_booking_id=booking_db_id,
    )

    now = _now_ist().isoformat(timespec="seconds")

    if candidate:
        new_worker_id = int(candidate["worker_db_id"])
        new_partner_id = find_partner_for_worker(
            conn,
            new_worker_id,
        )

        if current_worker is None:
            where_guard = (
                "AND worker_id IS NULL "
                "AND assigned_worker_id IS NULL"
            )
            guard_params = []
        else:
            where_guard = (
                "AND (worker_id=? OR assigned_worker_id=?)"
            )
            guard_params = [
                current_worker,
                current_worker,
            ]

        cursor = conn.execute(
            f"""
            UPDATE bookings
            SET booking_status='assigned',
                status='assigned',
                worker_id=?,
                assigned_worker_id=?,
                partner_id=?,
                assigned_partner_id=?,
                assigned_at=?,
                worker_seen_at=NULL,
                partner_acknowledged_at=NULL,
                assignment_retry_after=NULL,
                cancellation_reason=?,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=?
              AND LOWER(
                    COALESCE(
                        NULLIF(booking_status,''),
                        NULLIF(status,''),
                        'pending'
                    )
                  ) IN ('assigned','pending')
              {where_guard}
            """,
            [
                new_worker_id,
                new_worker_id,
                new_partner_id,
                new_partner_id,
                now,
                str(reason or "")[:500],
                booking_db_id,
            ] + guard_params,
        )

        if cursor.rowcount != 1:
            raise RuntimeError(
                "Booking assignment changed while reassignment was in progress"
            )

        record_worker_attempt(
            conn,
            booking_db_id,
            new_worker_id,
            candidate.get("distance_km"),
            "assigned",
        )

        allocated_seconds = 3600
        try:
            allocated_seconds = max(1, int(booking["assignment_response_seconds"] or 3600))
        except (KeyError, TypeError, ValueError):
            pass
        worker_message = (
            f"Booking {booking['booking_id']} has been assigned to you. "
            f"You have {_format_duration(allocated_seconds)} to accept or reject. "
            "If accepted, reach the customer within the stated service window."
        )
        _notify(conn, "worker", new_worker_id, "New Booking Assigned", worker_message)

        if new_partner_id:
            partner_message = (
                f"Booking {booking['booking_id']} is now assigned to another eligible worker. "
                "Please follow up if they do not respond."
            )
            _notify(conn, "partner", new_partner_id, "Worker Assigned to Booking", partner_message)

        # Do not expose worker rejection/reassignment attempts to the customer.
        # The booking status API/UI remains the single customer-facing source.

        _record_event_once(
            conn,
            booking_db_id,
            new_worker_id,
            "assigned_after_reassignment",
            now,
            reason,
        )

        return {
            "reassigned": True,
            "worker": candidate,
            "partner_id": new_partner_id,
        }

    if current_worker is None:
        where_guard = (
            "AND worker_id IS NULL "
            "AND assigned_worker_id IS NULL"
        )
        guard_params = []
    else:
        where_guard = (
            "AND (worker_id=? OR assigned_worker_id=?)"
        )
        guard_params = [
            current_worker,
            current_worker,
        ]

    retry_at = (
        _now_ist()
        + timedelta(
            minutes=_bounded_int(
                "SIVDHI_NO_WORKER_RETRY_MINUTES",
                5,
                1,
                60,
            )
        )
    ).isoformat(timespec="seconds")

    cursor = conn.execute(
        f"""
        UPDATE bookings
        SET booking_status='pending',
            status='pending',
            worker_id=NULL,
            assigned_worker_id=NULL,
            partner_id=NULL,
            assigned_partner_id=NULL,
            assigned_at=NULL,
            worker_seen_at=NULL,
            partner_acknowledged_at=NULL,
            assignment_retry_after=?,
            cancellation_reason=?,
            updated_at=CURRENT_TIMESTAMP
        WHERE id=?
          AND LOWER(
                COALESCE(
                    NULLIF(booking_status,''),
                    NULLIF(status,''),
                    'pending'
                )
              ) IN ('assigned','pending')
          {where_guard}
        """,
        [
            retry_at,
            str(reason or "")[:500],
            booking_db_id,
        ] + guard_params,
    )

    if cursor.rowcount != 1:
        raise RuntimeError(
            "Booking assignment changed while clearing an unresponsive assignment"
        )

    # Customer-facing assignment internals are intentionally silent. The customer
    # sees only the approved lifecycle statuses (confirmed, on the way, started,
    # completed), never retries, worker rejection, or reassignment attempts.

    return {
        "reassigned": False,
        "worker": None,
        "partner_id": None,
    }


def response_seconds_for_count(worker_count) -> int:
    """Allocate the one-hour assignment window evenly across eligible workers."""
    try:
        count = int(worker_count or 0)
    except (TypeError, ValueError):
        count = 0
    return max(1, 3600 // max(1, count))


def reminder_thresholds(response_seconds: int) -> tuple[int, int]:
    """Return early/final reminder offsets strictly inside the response window."""
    window = max(3, int(response_seconds or 0))
    first = max(1, window // 3)
    second = max(first + 1, (window * 2) // 3)
    # Keep the final reminder inside the window even for very short test windows.
    second = min(second, window - 1)
    return first, max(first, second)


def _format_duration(seconds: int) -> str:
    seconds = max(1, int(seconds))
    minutes, remainder = divmod(seconds, 60)
    if minutes and remainder:
        return f"{minutes} minute(s) {remainder} second(s)"
    if minutes:
        return f"{minutes} minute(s)"
    return f"{remainder} second(s)"


def _response_window_seconds(conn, booking) -> int:
    """Return the persisted per-worker slice of the one-hour assignment window."""
    try:
        saved = int(booking["assignment_response_seconds"] or 0)
    except (KeyError, TypeError, ValueError):
        saved = 0
    if saved > 0:
        return min(3600, saved)

    # Legacy bookings predate the stored allocation. Calculate it once from the
    # eligible worker pool and persist it so later retries use the same slice.
    try:
        from routes.customer import count_eligible_workers
        mode = (
            booking["booking_type"] if "booking_type" in booking.keys() else None
        ) or (
            booking["service_type"] if "service_type" in booking.keys() else None
        ) or "normal"
        count = count_eligible_workers(
            conn,
            booking["category"] if "category" in booking.keys() else "",
            booking["latitude"] if "latitude" in booking.keys() else None,
            booking["longitude"] if "longitude" in booking.keys() else None,
            mode=mode,
            preferred_date=(booking["preferred_date"] if "preferred_date" in booking.keys() else None) or (booking["booking_date"] if "booking_date" in booking.keys() else None),
            preferred_time=(booking["preferred_time"] if "preferred_time" in booking.keys() else None) or (booking["booking_time"] if "booking_time" in booking.keys() else None),
            exclude_booking_id=booking["id"],
        )
        seconds = response_seconds_for_count(count)
        conn.execute(
            "UPDATE bookings SET assignment_response_seconds=? WHERE id=?",
            (seconds, int(booking["id"])),
        )
        return seconds
    except Exception:
        LOGGER.exception("Could not calculate worker response allocation; using one-hour fallback")
        return 3600


def _booking_assignment_deadline(booking, now: datetime) -> datetime | None:
    created = _parse_datetime(booking["created_at"] if "created_at" in booking.keys() else None)
    if created is None:
        created = _parse_datetime(booking["assigned_at"] if "assigned_at" in booking.keys() else None)
    if created is None:
        return None
    minutes = _bounded_int("SIVDHI_ASSIGNMENT_DEADLINE_MINUTES", 60, 1, 240)
    return created + timedelta(minutes=minutes)


def _discover_live_worker_offers(conn, booking, now: datetime) -> int:
    """Offer an active booking to every newly eligible worker, without resetting its global deadline."""
    from routes.customer import (
        find_nearest_worker, get_booking_attempted_worker_ids, record_worker_attempt,
    )

    status = str(booking["booking_status"] or booking["status"] or "pending").lower()
    if status not in {"assigned", "pending"}:
        return 0
    deadline = _booking_assignment_deadline(booking, now)
    if deadline is None or now >= deadline:
        return 0
    booking_id = int(booking["id"])
    current_worker = booking["worker_id"] or booking["assigned_worker_id"]
    try:
        current_worker = int(current_worker) if current_worker is not None else None
    except (TypeError, ValueError):
        current_worker = None

    # Make the originally assigned worker part of the offer pool too.
    active_rows = conn.execute(
        "SELECT worker_id FROM booking_assignment_offers WHERE booking_db_id=? AND status='offered'",
        (booking_id,),
    ).fetchall()
    offered_ids = {str(row[0]) for row in active_rows}
    if current_worker is not None and str(current_worker) not in offered_ids:
        partner_id = (booking["assigned_partner_id"] if "assigned_partner_id" in booking.keys() else None) or (booking["partner_id"] if "partner_id" in booking.keys() else None)
        conn.execute(
            "INSERT OR IGNORE INTO booking_assignment_offers(booking_db_id,worker_id,partner_id,status,offered_at,response_deadline) VALUES(?,?,?,'offered',?,?)",
            (booking_id, current_worker, partner_id, now.isoformat(timespec="seconds"), deadline.isoformat(timespec="seconds")),
        )
        offered_ids.add(str(current_worker))

    attempted = get_booking_attempted_worker_ids(conn, booking_id)
    excluded = {str(x) for x in attempted} | offered_ids
    mode = (booking["booking_type"] if "booking_type" in booking.keys() else None) or (booking["service_type"] if "service_type" in booking.keys() else None) or "normal"
    added = 0
    new_offers = []
    # Repeated nearest-worker search yields all eligible workers in nearest-first order.
    for _ in range(2000):
        candidate = find_nearest_worker(
            conn, booking["category"], booking["latitude"], booking["longitude"],
            excluded, exclude_booking_id=booking_id, mode=mode,
            preferred_date=(booking["preferred_date"] if "preferred_date" in booking.keys() else None) or (booking["booking_date"] if "booking_date" in booking.keys() else None),
            preferred_time=(booking["preferred_time"] if "preferred_time" in booking.keys() else None) or (booking["booking_time"] if "booking_time" in booking.keys() else None),
        )
        if not candidate:
            break
        worker_id = int(candidate["worker_db_id"])
        excluded.add(str(worker_id))
        partner_id = find_partner_for_worker(conn, worker_id)
        cursor = conn.execute(
            "INSERT OR IGNORE INTO booking_assignment_offers(booking_db_id,worker_id,partner_id,status,offered_at,response_deadline) VALUES(?,?,?,'offered',?,?)",
            (booking_id, worker_id, partner_id, now.isoformat(timespec="seconds"), deadline.isoformat(timespec="seconds")),
        )
        if cursor.rowcount != 1:
            continue
        added += 1
        record_worker_attempt(conn, booking_id, worker_id, candidate.get("distance_km"), "offered")
        new_offers.append((worker_id, partner_id))

    if added:
        # Re-divide only the remaining time among the currently offered workers.
        # This never moves a response deadline beyond the booking's fixed deadline.
        active = conn.execute(
            "SELECT worker_id FROM booking_assignment_offers WHERE booking_db_id=? AND status='offered'",
            (booking_id,),
        ).fetchall()
        remaining = max(1, int((deadline - now).total_seconds()))
        slice_seconds = max(1, remaining // max(1, len(active)))
        per_worker_deadline = min(deadline, now + timedelta(seconds=slice_seconds))
        conn.execute(
            "UPDATE booking_assignment_offers SET response_deadline=?, allocation_started_at=? WHERE booking_db_id=? AND status='offered'",
            (per_worker_deadline.isoformat(timespec="seconds"), now.isoformat(timespec="seconds"), booking_id),
        )
        conn.execute(
            "UPDATE bookings SET assignment_response_seconds=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (slice_seconds, booking_id),
        )
        booking_ref = str(booking["booking_id"] or booking_id)
        allocation_message = (
            f"Booking {booking_ref}: your current response window is {_format_duration(slice_seconds)} and ends at "
            f"{per_worker_deadline.strftime('%I:%M:%S %p')} IST. The booking's original one-hour deadline remains "
            f"{deadline.strftime('%I:%M %p')} IST."
        )
        # Send the new request once, then update previously-offered workers and
        # their partners because adding a worker changes the fair time split.
        new_worker_ids = {str(worker_id) for worker_id, _ in new_offers}
        new_partner_ids = {str(partner_id) for _, partner_id in new_offers if partner_id}
        active_rows = conn.execute(
            "SELECT worker_id, partner_id FROM booking_assignment_offers WHERE booking_db_id=? AND status='offered'",
            (booking_id,),
        ).fetchall()
        notified_partners = set()
        for active_row in active_rows:
            worker_id = int(active_row["worker_id"])
            _notify_assignment_event(
                conn, "worker", worker_id,
                "New Booking Request" if str(worker_id) in new_worker_ids else "Response Time Updated",
                allocation_message,
            )
            partner_id = active_row["partner_id"]
            partner_key = str(partner_id) if partner_id else ""
            if partner_key and partner_key not in notified_partners:
                notified_partners.add(partner_key)
                _notify_assignment_event(
                    conn, "partner", partner_id,
                    "Eligible Worker Notified" if partner_key in new_partner_ids else "Assignment Time Updated",
                    allocation_message,
                )
    return added


def _send_offer_pool_reminders(conn, booking, active_offers, now: datetime) -> None:
    """Send two worker/partner reminders inside each currently allocated slice."""
    booking_id = int(booking["id"])
    booking_ref = str(booking["booking_id"] or booking_id)
    partner_reminders = set()
    for offer in active_offers:
        worker_id = int(offer["worker_id"])
        allocation_start = _parse_datetime(offer["allocation_started_at"]) or _parse_datetime(offer["offered_at"])
        response_deadline = _parse_datetime(offer["response_deadline"])
        if allocation_start is None or response_deadline is None or now < allocation_start:
            continue
        allocated_seconds = max(1, int((response_deadline - allocation_start).total_seconds()))
        elapsed_seconds = max(0, int((now - allocation_start).total_seconds()))
        first, second = reminder_thresholds(allocated_seconds)
        stage = 2 if elapsed_seconds >= second else 1 if elapsed_seconds >= first else 0
        if not stage:
            continue
        started_key = allocation_start.isoformat(timespec="seconds")
        if _record_event_once(
            conn, booking_id, worker_id, f"offer_pool_reminder_{stage}", started_key,
            f"Worker reminder stage {stage} for {allocated_seconds} seconds allocation",
        ):
            remaining = max(1, int((response_deadline - now).total_seconds()))
            _notify_assignment_event(
                conn, "worker", worker_id, "Booking Response Reminder",
                f"Booking {booking_ref} is still awaiting your response. You have about {_format_duration(remaining)} left in your current allocated window. The original booking deadline does not change.",
            )
        partner_id = offer["partner_id"]
        if partner_id:
            partner_key = (str(partner_id), stage, started_key)
            if partner_key not in partner_reminders and _record_event_once(
                conn, booking_id, str(partner_id), f"partner_offer_pool_reminder_{stage}", started_key,
                f"Partner reminder stage {stage} for active offer pool",
            ):
                partner_reminders.add(partner_key)
                remaining = max(1, int((response_deadline - now).total_seconds()))
                _notify_assignment_event(
                    conn, "partner", partner_id, "Worker Response Reminder",
                    f"Booking {booking_ref} is still awaiting a worker response. The current response window has about {_format_duration(remaining)} left. The original booking deadline does not change.",
                )


def _process_offer_pool(conn, booking, now: datetime) -> str | None:
    """Keep simultaneous offers alive until their divided slice ends; then advance the pool."""
    booking_id = int(booking["id"])
    # Expire overdue offers before discovering new workers so a late arrival
    # cannot accidentally reset an already-expired worker's response window.
    conn.execute(
        "UPDATE booking_assignment_offers SET status='expired', responded_at=? WHERE booking_db_id=? AND status='offered' AND response_deadline<=?",
        (now.isoformat(timespec="seconds"), booking_id, now.isoformat(timespec="seconds")),
    )
    _discover_live_worker_offers(conn, booking, now)
    active = conn.execute(
        "SELECT * FROM booking_assignment_offers WHERE booking_db_id=? AND status='offered' ORDER BY offered_at, worker_id",
        (booking_id,),
    ).fetchall()
    if not active:
        return None
    deadlines = [_parse_datetime(row["response_deadline"]) for row in active]
    deadlines = [value for value in deadlines if value is not None]
    if deadlines and now < min(deadlines):
        _send_offer_pool_reminders(conn, booking, active, now)
        return "waiting_offers"
    # Some active offers may have a later deadline; do not advance the booking
    # while any worker still has valid response time.
    conn.execute(
        "UPDATE booking_assignment_offers SET status='expired', responded_at=? WHERE booking_db_id=? AND status='offered' AND response_deadline<=?",
        (now.isoformat(timespec="seconds"), booking_id, now.isoformat(timespec="seconds")),
    )
    remaining = conn.execute(
        "SELECT 1 FROM booking_assignment_offers WHERE booking_db_id=? AND status='offered' LIMIT 1",
        (booking_id,),
    ).fetchone()
    return "waiting_offers" if remaining else None


def _process_one_booking(booking_db_id: int, now: datetime) -> str:
    conn = get_db()

    try:
        # Schema is initialized once before the pass by
        # process_unanswered_assignments_once(); avoid DDL while holding a
        # booking write lock.
        conn.execute("BEGIN IMMEDIATE")

        booking = conn.execute(
            "SELECT * FROM bookings WHERE id=? LIMIT 1",
            (int(booking_db_id),),
        ).fetchone()

        if not booking:
            conn.rollback()
            return "skipped"

        status = str(
            booking["booking_status"]
            or booking["status"]
            or "pending"
        ).strip().lower()

        worker_id = booking["worker_id"] or booking["assigned_worker_id"]

        if status not in {"assigned", "pending"}:
            conn.rollback()
            return "skipped"

        deadline_minutes = _bounded_int("SIVDHI_ASSIGNMENT_DEADLINE_MINUTES", 60, 1, 240)
        booking_age = _booking_age_minutes(booking, now)
        if booking_age is not None and booking_age >= deadline_minutes:
            if not _record_event_once(
                conn, booking_db_id, "0", "assignment_deadline_expired",
                str(booking["created_at"] or ""),
                f"No worker accepted within {deadline_minutes} minutes.",
            ):
                conn.rollback()
                return "duplicate"
            _finalize_unassigned_booking(conn, booking)
            conn.commit()
            return "cancelled"

        offer_state = _process_offer_pool(conn, booking, now)
        if offer_state == "waiting_offers":
            conn.commit()
            return "waiting"
        # If a simultaneous offer group expired, hand the booking to the next
        # eligible untried worker; the original 60-minute clock still governs.
        refreshed = conn.execute("SELECT * FROM bookings WHERE id=? LIMIT 1", (int(booking_db_id),)).fetchone()
        if refreshed:
            booking = refreshed
            worker_id = booking["worker_id"] or booking["assigned_worker_id"]

        # If no alternate worker was available during a rejection/timeout,
        # retry the matching search at a bounded interval until someone is
        # available. Ordinary unpaid/unassigned pending bookings have no
        # assignment_retry_after value and are deliberately ignored.
        if not worker_id:
            retry_at = _parse_datetime(
                booking["assignment_retry_after"]
                if "assignment_retry_after" in booking.keys()
                else None
            )

            if status != "pending":
                conn.rollback()
                return "skipped"
            if retry_at is None:
                if booking_age is None or booking_age < 1:
                    conn.rollback()
                    return "skipped"
            elif now < retry_at:
                conn.rollback()
                return "skipped"

            result = reassign_booking(
                conn,
                booking,
                None,
                reason=str(
                    booking["cancellation_reason"]
                    or "Retrying worker search"
                ),
                attempt_status="no_response",
                notify_no_candidate=False,
            )

            conn.commit()

            return (
                "reassigned"
                if result["reassigned"]
                else "waiting_retry"
            )

        assigned_at = str(
            booking["assigned_at"]
            or booking["created_at"]
            or ""
        )

        start_at = _assignment_clock_start(booking, now)

        if start_at is None or now < start_at:
            conn.rollback()
            return "skipped"

        partner_id = (
            booking["assigned_partner_id"]
            if "assigned_partner_id" in booking.keys()
            else None
        ) or (
            booking["partner_id"]
            if "partner_id" in booking.keys()
            else None
        )

        if not partner_id:
            partner_id = find_partner_for_worker(
                conn,
                int(worker_id),
            )

        customer_id = booking["customer_id"]
        booking_ref = booking["booking_id"]

        response_seconds = _response_window_seconds(conn, booking)
        elapsed_seconds = (now - start_at).total_seconds()
        # Send two worker/partner reminders while the response window is still
        # open: at one-third and two-thirds of the worker's allocated time.
        # For a 10-minute window, reminders occur at about 3m20s and 6m40s;
        # at the deadline the next worker is assigned immediately.
        first_reminder_seconds, second_reminder_seconds = reminder_thresholds(
            response_seconds
        )

        if elapsed_seconds >= response_seconds:
            if not _record_event_once(
                conn,
                booking_db_id,
                worker_id,
                "no_response_reassignment",
                assigned_at,
                f"Worker did not respond within the allocated {response_seconds} seconds.",
            ):
                conn.rollback()
                return "duplicate"

            # Notify the unresponsive worker and their partner before moving the
            # booking on. The notification trigger queues email when enabled.
            _notify_assignment_event(
                conn, "worker", worker_id, "Response Time Expired",
                f"Your response time for booking {booking_ref} ({_format_duration(response_seconds)} allocated) has expired. The booking is being offered to the next eligible worker.",
            )
            if partner_id:
                _notify_assignment_event(
                    conn, "partner", partner_id, "Worker Did Not Respond",
                    f"The worker assigned to booking {booking_ref} did not respond within their allocated {_format_duration(response_seconds)}. Sivdhi is assigning the next eligible worker.",
                )

            result = reassign_booking(
                conn,
                booking,
                int(worker_id),
                reason=(
                    "Worker did not respond within the allocated "
                    f"{response_seconds} seconds."
                ),
                attempt_status="no_response",
            )

            conn.commit()

            return (
                "reassigned"
                if result["reassigned"]
                else "pending"
            )

        reminder_stage = (
            2 if elapsed_seconds >= second_reminder_seconds
            else 1 if elapsed_seconds >= first_reminder_seconds
            else 0
        )
        if reminder_stage:
            event_type = f"partner_escalation_{reminder_stage}"
            if not _record_event_once(
                conn,
                booking_db_id,
                worker_id,
                event_type,
                assigned_at,
                f"No worker response by reminder stage {reminder_stage}",
            ):
                conn.rollback()
                return "already_reminded"

            seen_at = (
                booking["worker_seen_at"]
                if "worker_seen_at" in booking.keys()
                else None
            )
            if reminder_stage == 1:
                stage_label = "Reminder"
                worker_title = "Booking Response Needed"
                worker_message = (
                    f"Booking {booking_ref} is still awaiting your response. "
                    f"You have {_format_duration(max(1, response_seconds - int(elapsed_seconds)))} left. "
                    "Please accept or reject now. If you do not respond, Sivdhi "
                    "will offer the booking to the next eligible worker."
                )
            else:
                stage_label = "Final reminder"
                worker_title = "Final Booking Response Reminder"
                worker_message = (
                    f"Final reminder for booking {booking_ref}: your response window "
                    f"is nearly over (allocated time: {_format_duration(response_seconds)}). "
                    "Accept or reject now. At the deadline, Sivdhi will immediately "
                    "offer the booking to the next eligible worker."
                )

            _notify_assignment_event(
                conn, "worker", worker_id, worker_title, worker_message
            )

            if partner_id:
                partner_message = (
                    f"{stage_label}: the worker assigned to booking {booking_ref} "
                    f"has not responded{' after opening the booking' if seen_at else ''}. "
                    f"The response window is {_format_duration(response_seconds)} total. "
                    "Please follow up now. At the deadline, Sivdhi will immediately "
                    "offer the booking to the next eligible worker."
                )
                _notify_assignment_event(
                    conn, "partner", partner_id,
                    "Worker Response Needed" if reminder_stage == 1 else "Final Worker Response Reminder",
                    partner_message,
                )

            conn.commit()
            return "reminded"

        conn.rollback()
        return "waiting"

    except Exception:
        conn.rollback()
        LOGGER.exception(
            "Sivdhi assignment monitor failed for booking id=%s",
            booking_db_id,
        )
        return "error"

    finally:
        conn.close()


def process_unanswered_assignments_once(limit: int = 200) -> dict:
    """Run a single monitor pass; safe to call in a test, cron, or daemon."""
    limit = max(1, min(int(limit), 2000))
    now = _now_ist()

    conn = get_db()

    try:
        ensure_assignment_monitor_schema(conn)
        conn.commit()

        rows = conn.execute(
            """
            SELECT id
            FROM bookings
            WHERE LOWER(
                    COALESCE(
                        NULLIF(booking_status,''),
                        NULLIF(status,''),
                        'pending'
                    )
                  ) IN ('assigned','pending')
              AND (
                    worker_id IS NOT NULL
                    OR assigned_worker_id IS NOT NULL
                    OR LOWER(COALESCE(NULLIF(booking_status,''),NULLIF(status,''),'pending'))='pending'
              )
            ORDER BY COALESCE(assignment_retry_after, assigned_at, created_at)
                ASC, id ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

        booking_ids = [
            int(row["id"])
            for row in rows
        ]

    finally:
        conn.close()

    counts = {
        "checked": 0,
        "reminded": 0,
        "reassigned": 0,
        "pending": 0,
        "cancelled": 0,
        "skipped": 0,
        "errors": 0,
    }

    for booking_id in booking_ids:
        outcome = _process_one_booking(booking_id, now)
        counts["checked"] += 1

        if outcome in counts:
            counts[outcome] += 1
        elif outcome in {
            "duplicate",
            "already_reminded",
            "waiting",
            "waiting_retry",
        }:
            counts["skipped"] += 1
        else:
            counts["errors"] += 1

    try:
        _deliver_email_outbox()
    except Exception:
        LOGGER.exception("Sivdhi transactional email outbox processing failed")
    return counts


def _monitor_loop(app=None) -> None:
    interval = _bounded_int(
        "SIVDHI_ASSIGNMENT_MONITOR_INTERVAL_SECONDS",
        2,
        1,
        30,
    )

    while not _MONITOR_STOP.is_set():
        try:
            if app is not None:
                with app.app_context():
                    report = process_unanswered_assignments_once()
            else:
                report = process_unanswered_assignments_once()

            if (
                report.get("reminded")
                or report.get("reassigned")
                or report.get("pending")
                or report.get("errors")
            ):
                LOGGER.info("Sivdhi assignment monitor: %s", report)

            push_report = process_push_outbox_once()
            if push_report.get("sent") or push_report.get("errors"):
                LOGGER.info("Sivdhi push dispatcher: %s", push_report)

        except Exception:
            LOGGER.exception(
                "Sivdhi assignment monitor loop failed"
            )

        _MONITOR_STOP.wait(interval)


def start_assignment_monitor(app=None) -> None:
    """Start one in-process daemon; WSGI deployments should run it separately."""
    global _MONITOR_THREAD

    if (
        os.getenv("SIVDHI_ASSIGNMENT_MONITOR_ENABLED", "1")
        .strip()
        .lower()
        in {"0", "false", "no", "off"}
    ):
        LOGGER.info("Sivdhi assignment monitor disabled by environment")
        return

    if _MONITOR_THREAD is not None and _MONITOR_THREAD.is_alive():
        return

    _MONITOR_STOP.clear()

    _MONITOR_THREAD = threading.Thread(
        target=_monitor_loop,
        args=(app,),
        name="sivdhi-assignment-monitor",
        daemon=True,
    )

    _MONITOR_THREAD.start()


def run_assignment_monitor_forever() -> None:
    """Foreground monitor process for production deployments."""
    ensure_conn = get_db()

    try:
        ensure_assignment_monitor_schema(ensure_conn)
        ensure_conn.commit()
    finally:
        ensure_conn.close()

    _monitor_loop(None)


if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

    # Importing app runs the project's idempotent table migrations before the
    # monitor begins polling. No web server is started by this import.
    import app as _sivdhi_app  # noqa: F401

    run_assignment_monitor_forever()