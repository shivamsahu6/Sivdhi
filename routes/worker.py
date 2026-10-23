from flask import (
    Blueprint,
    render_template,
    session,
    request,
    redirect,
    url_for,
    flash,
    jsonify,
    current_app
)

from werkzeug.utils import secure_filename

from werkzeug.security import (
    check_password_hash,
    generate_password_hash
)

import os
import secrets
import re

from datetime import datetime, timedelta

from utils.email_sender import send_otp
from utils.otp_security import reserve_otp_request
from utils.pending_changes import issue_pending_change, refresh_pending_change, verify_pending_change, delete_pending_change
from database import get_db
from utils.sivdhi_finance import (
    ensure_finance_schema, is_demo_mode, add_finance_ledger_entry,
    settle_completed_booking, money as finance_money,
)
from routes.decorators import worker_required
from utils.assignment_monitor import ensure_assignment_monitor_schema, reassign_booking
from routes.customer import (
    find_nearest_worker,
    get_booking_attempted_worker_ids,
    record_worker_attempt,
    mark_worker_attempt_rejected
)


# ============================================================
# BLUEPRINT
# ============================================================

worker_bp = Blueprint(
    "worker",
    __name__
)


# ============================================================
# CONFIG
# ============================================================

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "static", "uploads")

INDIAN_BANKS = [
    "State Bank of India", "HDFC Bank", "ICICI Bank", "Axis Bank",
    "Kotak Mahindra Bank", "IndusInd Bank", "Yes Bank", "Punjab National Bank",
    "Bank of Baroda", "Canara Bank", "Union Bank of India", "Bank of India",
    "Indian Bank", "Central Bank of India", "Indian Overseas Bank",
    "UCO Bank", "Bank of Maharashtra", "Punjab & Sind Bank", "IDBI Bank",
    "Federal Bank", "South Indian Bank", "Bandhan Bank", "RBL Bank",
    "AU Small Finance Bank", "Equitas Small Finance Bank", "ESAF Small Finance Bank",
    "Jana Small Finance Bank", "Ujjivan Small Finance Bank", "Karnataka Bank",
    "Karur Vysya Bank", "Tamilnad Mercantile Bank", "DCB Bank", "City Union Bank",
    "Dhanlaxmi Bank", "CSB Bank", "Jammu & Kashmir Bank"
]

ALLOWED_IMAGE_EXTENSIONS = {
    "jpg",
    "jpeg",
    "png",
    "webp"
}

# IFSC bank-code prefixes used for the bank/IFSC consistency check.
# This is intentionally limited to stable, well-known prefixes; an unknown
# prefix is still accepted when the IFSC format itself is valid.
BANK_IFSC_PREFIXES = {
    "State Bank of India": {"SBIN"},
    "HDFC Bank": {"HDFC"},
    "ICICI Bank": {"ICIC"},
    "Axis Bank": {"UTIB"},
    "Kotak Mahindra Bank": {"KKBK"},
    "IndusInd Bank": {"INDB"},
    "Yes Bank": {"YESB"},
    "Punjab National Bank": {"PUNB"},
    "Bank of Baroda": {"BARB"},
    "Canara Bank": {"CNRB"},
    "Union Bank of India": {"UBIN"},
    "Bank of India": {"BKID"},
    "Indian Bank": {"IDIB"},
    "Central Bank of India": {"CBIN"},
    "Indian Overseas Bank": {"IOBA"},
    "UCO Bank": {"UCBA"},
    "Bank of Maharashtra": {"MAHB"},
    "Punjab & Sind Bank": {"PSIB"},
    "IDBI Bank": {"IBKL"},
    "Federal Bank": {"FDRL"},
    "South Indian Bank": {"SIBL"},
    "Bandhan Bank": {"BDBL"},
    "RBL Bank": {"RATN"},
    "AU Small Finance Bank": {"AUBL"},
    "Equitas Small Finance Bank": {"ESFB"},
    "ESAF Small Finance Bank": {"ESAF"},
    "Jana Small Finance Bank": {"JSFB"},
    "Ujjivan Small Finance Bank": {"UJVN"},
    "Karnataka Bank": {"KARB"},
    "Karur Vysya Bank": {"KVBL"},
    "Tamilnad Mercantile Bank": {"TMBL"},
    "DCB Bank": {"DCBL"},
    "City Union Bank": {"CIUB"},
    "Dhanlaxmi Bank": {"DLXB"},
    "CSB Bank": {"CSBK"},
    "Jammu & Kashmir Bank": {"JAKA"},
}


# ============================================================
# COMMON HELPERS
# ============================================================

def generate_otp():
    """
    Generate a secure 6-digit OTP.
    """

    return str(
        secrets.randbelow(900000) + 100000
    )


def set_otp_expiry(
    session_key="password_otp_expiry",
    minutes=5
):
    """
    Store OTP expiry time in session.
    """

    session[session_key] = (
        datetime.now()
        + timedelta(minutes=minutes)
    ).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def is_otp_expired(
    session_key
):
    """
    Check whether an OTP expiry stored in
    session is missing, invalid, or expired.
    """

    expiry_value = session.get(
        session_key
    )

    if not expiry_value:
        return True

    try:

        expiry = datetime.strptime(
            expiry_value,
            "%Y-%m-%d %H:%M:%S"
        )

    except (
        ValueError,
        TypeError
    ):

        return True

    return datetime.now() > expiry


MAX_IMAGE_SIZE = 5 * 1024 * 1024

def allowed_image_file(filename):
    """Validate the uploaded image filename extension."""
    if not filename or "." not in filename:
        return False
    extension = filename.rsplit(".", 1)[1].lower()
    return extension in ALLOWED_IMAGE_EXTENSIONS

def validate_uploaded_image(photo):
    """Validate image extension, size and file signature."""
    if not photo or not photo.filename:
        return False, "Please select an image."
    if not allowed_image_file(photo.filename):
        return False, "Only JPG, JPEG, PNG and WEBP images are allowed."
    try:
        photo.stream.seek(0, os.SEEK_END)
        size = photo.stream.tell()
        photo.stream.seek(0)
    except (OSError, ValueError):
        return False, "Could not read the uploaded image."
    if size <= 0:
        return False, "Uploaded image is empty."
    if size > MAX_IMAGE_SIZE:
        return False, "Image size must be 5 MB or less."
    header = photo.stream.read(12)
    photo.stream.seek(0)
    extension = photo.filename.rsplit(".", 1)[1].lower()
    is_jpeg = header.startswith(b"\xff\xd8\xff")
    is_png = header.startswith(b"\x89PNG\r\n\x1a\n")
    is_webp = header[:4] == b"RIFF" and header[8:12] == b"WEBP"
    # Do not save mismatched file signatures/extensions; this prevents confusing
    # MIME handling and makes the accepted upload type explicit.
    matches_extension = (
        (extension in {"jpg", "jpeg"} and is_jpeg)
        or (extension == "png" and is_png)
        or (extension == "webp" and is_webp)
    )
    if not matches_extension:
        return False, "The image contents do not match its file extension."
    return True, ""


def get_current_worker():

    worker_id = session.get(
        "user_id"
    )

    if not worker_id:
        return None

    conn = get_db()

    try:

        worker = conn.execute(
            """
            SELECT *
            FROM workers
            WHERE worker_id = ?
            LIMIT 1
            """,
            (worker_id,)
        ).fetchone()

        return worker

    except Exception as error:

        print(
            "GET CURRENT WORKER ERROR:",
            error
        )

        return None

    finally:

        conn.close()


def get_current_worker_db_id():

    worker = get_current_worker()

    if not worker:
        return None

    return worker["id"]


def normalize_worker_assignment(conn, booking_id, worker_db_id):
    """Keep the legacy/public assignment column aligned with worker_id."""
    try:
        conn.execute(
            """
            UPDATE bookings
            SET worker_id = ?, assigned_worker_id = ?
            WHERE id = ?
            """,
            (worker_db_id, worker_db_id, booking_id)
        )
    except Exception as error:
        # Older databases may not have assigned_worker_id yet. The main
        # worker_id assignment remains authoritative in that case.
        print("WORKER ASSIGNMENT SYNC WARNING:", error)


def get_booking_for_worker(
    booking_value
):

    worker_db_id = (
        get_current_worker_db_id()
    )

    if not worker_db_id:
        return None

    if not booking_value:
        return None

    conn = get_db()

    try:

        if str(booking_value).isdigit():

            booking = conn.execute(
                """
                SELECT
                    b.*,

                    c.fullname AS customer_name,
                    c.email AS customer_email,
                    c.mobile AS customer_mobile,

                    b.address AS customer_address,
                    b.city AS customer_booking_city,
                    b.state AS customer_booking_state,
                    b.pincode AS customer_booking_pincode,

                    w.fullname AS worker_name,
                    w.mobile AS worker_mobile

                FROM bookings b

                LEFT JOIN customers c
                    ON c.id = b.customer_id

                LEFT JOIN workers w
                    ON w.id = b.worker_id

                WHERE b.id = ?
                AND (
                    b.worker_id = ?
                    OR b.assigned_worker_id = ?
                    OR CAST(b.assigned_worker_id AS TEXT) = (
                        SELECT worker_id FROM workers WHERE id = ? LIMIT 1
                    )
                    OR EXISTS (SELECT 1 FROM booking_assignment_offers o WHERE o.booking_db_id=b.id AND o.worker_id=? AND o.status='offered')
                )

                LIMIT 1
                """,
                (
                    int(booking_value),
                    worker_db_id,
                    worker_db_id,
                    worker_db_id,
                    worker_db_id
                )
            ).fetchone()

            if booking:
                return booking

        booking = conn.execute(
            """
            SELECT
                b.*,

                c.fullname AS customer_name,
                c.mobile AS customer_mobile,

                b.address AS customer_address,
                b.city AS customer_booking_city,
                b.state AS customer_booking_state,
                b.pincode AS customer_booking_pincode,

                w.fullname AS worker_name,
                w.mobile AS worker_mobile

            FROM bookings b

            LEFT JOIN customers c
                ON c.id = b.customer_id

            LEFT JOIN workers w
                ON w.id = b.worker_id

            WHERE b.booking_id = ?
            AND (
                b.worker_id = ?
                OR b.assigned_worker_id = ?
                OR CAST(b.assigned_worker_id AS TEXT) = (
                    SELECT worker_id FROM workers WHERE id = ? LIMIT 1
                )
                OR EXISTS (SELECT 1 FROM booking_assignment_offers o WHERE o.booking_db_id=b.id AND o.worker_id=? AND o.status='offered')
            )

            LIMIT 1
            """,
            (
                booking_value,
                worker_db_id,
                worker_db_id,
                worker_db_id,
                worker_db_id
            )
        ).fetchone()

        return booking

    except Exception as error:

        print(
            "GET WORKER BOOKING ERROR:",
            error
        )

        return None

    finally:

        conn.close()


def create_notification(
    user_type,
    user_id,
    title,
    message
):

    conn = get_db()

    try:

        conn.execute(
            """
            INSERT INTO notifications
            (
                user_type,
                user_id,
                title,
                message
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                user_type,
                str(user_id),
                title,
                message
            )
        )

        conn.commit()

    except Exception as error:

        conn.rollback()

        print(
            "NOTIFICATION ERROR:",
            error
        )

    finally:

        conn.close()


def update_booking_status(
    booking_id,
    new_status,
    allowed_statuses=None
):

    worker_db_id = (
        get_current_worker_db_id()
    )

    if not worker_db_id:
        return (
            False,
            "Worker session expired."
        )

    conn = get_db()

    try:

        if allowed_statuses:

            placeholders = ",".join(
                ["?"] * len(
                    allowed_statuses
                )
            )

            query = f"""
                UPDATE bookings
                SET
                    booking_status = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                AND (
                    worker_id = ?
                    OR assigned_worker_id = ?
                    OR CAST(assigned_worker_id AS TEXT) = (
                        SELECT worker_id FROM workers WHERE id = ? LIMIT 1
                    )
                )
                AND LOWER(booking_status)
                    IN ({placeholders})
            """

            params = [
                new_status,
                booking_id,
                worker_db_id,
                worker_db_id,
                worker_db_id
            ]

            params.extend(
                [
                    status.lower()
                    for status in allowed_statuses
                ]
            )

            cursor = conn.execute(
                query,
                params
            )

        else:

            cursor = conn.execute(
                """
                UPDATE bookings
                SET
                    booking_status = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                AND worker_id = ?
                """,
                (
                    new_status,
                    booking_id,
                    worker_db_id
                )
            )

        if cursor.rowcount == 0:

            conn.rollback()

            return (
                False,
                "Booking cannot be updated."
            )

        conn.commit()

        return (
            True,
            "Booking status updated."
        )

    except Exception as error:

        conn.rollback()

        print(
            "UPDATE BOOKING STATUS ERROR:",
            error
        )

        return (
            False,
            "Booking could not be updated."
        )

    finally:

        conn.close()


# ============================================================
# WORKER DASHBOARD
# ============================================================

@worker_bp.route("/worker/home")
@worker_required
def worker_home():
    worker = get_current_worker()

    if not worker:
        session.clear()
        return redirect(url_for("auth.login", role="worker"))

    worker_db_id = worker["id"]
    worker_id = str(worker["worker_id"])

    conn = get_db()

    # Defaults, so one optional query failure does not hide other counts.
    total_jobs = 0
    pending_jobs = 0
    active_jobs = 0
    completed_jobs = 0
    recent_jobs = []
    available_balance = 0.0
    today_earning = 0.0
    notification_count = 0

    booking_filter = """
        (
            worker_id = ?
            OR assigned_worker_id = ?
            OR CAST(assigned_worker_id AS TEXT) = (
                SELECT worker_id
                FROM workers
                WHERE id = ?
                LIMIT 1
            )
        )
    """
    booking_params = (worker_db_id, worker_db_id, worker_db_id)

    try:
        try:
            total_jobs = conn.execute(
                f"""
                SELECT COUNT(*)
                FROM bookings
                WHERE {booking_filter}
                """,
                booking_params,
            ).fetchone()[0] or 0

            pending_jobs = conn.execute(
                f"""
                SELECT COUNT(*)
                FROM bookings
                WHERE {booking_filter}
                AND LOWER(COALESCE(booking_status, ''))
                    IN ('assigned', 'pending')
                """,
                booking_params,
            ).fetchone()[0] or 0

            active_jobs = conn.execute(
                f"""
                SELECT COUNT(*)
                FROM bookings
                WHERE {booking_filter}
                AND LOWER(COALESCE(booking_status, ''))
                    IN ('accepted', 'started', 'in_progress')
                """,
                booking_params,
            ).fetchone()[0] or 0

            completed_jobs = conn.execute(
                f"""
                SELECT COUNT(*)
                FROM bookings
                WHERE {booking_filter}
                AND LOWER(COALESCE(booking_status, '')) = 'completed'
                """,
                booking_params,
            ).fetchone()[0] or 0

            recent_jobs = conn.execute(
                f"""
                SELECT b.*, c.fullname AS customer_name
                FROM bookings b
                LEFT JOIN customers c ON c.id = b.customer_id
                WHERE (
                    b.worker_id = ?
                    OR b.assigned_worker_id = ?
                    OR CAST(b.assigned_worker_id AS TEXT) = (
                        SELECT worker_id
                        FROM workers
                        WHERE id = ?
                        LIMIT 1
                    )
                )
                ORDER BY b.id DESC
                LIMIT 5
                """,
                booking_params,
            ).fetchall()

        except Exception as error:
            print("WORKER DASHBOARD JOB COUNTS ERROR:", error)

        try:
            # Dashboard earnings must match the transaction types actually
            # written by the finance ledger (currently service_earning).
            # Do not count generic credits/top-ups as service earnings.
            earned = conn.execute(
                """
                SELECT COALESCE(SUM(amount), 0)
                FROM wallet_transactions
                WHERE user_type = 'worker'
                  AND user_id = ?
                  AND LOWER(COALESCE(transaction_type, ''))
                      IN ('service_earning', 'earning')
                """,
                (worker_id,),
            ).fetchone()[0] or 0

            # SQLite stores CURRENT_TIMESTAMP in UTC. Convert both sides to
            # the server's local date so the dashboard's "Today" is consistent
            # with the date shown by the running Sivdhi server.
            today_earning = conn.execute(
                """
                SELECT COALESCE(SUM(amount), 0)
                FROM wallet_transactions
                WHERE user_type = 'worker'
                  AND user_id = ?
                  AND LOWER(COALESCE(transaction_type, ''))
                      IN ('service_earning', 'earning')
                  AND date(created_at, 'localtime') = date('now', 'localtime')
                """,
                (worker_id,),
            ).fetchone()[0] or 0

            paid = conn.execute(
                """
                SELECT COALESCE(SUM(amount), 0)
                FROM wallet_transactions
                WHERE user_type = 'worker'
                  AND user_id = ?
                  AND transaction_type IN ('withdraw', 'debit')
                """,
                (worker_id,),
            ).fetchone()[0] or 0

            available_balance = max(
                float(earned) - float(paid),
                0.0,
            )

        except Exception as error:
            print("WORKER DASHBOARD WALLET ERROR:", error)

        try:
            # Older events have used both workers.id (internal DB ID)
            # and workers.worker_id (public ID). Read both representations
            # so valid notifications are not silently hidden.
            notification_count = conn.execute(
                """
                SELECT COUNT(*)
                FROM notifications
                WHERE user_type = 'worker'
                  AND user_id IN (?, ?)
                  AND COALESCE(is_read, 0) = 0
                """,
                (str(worker_db_id), worker_id),
            ).fetchone()[0] or 0

        except Exception as error:
            print("WORKER DASHBOARD NOTIFICATION COUNT ERROR:", error)

    finally:
        conn.close()

    return render_template(
        "worker/home.html",
        worker=worker,
        total_jobs=total_jobs,
        pending_jobs=pending_jobs,
        active_jobs=active_jobs,
        completed_jobs=completed_jobs,
        recent_jobs=recent_jobs,
        available_balance=available_balance,
        today_earning=today_earning,
        notification_count=notification_count,
    )


# ============================================================
# MY JOBS
# ============================================================

@worker_bp.route(
    "/worker/jobs"
)
@worker_required
def worker_jobs():

    worker = get_current_worker()

    if not worker:

        session.clear()

        return redirect(url_for("auth.login", role="worker"))

    conn = get_db()

    try:

        jobs = conn.execute(
            """
            SELECT
                b.*,

                c.fullname AS customer_name,

                c.address AS customer_saved_address,
                c.city AS customer_city,
                c.state AS customer_state,
                c.pincode AS customer_pincode,

                b.address AS customer_address,
                b.city AS customer_booking_city,
                b.state AS customer_booking_state,
                b.pincode AS customer_booking_pincode

            FROM bookings b

            LEFT JOIN customers c
                ON c.id = b.customer_id

            WHERE (
                b.worker_id = ?
                OR b.assigned_worker_id = ?
                OR CAST(b.assigned_worker_id AS TEXT) = (
                    SELECT worker_id FROM workers WHERE id = ? LIMIT 1
                )
                OR EXISTS (SELECT 1 FROM booking_assignment_offers o WHERE o.booking_db_id=b.id AND o.worker_id=? AND o.status='offered')
            )

            ORDER BY
                CASE
                    WHEN LOWER(b.booking_status)
                        IN ('assigned', 'pending')
                    THEN 1

                    WHEN LOWER(b.booking_status)
                        IN (
                            'accepted',
                            'started',
                            'in_progress'
                        )
                    THEN 2

                    WHEN LOWER(b.booking_status)
                        = 'completed'
                    THEN 3

                    ELSE 4
                END,

                b.id DESC
            """,
            (worker["id"], worker["id"], worker["id"], worker["id"])
        ).fetchall()

    except Exception as error:

        print(
            "WORKER JOBS ERROR:",
            error
        )

        jobs = []

    finally:

        conn.close()

    return render_template(
        "worker/jobs.html",
        worker=worker,
        jobs=jobs
    )


# ============================================================
# JOB DETAILS
# ============================================================

@worker_bp.route(
    "/worker/job/<job_id>"
)
@worker_required
def job_details(
    job_id
):

    worker = get_current_worker()

    if not worker:

        session.clear()

        return redirect(url_for("auth.login", role="worker"))

    job = get_booking_for_worker(
        job_id
    )

    if not job:

        flash(
            "Job not found.",
            "danger"
        )

        return redirect(
            url_for(
                "worker.worker_jobs"
            )
        )

    # Opening the job details page counts as the worker having seen the
    # assignment. The monitor still requires an accept/reject response before
    # its timeout, so simply viewing a booking does not stop escalation.
    seen_conn = get_db()
    try:
        ensure_finance_schema(seen_conn)
        current_worker_id = get_current_worker_db_id()
        if current_worker_id:
            seen_conn.execute("""
                UPDATE bookings
                SET worker_seen_at=COALESCE(worker_seen_at, CURRENT_TIMESTAMP),
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                  AND (worker_id=? OR assigned_worker_id=?)
                  AND LOWER(COALESCE(NULLIF(booking_status,''), NULLIF(status,''), 'pending'))
                      IN ('assigned','pending')
            """, (job["id"], current_worker_id, current_worker_id))
            seen_conn.commit()
    except Exception as error:
        seen_conn.rollback()
        print("WORKER BOOKING SEEN ERROR:", error)
    finally:
        seen_conn.close()

    return render_template(
        "worker/job_details.html",
        worker=worker,
        job=job,
        booking=job,
        job_id=job_id
    )


# ============================================================
# WORKER AVAILABILITY HEARTBEAT
# ============================================================

@worker_bp.route("/worker/ping", methods=["POST"])
@worker_required
def worker_ping():
    """Refresh online state while an authenticated worker app page is active."""
    worker_db_id = get_current_worker_db_id()
    if not worker_db_id:
        return jsonify({"success": False, "message": "Worker session expired."}), 401
    conn = get_db()
    try:
        conn.execute("""
            UPDATE workers
            SET last_seen_at=CURRENT_TIMESTAMP, is_online=1
            WHERE id=? AND COALESCE(is_deleted,0)=0
        """, (worker_db_id,))
        conn.commit()
        return jsonify({"success": True, "server_time": datetime.utcnow().isoformat(timespec="seconds")})
    except Exception as error:
        conn.rollback()
        print("WORKER HEARTBEAT ERROR:", error)
        return jsonify({"success": False, "message": "Availability could not be refreshed."}), 500
    finally:
        conn.close()


# ============================================================
# ACCEPT JOB
# ============================================================

@worker_bp.route(
    "/worker/job/<job_id>/accept",
    methods=["POST"]
)
@worker_required
def accept_job(
    job_id
):

    booking = get_booking_for_worker(
        job_id
    )

    if not booking:

        return jsonify({
            "success": False,
            "message": "Job not found."
        }), 404

    current_worker_id = get_current_worker_db_id()
    assigned_id = booking["worker_id"] or booking["assigned_worker_id"]
    try:
        is_current_assignee = int(assigned_id or 0) == int(current_worker_id or 0)
    except (TypeError, ValueError):
        is_current_assignee = False

    if not is_current_assignee:
        conn = get_db()
        try:
            ensure_assignment_monitor_schema(conn)
            conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute("SELECT * FROM bookings WHERE id=? LIMIT 1", (booking["id"],)).fetchone()
            offer = conn.execute("SELECT * FROM booking_assignment_offers WHERE booking_db_id=? AND worker_id=? AND status='offered' LIMIT 1", (booking["id"], current_worker_id)).fetchone()
            if not current or not offer:
                conn.rollback()
                return jsonify({"success": False, "message": "This request is no longer available."}), 409
            from utils.assignment_monitor import _booking_assignment_deadline, _now_ist, _parse_datetime
            now = _now_ist()
            global_deadline = _booking_assignment_deadline(current, now)
            offer_deadline = _parse_datetime(offer["response_deadline"])
            current_status = str(current["booking_status"] or current["status"] or "pending").lower()
            if current_status not in {"assigned", "pending"} or global_deadline is None or now >= global_deadline or offer_deadline is None or now >= offer_deadline:
                conn.execute("UPDATE booking_assignment_offers SET status='expired', responded_at=? WHERE id=? AND status='offered'", (now.isoformat(timespec="seconds"), offer["id"]))
                conn.commit()
                return jsonify({"success": False, "message": "The response time for this request has expired."}), 409
            partner_id = None
            try:
                from utils.sivdhi_finance import find_partner_for_worker
                partner_id = find_partner_for_worker(conn, int(current_worker_id))
            except Exception:
                partner_id = None
            cursor = conn.execute("""
                UPDATE bookings SET worker_id=?, assigned_worker_id=?, partner_id=?, assigned_partner_id=?,
                    booking_status='accepted', status='accepted', assigned_at=?, worker_seen_at=CURRENT_TIMESTAMP,
                    partner_acknowledged_at=NULL, updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND LOWER(COALESCE(NULLIF(booking_status,''),NULLIF(status,''),'pending')) IN ('assigned','pending')
                  AND (worker_id IS NULL OR assigned_worker_id IS NULL OR worker_id<>? OR assigned_worker_id<>?)
            """, (current_worker_id, current_worker_id, partner_id, partner_id, now.isoformat(timespec="seconds"), booking["id"], current_worker_id, current_worker_id))
            if cursor.rowcount != 1:
                conn.rollback()
                return jsonify({"success": False, "message": "Another worker has already accepted this booking."}), 409
            conn.execute("UPDATE booking_assignment_offers SET status='accepted', responded_at=? WHERE id=?", (now.isoformat(timespec="seconds"), offer["id"]))
            conn.execute("UPDATE booking_assignment_offers SET status='cancelled', responded_at=? WHERE booking_db_id=? AND worker_id<>? AND status='offered'", (now.isoformat(timespec="seconds"), booking["id"], current_worker_id))
            conn.commit()
            create_notification("worker", current_worker_id, "Booking Accepted", "You accepted booking " + str(current["booking_id"]) + ". Please follow the scheduled service time.")
            if partner_id:
                create_notification("partner", partner_id, "Booking Accepted by Worker", "Your worker accepted booking " + str(current["booking_id"]) + ".")
            return jsonify({"success": True, "message": "Job accepted successfully.", "status": "accepted"})
        except Exception as error:
            conn.rollback()
            current_app.logger.exception("LIVE OFFER ACCEPT ERROR: %s", error)
            return jsonify({"success": False, "message": "Job could not be accepted."}), 500
        finally:
            conn.close()

    if is_current_assignee:
        check_conn = get_db()
        try:
            from utils.assignment_monitor import _booking_assignment_deadline, _now_ist, _parse_datetime
            check_conn.execute("""CREATE TABLE IF NOT EXISTS booking_assignment_offers (
                id INTEGER PRIMARY KEY AUTOINCREMENT, booking_db_id INTEGER NOT NULL, worker_id INTEGER NOT NULL,
                partner_id TEXT, status TEXT NOT NULL DEFAULT 'offered', offered_at TEXT NOT NULL,
                response_deadline TEXT NOT NULL, responded_at TEXT, UNIQUE(booking_db_id,worker_id))""")
            check_conn.commit()
            current = check_conn.execute("SELECT * FROM bookings WHERE id=? LIMIT 1", (booking["id"],)).fetchone()
            offer = check_conn.execute("SELECT * FROM booking_assignment_offers WHERE booking_db_id=? AND worker_id=? LIMIT 1", (booking["id"], current_worker_id)).fetchone()
            now = _now_ist()
            deadline = _booking_assignment_deadline(current, now) if current else None
            offer_deadline = _parse_datetime(offer["response_deadline"]) if offer else None
            if deadline is not None and now >= deadline:
                return jsonify({"success": False, "message": "The assignment response window has expired."}), 409
            if offer and (offer["status"] != "offered" or offer_deadline is None or now >= offer_deadline):
                return jsonify({"success": False, "message": "Your allocated response time has expired."}), 409
        finally:
            check_conn.close()

    status = (
        booking["booking_status"]
        or "pending"
    ).lower()

    if status not in (
        "assigned",
        "pending"
    ):

        return jsonify({
            "success": False,
            "message":
                "This job cannot be accepted."
        }), 400

    # The database migration uses workers.id as the canonical internal key.
    # Re-sync both assignment columns before changing the status so old
    # bookings created by earlier versions continue to work.
    conn = get_db()
    try:
        normalize_worker_assignment(conn, booking["id"], get_current_worker_db_id())
        conn.commit()
    except Exception as error:
        conn.rollback()
        print("ACCEPT ASSIGNMENT SYNC ERROR:", error)
        return jsonify({
            "success": False,
            "message": "Job could not be accepted."
        }), 500
    finally:
        conn.close()

    success, message = (
        update_booking_status(
            booking["id"],
            "accepted",
            [
                "assigned",
                "pending"
            ]
        )
    )

    if not success:
        return jsonify({"success": False, "message": message}), 400

    seen_conn = get_db()
    try:
        ensure_finance_schema(seen_conn)
        from utils.assignment_monitor import ensure_assignment_monitor_schema, _now_ist
        ensure_assignment_monitor_schema(seen_conn)
        now_text = _now_ist().isoformat(timespec="seconds")
        seen_conn.execute("""
            UPDATE bookings
            SET worker_seen_at = COALESCE(worker_seen_at, CURRENT_TIMESTAMP),
                status = 'accepted', updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND worker_id = ?
        """, (booking["id"], get_current_worker_db_id()))
        seen_conn.execute("UPDATE booking_assignment_offers SET status='accepted', responded_at=? WHERE booking_db_id=? AND worker_id=? AND status='offered'", (now_text, booking["id"], get_current_worker_db_id()))
        seen_conn.execute("UPDATE booking_assignment_offers SET status='cancelled', responded_at=? WHERE booking_db_id=? AND worker_id<>? AND status='offered'", (now_text, booking["id"], get_current_worker_db_id()))
        seen_conn.commit()
    except Exception as error:
        seen_conn.rollback()
        print("WORKER SEEN TIMESTAMP ERROR:", error)
    finally:
        seen_conn.close()

    try:
        conn = get_db()
        try:
            current = conn.execute("SELECT partner_id, assigned_partner_id FROM bookings WHERE id=? LIMIT 1", (booking["id"],)).fetchone()
            partner_id = (current["assigned_partner_id"] or current["partner_id"]) if current else None
        finally:
            conn.close()
        if partner_id:
            create_notification("partner", partner_id, "Booking Accepted by Worker", "Your worker accepted booking " + str(booking["booking_id"]) + ".")
    except Exception:
        current_app.logger.exception("Could not notify partner about worker acceptance")

    return jsonify({
        "success": True,
        "message":
            "Job accepted successfully.",
        "status": "accepted"
    })


# ============================================================
# REJECT JOB
# ============================================================

@worker_bp.route(
    "/worker/job/<job_id>/reject",
    methods=["POST"]
)
@worker_required
def reject_job(job_id):
    """Record a rejection and atomically assign the next eligible worker."""
    visible_booking = get_booking_for_worker(job_id)
    if not visible_booking:
        return jsonify({"success": False, "message": "Job not found."}), 404

    data = request.get_json(silent=True)
    if data is None:
        data = request.form.to_dict()
    reason = str(data.get("reason", "Worker rejected the job.")).strip() or "Worker rejected the job."
    reason = reason[:500]

    worker_db_id = get_current_worker_db_id()
    if not worker_db_id:
        return jsonify({"success": False, "message": "Worker session expired."}), 401

    conn = get_db()
    try:
        ensure_assignment_monitor_schema(conn)
        conn.commit()  # keep additive schema changes outside the assignment transaction
        conn.execute("BEGIN IMMEDIATE")
        offered = conn.execute("SELECT * FROM booking_assignment_offers WHERE booking_db_id=? AND worker_id=? AND status='offered' LIMIT 1", (visible_booking["id"], worker_db_id)).fetchone()
        if offered:
            current_assignment = conn.execute("SELECT worker_id,assigned_worker_id FROM bookings WHERE id=? LIMIT 1", (visible_booking["id"],)).fetchone()
            try:
                assigned_now = int((current_assignment["worker_id"] or current_assignment["assigned_worker_id"]) or 0)
            except (TypeError, ValueError):
                assigned_now = 0
            if assigned_now != int(worker_db_id):
                from utils.assignment_monitor import _now_ist
                conn.execute("UPDATE booking_assignment_offers SET status='rejected', responded_at=? WHERE id=? AND status='offered'", (_now_ist().isoformat(timespec="seconds"), offered["id"]))
                conn.commit()
                create_notification("worker", worker_db_id, "Booking Request Declined", "You declined booking " + str(visible_booking["booking_id"]) + ". The customer will not be notified of this internal decision.")
                partner_id = offered["partner_id"]
                if partner_id:
                    create_notification("partner", partner_id, "Worker Declined Request", "A worker declined booking " + str(visible_booking["booking_id"]) + ". Sivdhi will continue matching until the original deadline.")
                return jsonify({"success": True, "message": "Request declined. The booking remains available to other eligible workers.", "reassigned": False})
        booking = conn.execute(
            "SELECT * FROM bookings WHERE id=? LIMIT 1", (visible_booking["id"],)
        ).fetchone()
        if not booking:
            conn.rollback()
            return jsonify({"success": False, "message": "Job not found."}), 404

        try:
            assigned_worker_id = int(booking["worker_id"] or booking["assigned_worker_id"] or 0)
        except (TypeError, ValueError):
            assigned_worker_id = 0
        if assigned_worker_id != int(worker_db_id):
            conn.rollback()
            return jsonify({"success": False, "message": "This booking is no longer assigned to you."}), 409

        status = str(booking["booking_status"] or booking["status"] or "pending").lower()
        if status not in {"assigned", "pending"}:
            conn.rollback()
            return jsonify({"success": False, "message": "This job can no longer be rejected."}), 400

        from utils.assignment_monitor import _now_ist
        conn.execute("UPDATE booking_assignment_offers SET status='rejected', responded_at=? WHERE booking_db_id=? AND worker_id=? AND status='offered'", (_now_ist().isoformat(timespec="seconds"), booking["id"], worker_db_id))
        result = reassign_booking(
            conn,
            booking,
            int(worker_db_id),
            reason=reason,
            attempt_status="rejected",
        )
        conn.commit()

        if result["reassigned"]:
            candidate = result["worker"]
            return jsonify({
                "success": True,
                "message": "Job rejected and reassigned to the next eligible worker.",
                "reassigned": True,
                "worker_distance_km": candidate.get("distance_km"),
            })

        return jsonify({
            "success": True,
            "message": "Job rejected. No other eligible worker is available right now; the booking is waiting for another worker.",
            "reassigned": False,
        })
    except Exception as error:
        conn.rollback()
        print("REJECT JOB ERROR:", error)
        return jsonify({"success": False, "message": "Job could not be rejected."}), 500
    finally:
        conn.close()


# ============================================================
# START JOB
# ============================================================

@worker_bp.route("/worker/job/<job_id>/arrived", methods=["POST"])
@worker_required
def worker_arrived(job_id):
    """Record physical arrival only. The customer pays the balance separately."""
    booking = get_booking_for_worker(job_id)
    if not booking:
        return jsonify({"success": False, "message": "Job not found."}), 404

    worker_id = get_current_worker_db_id()
    conn = get_db()
    newly_arrived = False
    customer_id = None
    booking_ref = None
    balance_due = 0.0
    try:
        ensure_finance_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT * FROM bookings
            WHERE id=? AND (worker_id=? OR assigned_worker_id=?)
            LIMIT 1
            """,
            (booking["id"], worker_id, worker_id),
        ).fetchone()
        if not row:
            conn.rollback()
            return jsonify({"success": False, "message": "Booking not assigned to this worker."}), 404

        status = str(row["booking_status"] or row["status"] or "").lower()
        if status != "accepted":
            conn.rollback()
            return jsonify({"success": False, "message": "Accept the booking before marking arrival."}), 400

        total = finance_money(
            row["total_amount"] if row["total_amount"] is not None else row["amount"]
        )
        advance_paid = finance_money(row["advance_paid_amount"])
        balance_paid = finance_money(row["balance_paid_amount"])
        balance_status = str(row["balance_payment_status"] or "DUE").upper()
        paid_states = {"PAID", "SUCCESS", "DEMO_PAID", "COLLECTED", "COMPLETED"}
        if balance_status in paid_states:
            balance_due = 0.0
        else:
            saved_due = finance_money(row["balance_due_amount"])
            balance_due = saved_due if saved_due > 0 else finance_money(
                max(0.0, total - advance_paid - balance_paid)
            )

        already_arrived = bool(row["worker_arrived_at"])
        cursor = conn.execute(
            """
            UPDATE bookings
            SET worker_arrived_at=COALESCE(worker_arrived_at, CURRENT_TIMESTAMP),
                updated_at=CURRENT_TIMESTAMP
            WHERE id=? AND (worker_id=? OR assigned_worker_id=?)
            """,
            (row["id"], worker_id, worker_id),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            return jsonify({"success": False, "message": "Arrival could not be recorded."}), 409

        newly_arrived = not already_arrived
        customer_id = row["customer_id"]
        booking_ref = row["booking_id"]

        if newly_arrived and customer_id:
            message = (
                f"Your worker has arrived for booking {booking_ref}. "
                + (
                    f"Remaining balance ₹{balance_due:.2f} is now payable from My Bookings. "
                    "The worker-arrival action has NOT charged or marked this balance as paid."
                    if balance_due > 0
                    else "No remaining balance is due."
                )
            )
            conn.execute(
                """
                INSERT INTO notifications
                    (user_type, user_id, title, message, is_read, created_at)
                VALUES ('customer', ?, 'Worker Arrived', ?, 0, CURRENT_TIMESTAMP)
                """,
                (str(customer_id), message),
            )
            conn.execute(
                "INSERT INTO notifications(user_type,user_id,title,message,is_read,created_at) VALUES('worker',?,'Arrival Recorded',?,0,CURRENT_TIMESTAMP)",
                (str(worker_id), f"Your arrival for booking {booking_ref} has been recorded."),
            )
            partner_id = row["assigned_partner_id"] or row["partner_id"]
            if not partner_id:
                from utils.sivdhi_finance import find_partner_for_worker
                partner_id = find_partner_for_worker(conn, int(worker_id))
            if partner_id:
                conn.execute(
                    "INSERT INTO notifications(user_type,user_id,title,message,is_read,created_at) VALUES('partner',?,'Worker Arrived',?,0,CURRENT_TIMESTAMP)",
                    (str(partner_id), f"Worker has arrived for booking {booking_ref}."),
                )

        conn.commit()

    except Exception:
        conn.rollback()
        current_app.logger.exception("WORKER ARRIVAL ERROR")
        return jsonify({"success": False, "message": "Arrival could not be recorded."}), 500
    finally:
        conn.close()

    return jsonify({
        "success": True,
        "message": (
            f"Arrival recorded. Customer has been notified to pay the remaining ₹{balance_due:.2f} from My Bookings. "
            "No balance payment was recorded by the arrival action."
            if balance_due > 0
            else "Arrival recorded. No remaining balance is due."
        ),
        "status": "accepted",
        "arrived": True,
        "balance_due": balance_due,
        "balance_payment_required": balance_due > 0,
    })


@worker_bp.route("/worker/job/<job_id>/start", methods=["POST"])
@worker_required
def start_job(job_id):
    booking = get_booking_for_worker(job_id)
    if not booking:
        return jsonify({"success": False, "message": "Job not found."}), 404

    conn = get_db()
    try:
        ensure_finance_schema(conn)
        current = conn.execute("SELECT * FROM bookings WHERE id=? LIMIT 1", (booking["id"],)).fetchone()
        if not current:
            return jsonify({"success": False, "message": "Booking not found."}), 404

        # A worker may start the service only at or after the customer's
        # scheduled date and time. Booking columns are kept compatible with
        # both the preferred_* fields and the older booking_* fields.
        scheduled_date = current["preferred_date"] or current["booking_date"]
        scheduled_time = current["preferred_time"] or current["booking_time"]
        if scheduled_date and scheduled_time:
            try:
                from utils.booking_schedule import india_now
                now = india_now()
                scheduled_at = datetime.fromisoformat(
                    str(scheduled_date).strip()[:10] + "T" + str(scheduled_time).strip()[:5]
                )
                if scheduled_at.tzinfo is None and now.tzinfo is not None:
                    scheduled_at = scheduled_at.replace(tzinfo=now.tzinfo)
                elif scheduled_at.tzinfo is not None and now.tzinfo is None:
                    now = now.replace(tzinfo=scheduled_at.tzinfo)
                if now < scheduled_at:
                    display_date = str(scheduled_date).strip()[:10]
                    display_time = str(scheduled_time).strip()[:5]
                    return jsonify({
                        "success": False,
                        "message": f"You can start this service only on or after the customer's booked time ({display_date} at {display_time})."
                    }), 400
            except (TypeError, ValueError):
                current_app.logger.exception("Invalid booking date/time for booking %s", booking["id"])
                return jsonify({
                    "success": False,
                    "message": "The customer's booking date/time is invalid. Please contact support before starting this service."
                }), 400

        if current and finance_money(current["advance_amount"]) > 0:
            if not current["worker_arrived_at"]:
                return jsonify({"success": False, "message": "Mark arrival and record the remaining balance first."}), 400
            if str(current["balance_payment_status"] or "").upper() not in {"PAID", "SUCCESS", "DEMO_PAID", "COLLECTED", "COMPLETED"}:
                return jsonify({"success": False, "message": "The customer must pay/record the remaining balance from My Bookings after arrival before the service can start."}), 400
    finally:
        conn.close()

    success, message = update_booking_status(booking["id"], "in_progress", ["accepted"])
    if not success:
        return jsonify({"success": False, "message": message}), 400

    conn = get_db()
    try:
        conn.execute("UPDATE bookings SET status='in_progress', updated_at=CURRENT_TIMESTAMP WHERE id=?", (booking["id"],))
        conn.commit()
    finally:
        conn.close()

    create_notification("customer", booking["customer_id"], "Booking Started",
                        "Work has started for your booking " + str(booking["booking_id"]) + ".")
    create_notification("worker", get_current_worker_db_id(), "Service Start Recorded",
                        "You started service for booking " + str(booking["booking_id"]) + ".")
    try:
        conn = get_db()
        try:
            row = conn.execute("SELECT partner_id, assigned_partner_id FROM bookings WHERE id=? LIMIT 1", (booking["id"],)).fetchone()
            partner_id = (row["assigned_partner_id"] or row["partner_id"]) if row else None
            if not partner_id:
                from utils.sivdhi_finance import find_partner_for_worker
                partner_id = find_partner_for_worker(conn, int(get_current_worker_db_id()))
        finally:
            conn.close()
        if partner_id:
            create_notification("partner", partner_id, "Booking Started", "Your worker started booking " + str(booking["booking_id"]) + ".")
    except Exception:
        current_app.logger.exception("Could not notify partner about service start")
    return jsonify({"success": True, "message": "Service started successfully.", "status": "in_progress"})


# ============================================================
# COMPLETE JOB
# ============================================================

@worker_bp.route("/worker/job/<job_id>/complete", methods=["POST"])
@worker_required
def complete_job(job_id):
    booking = get_booking_for_worker(job_id)
    if not booking:
        return jsonify({"success": False, "message": "Job not found."}), 404

    conn = get_db()
    try:
        ensure_finance_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute("SELECT * FROM bookings WHERE id=? LIMIT 1", (booking["id"],)).fetchone()
        if not current:
            conn.rollback()
            return jsonify({"success": False, "message": "Booking not found."}), 404
        status = str(current["booking_status"] or current["status"] or "").lower()
        if status not in {"in_progress", "started"}:
            conn.rollback()
            return jsonify({"success": False, "message": "Job must be in progress before completion."}), 400
        if finance_money(current["advance_amount"]) > 0:
            if not current["worker_arrived_at"]:
                conn.rollback()
                return jsonify({"success": False, "message": "Arrival has not been recorded."}), 400
            paid_states = {"PAID", "SUCCESS", "DEMO_PAID", "COLLECTED", "COMPLETED"}
            if str(current["advance_payment_status"] or "").upper() not in paid_states or str(current["balance_payment_status"] or "").upper() not in paid_states:
                conn.rollback()
                return jsonify({"success": False, "message": "Advance and remaining balance must both be recorded before completion."}), 400

        conn.execute("UPDATE bookings SET booking_status='completed', status='completed', updated_at=CURRENT_TIMESTAMP WHERE id=?", (booking["id"],))
        ok, message = settle_completed_booking(conn, booking["id"])
        if not ok:
            conn.rollback()
            return jsonify({"success": False, "message": message}), 400
        conn.commit()
    except Exception as error:
        conn.rollback()
        print("COMPLETE JOB/SETTLEMENT ERROR:", error)
        return jsonify({"success": False, "message": "Job completion and commission settlement failed; please retry."}), 500
    finally:
        conn.close()

    create_notification("customer", booking["customer_id"], "Booking Completed",
                        "Your service booking " + str(booking["booking_id"]) + " has been completed.")
    create_notification("worker", get_current_worker_db_id(), "Booking Completed",
                        "Booking " + str(booking["booking_id"]) + " is complete and settlement has been recorded.")
    try:
        conn = get_db()
        try:
            row = conn.execute("SELECT partner_id, assigned_partner_id FROM bookings WHERE id=? LIMIT 1", (booking["id"],)).fetchone()
            partner_id = (row["assigned_partner_id"] or row["partner_id"]) if row else None
            if not partner_id:
                from utils.sivdhi_finance import find_partner_for_worker
                partner_id = find_partner_for_worker(conn, int(get_current_worker_db_id()))
        finally:
            conn.close()
        if partner_id:
            create_notification("partner", partner_id, "Booking Completed", "Booking " + str(booking["booking_id"]) + " is complete; worker/partner/platform settlement has been processed.")
    except Exception:
        current_app.logger.exception("Could not notify partner about booking completion")
    return jsonify({"success": True, "message": "Service completed and Worker/Partner/Sivdhi shares recorded.", "status": "completed"})


@worker_bp.route("/worker/wallet")
@worker_required
def wallet():
    """Worker wallet and earnings summary without withdrawal controls."""
    worker = get_current_worker()

    if not worker:
        return redirect(url_for("auth.login", role="worker"))

    worker_db_id = str(worker["id"])
    worker_public_id = str(worker["worker_id"])
    worker_ids = tuple(dict.fromkeys((worker_db_id, worker_public_id)))

    now = datetime.now()
    today_label = now.strftime("%d %B %Y")
    month_label = now.strftime("%B %Y")
    today_key = now.strftime("%Y-%m-%d")
    month_key = now.strftime("%Y-%m")

    earning_types = ("service_earning", "earning", "credit")
    type_placeholders = ",".join("?" for _ in earning_types)
    user_placeholders = ",".join("?" for _ in worker_ids)

    today_earning = 0.0
    monthly_earning = 0.0
    total_earned = 0.0
    paid_amount = 0.0
    pending_payment = 0.0
    transactions = []

    conn = get_db()

    try:
        earning_filter = f"""
            FROM wallet_transactions
            WHERE user_type = 'worker'
              AND CAST(user_id AS TEXT) IN ({user_placeholders})
              AND LOWER(COALESCE(transaction_type, ''))
                  IN ({type_placeholders})
        """

        common_params = (*worker_ids, *earning_types)

        row = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) "
            + earning_filter
            + " AND date(created_at, 'localtime') = ?",
            (*common_params, today_key),
        ).fetchone()

        today_earning = float((row[0] if row else 0) or 0)

        row = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) "
            + earning_filter
            + " AND strftime('%Y-%m', created_at, 'localtime') = ?",
            (*common_params, month_key),
        ).fetchone()

        monthly_earning = float((row[0] if row else 0) or 0)

        row = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) " + earning_filter,
            common_params,
        ).fetchone()

        total_earned = float((row[0] if row else 0) or 0)

        # Retained for compatibility with any existing code using these values.
        row = conn.execute(
            f"""
            SELECT COALESCE(SUM(amount), 0)
            FROM wallet_transactions
            WHERE user_type = 'worker'
              AND CAST(user_id AS TEXT) IN ({user_placeholders})
              AND LOWER(COALESCE(transaction_type, ''))
                  IN ('withdraw', 'debit')
            """,
            worker_ids,
        ).fetchone()

        paid_amount = float((row[0] if row else 0) or 0)
        pending_payment = max(total_earned - paid_amount, 0.0)

        transactions = conn.execute(
            f"""
            SELECT *
            FROM wallet_transactions
            WHERE user_type = 'worker'
              AND CAST(user_id AS TEXT) IN ({user_placeholders})
              AND LOWER(COALESCE(transaction_type, ''))
                  IN ({type_placeholders})
            ORDER BY id DESC
            LIMIT 50
            """,
            common_params,
        ).fetchall()

    except Exception:
        current_app.logger.exception(
            "WORKER WALLET / EARNINGS ERROR"
        )

    finally:
        conn.close()

    return render_template(
        "worker/wallet.html",
        worker=worker,
        today_earning=today_earning,
        monthly_earning=monthly_earning,
        total_earned=total_earned,
        paid_amount=paid_amount,
        pending_payment=pending_payment,
        today_label=today_label,
        month_label=month_label,
        transactions=transactions,
    )


# ============================================================
# NOTIFICATIONS
# ============================================================

@worker_bp.route(
    "/worker/notifications",
    methods=["GET", "POST"]
)
@worker_required
def notifications():

    worker = get_current_worker()

    if not worker:

        return redirect(url_for("auth.login", role="worker"))

    conn = get_db()

    try:

        if request.method == "POST":

            app_notification = (
                1
                if request.form.get(
                    "app_notification"
                )
                else 0
            )

            email_notification = (
                1
                if request.form.get(
                    "email_notification"
                )
                else 0
            )

            job_notification = (
                1
                if request.form.get(
                    "job_notification"
                )
                else 0
            )

            payment_notification = (
                1
                if request.form.get(
                    "payment_notification"
                )
                else 0
            )

            promo_notification = (
                1
                if request.form.get(
                    "promo_notification"
                )
                else 0
            )

            conn.execute(
                """
                UPDATE workers
                SET
                    app_notification = ?,
                    email_notification = ?,
                    job_notification = ?,
                    payment_notification = ?,
                    promo_notification = ?
                WHERE worker_id = ?
                """,
                (
                    app_notification,
                    email_notification,
                    job_notification,
                    payment_notification,
                    promo_notification,
                    session["user_id"]
                )
            )

            conn.commit()

            flash(
                "Notification Settings Updated Successfully.",
                "success"
            )

        worker = conn.execute(
            """
            SELECT *
            FROM workers
            WHERE worker_id = ?
            """,
            (
                session["user_id"],
            )
        ).fetchone()

    except Exception as error:

        conn.rollback()

        print(
            "NOTIFICATION SETTINGS ERROR:",
            error
        )

    finally:

        conn.close()

    return render_template(
        "worker/notifications.html",
        worker=worker
    )
# ============================================================
# WORKER NOTIFICATION CENTER
# Keep the existing /worker/notifications settings route.
# ============================================================

@worker_bp.route("/worker/notification-center", methods=["GET"])
@worker_required
def worker_notification_center():
    worker = get_current_worker()

    if not worker:
        return redirect(url_for("auth.login", role="worker"))

    # Notification producers in existing code have used both the internal
    # workers.id and the public workers.worker_id. Query both representations
    # for compatibility, while keeping the user_type filter to protect other
    # roles' notification records.
    worker_db_id = str(worker["id"])
    worker_public_id = str(worker["worker_id"])
    notification_ids = (worker_db_id, worker_public_id)

    conn = get_db()
    notifications = []
    unread_count = 0

    try:
        unread_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM notifications
            WHERE user_type = 'worker'
              AND user_id IN (?, ?)
              AND COALESCE(is_read, 0) = 0
            """,
            notification_ids,
        ).fetchone()[0] or 0

        notifications = conn.execute(
            """
            SELECT id, title, message, is_read, created_at
            FROM notifications
            WHERE user_type = 'worker'
              AND user_id IN (?, ?)
            ORDER BY id DESC
            LIMIT 100
            """,
            notification_ids,
        ).fetchall()

        # Opening the notification center marks only this worker's records
        # (in either legacy ID format) as read. Other users remain untouched.
        conn.execute(
            """
            UPDATE notifications
            SET is_read = 1
            WHERE user_type = 'worker'
              AND user_id IN (?, ?)
              AND COALESCE(is_read, 0) = 0
            """,
            notification_ids,
        )
        conn.commit()

    except Exception as error:
        conn.rollback()
        print("WORKER NOTIFICATION CENTER ERROR:", error)
        flash("Notifications could not be loaded. Please try again.", "danger")

    finally:
        conn.close()

    return render_template(
        "worker/notification.html",
        worker=worker,
        notifications=notifications,
        unread_count=unread_count,
    )



@worker_bp.route(
    "/worker/profile"
)
@worker_required
def profile():
    worker = get_current_worker()

    if not worker:
        return redirect(url_for("auth.login", role="worker"))

    today_earning = 0.0
    conn = get_db()

    try:
        # Support both the internal database ID and the public worker ID.
        worker_ids = tuple(
            dict.fromkeys((
                str(worker["id"]),
                str(worker["worker_id"]),
            ))
        )

        placeholders = ",".join("?" for _ in worker_ids)

        row = conn.execute(
            f"""
            SELECT COALESCE(SUM(amount), 0)
            FROM wallet_transactions
            WHERE user_type = 'worker'
              AND CAST(user_id AS TEXT) IN ({placeholders})
              AND LOWER(COALESCE(transaction_type, ''))
                  IN ('service_earning', 'earning')
              AND date(created_at, 'localtime') =
                  date('now', 'localtime')
            """,
            worker_ids,
        ).fetchone()

        today_earning = float(
            (row[0] if row else 0) or 0
        )

    except Exception:
        current_app.logger.exception(
            "WORKER PROFILE TODAY EARNINGS ERROR"
        )

    finally:
        conn.close()

    return render_template(
        "worker/profile.html",
        worker=worker,
        today_earning=today_earning,
    )

# ============================================================
# EDIT PROFILE
# ============================================================

@worker_bp.route(
    "/worker/edit-profile",
    methods=["GET", "POST"]
)
@worker_required
def edit_profile():

    worker = get_current_worker()

    if not worker:

        return redirect(url_for("auth.login", role="worker"))

    if request.method == "POST":

        fullname = request.form.get(
            "fullname",
            ""
        ).strip()

        address = request.form.get(
            "address",
            ""
        ).strip()

        city = request.form.get(
            "city",
            ""
        ).strip()

        state = request.form.get(
            "state",
            ""
        ).strip()

        pincode = request.form.get(
            "pincode",
            ""
        ).strip()

        selected_skills = [
            str(value).strip()
            for value in request.form.getlist("skills")
            if str(value).strip()
        ]
        if not selected_skills:
            fallback_skill = request.form.get("skills", "").strip()
            selected_skills = [fallback_skill] if fallback_skill else []

        # Worker can select only categories currently managed/enabled by Admin.
        work_type = (
            request.form.get("work_type", "both")
            .strip().lower().replace("_", "-")
        )
        work_type_aliases = {
            "part-time": "part-time",
            "parttime": "part-time",
            "normal": "part-time",
            "full-day": "full-day",
            "fullday": "full-day",
            "full day": "full-day",
            "both": "both",
        }
        work_type = work_type_aliases.get(work_type, "")
        if not work_type:
            flash("Please choose Part-Time, Full-Day, or Both work types.", "danger")
            return redirect("/worker/edit-profile")

        experience = request.form.get(
            "experience",
            ""
        ).strip()

        service_radius_raw = request.form.get(
            "service_radius_km",
            "10"
        ).strip()

        try:
            service_radius_km = float(service_radius_raw)
        except (TypeError, ValueError):
            service_radius_km = 10.0

        # ----------------------------------------------------
        # BASIC PROFILE VALIDATION
        # ----------------------------------------------------

        if not fullname:

            flash(
                "Full name is required.",
                "danger"
            )

            return redirect(
                "/worker/edit-profile"
            )

        if len(fullname) > 100:

            flash(
                "Full name is too long.",
                "danger"
            )

            return redirect(
                "/worker/edit-profile"
            )

        if pincode and (
            not pincode.isdigit()
            or len(pincode) != 6
        ):

            flash(
                "Please enter a valid 6-digit pincode.",
                "danger"
            )

            return redirect(
                "/worker/edit-profile"
            )

        if service_radius_km < 1 or service_radius_km > 100:
            flash(
                "Work distance must be between 1 and 100 km.",
                "danger"
            )
            return redirect("/worker/edit-profile")

        # ----------------------------------------------------
        # LOCATION VALIDATION
        # ----------------------------------------------------

        latitude = request.form.get(
            "latitude"
        )

        longitude = request.form.get(
            "longitude"
        )

        try:

            latitude = (
                float(latitude)
                if latitude
                else None
            )

            if (
                latitude is not None
                and (
                    latitude < -90
                    or latitude > 90
                )
            ):

                latitude = None

        except (
            ValueError,
            TypeError
        ):

            latitude = None

        try:

            longitude = (
                float(longitude)
                if longitude
                else None
            )

            if (
                longitude is not None
                and (
                    longitude < -180
                    or longitude > 180
                )
            ):

                longitude = None

        except (
            ValueError,
            TypeError
        ):

            longitude = None

        # ----------------------------------------------------
        # PHOTO
        # ----------------------------------------------------

        photo = request.files.get(
            "profile_photo"
        )

        photo_name = worker[
            "profile_photo"
        ]

        if (
            photo
            and photo.filename
        ):

            valid_photo, photo_error = validate_uploaded_image(photo)
            if not valid_photo:
                flash(photo_error, "danger")
                return redirect("/worker/edit-profile")

            os.makedirs(
                UPLOAD_FOLDER,
                exist_ok=True
            )

            original_filename = secure_filename(
                photo.filename
            )

            if not original_filename:

                flash(
                    "Invalid image filename.",
                    "danger"
                )

                return redirect(
                    "/worker/edit-profile"
                )

            extension = (
                original_filename
                .rsplit(".", 1)[1]
                .lower()
            )

            filename = (
                "worker_"
                + str(worker["id"])
                + "_"
                + secrets.token_hex(8)
                + "."
                + extension
            )

            photo_path = os.path.join(
                UPLOAD_FOLDER,
                filename
            )

            try:

                photo.save(
                    photo_path
                )

                photo_name = filename

            except Exception as error:

                print(
                    "PROFILE PHOTO SAVE ERROR:",
                    error
                )

                flash(
                    "Profile photo could not be uploaded.",
                    "danger"
                )

                return redirect(
                    "/worker/edit-profile"
                )

        conn = get_db()

        try:
            worker_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(workers)").fetchall()
            }
            if "work_type" not in worker_columns:
                conn.execute("ALTER TABLE workers ADD COLUMN work_type TEXT DEFAULT 'both'")
                worker_columns.add("work_type")

            valid_rows = conn.execute("""
                SELECT name FROM service_categories
                WHERE COALESCE(is_active, 1)=1
                  AND (COALESCE(normal_enabled, 1)=1 OR COALESCE(full_day_enabled, 0)=1)
            """).fetchall()
            allowed_categories = {str(row["name"]).strip().casefold(): str(row["name"]).strip()
                                  for row in valid_rows if str(row["name"] or "").strip()}
            invalid_skills = [item for item in selected_skills if item.casefold() not in allowed_categories]
            if not selected_skills or invalid_skills:
                flash("Select at least one category that is currently enabled by Sivdhi Admin.", "danger")
                return redirect("/worker/edit-profile")
            skills = ", ".join(dict.fromkeys(allowed_categories[item.casefold()] for item in selected_skills))

            conn.execute(
                """
                UPDATE workers
                SET
                    fullname = ?,
                    address = ?,
                    city = ?,
                    state = ?,
                    pincode = ?,
                    skills = ?,
                    experience = ?,
                    profile_photo = ?,
                    latitude = ?,
                    longitude = ?,
                    service_radius_km = ?,
                    work_type = ?
                WHERE worker_id = ?
                """,
                (
                    fullname,
                    address,
                    city,
                    state,
                    pincode,
                    skills,
                    experience,
                    photo_name,
                    latitude,
                    longitude,
                    service_radius_km,
                    work_type,
                    session["user_id"]
                )
            )

            conn.commit()

        except Exception as error:

            conn.rollback()

            print(
                "EDIT PROFILE ERROR:",
                error
            )

            flash(
                "Profile could not be updated.",
                "danger"
            )

            return redirect(
                "/worker/edit-profile"
            )

        finally:

            conn.close()

        flash(
            "Profile Updated Successfully.",
            "success"
        )

        return redirect(
            "/worker/edit-profile"
        )

    conn = get_db()
    try:
        skill_categories = conn.execute("""
            SELECT id, name, normal_enabled, full_day_enabled
            FROM service_categories
            WHERE COALESCE(is_active, 1)=1
              AND (COALESCE(normal_enabled, 1)=1 OR COALESCE(full_day_enabled, 0)=1)
            ORDER BY name COLLATE NOCASE ASC
        """).fetchall()
    except Exception:
        skill_categories = []
    finally:
        conn.close()

    return render_template(
        "worker/edit_profile.html",
        worker=worker,
        skill_categories=skill_categories
    )


# ============================================================
# UPDATE LIVE LOCATION
# ============================================================

@worker_bp.route(
    "/worker/update-location",
    methods=["POST"]
)
@worker_required
def update_worker_location():

    try:

        data = request.get_json(
            silent=True
        ) or {}

        latitude = data.get(
            "latitude"
        )

        longitude = data.get(
            "longitude"
        )

        if (
            latitude is None
            or longitude is None
        ):

            return jsonify({
                "success": False,
                "message":
                    "Location not received."
            }), 400

        try:

            latitude = float(
                latitude
            )

            longitude = float(
                longitude
            )

        except (
            ValueError,
            TypeError
        ):

            return jsonify({
                "success": False,
                "message":
                    "Invalid location values."
            }), 400

        if (
            latitude < -90
            or latitude > 90
        ):

            return jsonify({
                "success": False,
                "message":
                    "Invalid latitude."
            }), 400

        if (
            longitude < -180
            or longitude > 180
        ):

            return jsonify({
                "success": False,
                "message":
                    "Invalid longitude."
            }), 400

        conn = get_db()

        try:

            conn.execute(
                """
                UPDATE workers
                SET
                    latitude = ?,
                    longitude = ?
                WHERE worker_id = ?
                """,
                (
                    latitude,
                    longitude,
                    session["user_id"]
                )
            )

            conn.commit()

        except Exception as error:

            conn.rollback()

            print(
                "LOCATION DATABASE ERROR:",
                error
            )

            return jsonify({
                "success": False,
                "message":
                    "Unable to update location."
            }), 500

        finally:

            conn.close()

        return jsonify({
            "success": True,
            "message":
                "Live location updated successfully."
        })

    except Exception as error:

        print(
            "LOCATION ERROR:",
            error
        )

        return jsonify({
            "success": False,
            "message":
                "Unable to update location."
        }), 500


# ============================================================
# BANK
# ============================================================

@worker_bp.route(
    "/worker/bank",
    methods=["GET", "POST"]
)
@worker_required
def worker_bank():

    worker = get_current_worker()

    if not worker:

        return redirect(url_for("auth.login", role="worker"))

    return render_template(
        "worker/bank.html",
        worker=worker,
        indian_banks=INDIAN_BANKS
    )


# ============================================================
# CHANGE BANK
# ============================================================

@worker_bp.route(
    "/worker/change-bank",
    methods=["POST"]
)
@worker_required
def change_bank():

    worker = get_current_worker()

    if not worker:

        return redirect(url_for("auth.login", role="worker"))

    bank_name = request.form.get(
        "bank_name",
        ""
    ).strip()

    account_holder = request.form.get(
        "account_holder",
        ""
    ).strip()

    account_number = request.form.get(
        "account_number",
        ""
    ).strip()

    ifsc = request.form.get(
        "ifsc",
        ""
    ).strip().upper()

    if not bank_name:

        flash(
            "Bank name is required.",
            "danger"
        )

        return redirect(
            "/worker/bank"
        )

    if bank_name not in INDIAN_BANKS:

        flash(
            "Please select a valid bank from the list.",
            "danger"
        )

        return redirect(
            "/worker/bank"
        )

    if not account_holder:

        flash(
            "Account holder name is required.",
            "danger"
        )

        return redirect(
            "/worker/bank"
        )

    if (
        not account_number
        or not account_number.isdigit()
        or not 9 <= len(account_number) <= 18
    ):

        flash(
            "Please enter a valid account number.",
            "danger"
        )

        return redirect(
            "/worker/bank"
        )

    if not re.fullmatch(r"[A-Z]{4}0[A-Z0-9]{6}", ifsc):

        flash(
            "Please enter a valid IFSC code.",
            "danger"
        )

        return redirect(
            "/worker/bank"
        )

    expected_prefixes = BANK_IFSC_PREFIXES.get(bank_name)
    if expected_prefixes and ifsc[:4] not in expected_prefixes:

        flash(
            "The IFSC code does not match the selected bank.",
            "danger"
        )

        return redirect(
            "/worker/bank"
        )

    allowed, retry_after = reserve_otp_request(
        "worker_bank_change", worker["worker_id"], request.remote_addr or "unknown"
    )
    if not allowed:
        flash(f"Please wait {retry_after} seconds before requesting another OTP.", "warning")
        return redirect("/worker/bank")

    otp = generate_otp()
    pending_token = issue_pending_change(
        "worker", worker["worker_id"], "bank_change",
        {
            "bank_name": bank_name,
            "account_holder": account_holder,
            "account_number": account_number,
            "ifsc": ifsc,
        },
        otp,
    )
    session.pop("new_bank", None)
    session.pop("bank_otp", None)
    session.pop("bank_expiry", None)
    session["pending_worker_bank_token"] = pending_token

    try:
        send_otp(worker["email"], otp)
    except Exception:
        delete_pending_change("worker", worker["worker_id"], "bank_change", pending_token)
        session.pop("pending_worker_bank_token", None)
        flash("OTP could not be sent. Please try again later.", "danger")
        return redirect("/worker/bank")

    flash("OTP sent to your email.", "success")
    return redirect("/worker/verify-bank-otp")

# ============================================================
# RESEND BANK OTP
# ============================================================

@worker_bp.route(
    "/worker/resend-bank-otp", methods=["POST"]
)
@worker_required
def resend_bank_otp():
    worker = get_current_worker()
    token = session.get("pending_worker_bank_token")
    if not worker:
        return redirect(url_for("auth.login", role="worker"))
    if not token:
        flash("Please start the bank update again.", "warning")
        return redirect("/worker/bank")

    allowed, retry_after = reserve_otp_request(
        "worker_bank_change", worker["worker_id"], request.remote_addr or "unknown"
    )
    if not allowed:
        flash(f"Please wait {retry_after} seconds before requesting another OTP.", "warning")
        return redirect("/worker/verify-bank-otp")

    otp = generate_otp()
    status = refresh_pending_change("worker", worker["worker_id"], "bank_change", token, otp)
    if status != "ok":
        session.pop("pending_worker_bank_token", None)
        flash("Bank update session expired. Please start again.", "warning")
        return redirect("/worker/bank")
    try:
        send_otp(worker["email"], otp)
    except Exception:
        delete_pending_change("worker", worker["worker_id"], "bank_change", token)
        session.pop("pending_worker_bank_token", None)
        flash("OTP could not be sent. Please start the bank update again.", "danger")
        return redirect("/worker/bank")
    flash("New OTP sent successfully.", "success")
    return redirect("/worker/verify-bank-otp")

# ============================================================
# VERIFY BANK OTP
# ============================================================

@worker_bp.route(
    "/worker/verify-bank-otp",
    methods=["GET", "POST"]
)
@worker_required
def verify_bank_otp():
    worker = get_current_worker()
    token = session.get("pending_worker_bank_token")
    if not worker:
        return redirect(url_for("auth.login", role="worker"))
    if not token:
        flash("Please start the bank update again.", "warning")
        return redirect("/worker/bank")

    if request.method == "POST":
        otp = request.form.get("otp", "").strip()
        status, bank, attempts = verify_pending_change(
            "worker", worker["worker_id"], "bank_change", token, otp
        )
        if status != "ok":
            if status in {"missing", "expired", "locked", "invalid_state"}:
                session.pop("pending_worker_bank_token", None)
                flash("OTP expired or too many attempts. Please start the bank update again.", "danger")
                return redirect("/worker/bank")
            flash(f"Invalid OTP. Attempt {attempts} of 5.", "danger")
            return redirect("/worker/verify-bank-otp")

        # The challenge has been consumed atomically; clear the client reference
        # even if the database update below needs to be retried from a fresh OTP.
        session.pop("pending_worker_bank_token", None)
        conn = get_db()
        try:
            cur = conn.execute(
                """UPDATE workers SET bank_name=?, account_holder=?, account_number=?, ifsc=?
                   WHERE worker_id=?""",
                (bank["bank_name"], bank["account_holder"], bank["account_number"], bank["ifsc"], worker["worker_id"]),
            )
            if cur.rowcount != 1:
                conn.rollback()
                flash("Worker account could not be found.", "danger")
                return redirect("/worker/bank")
            conn.commit()
        except Exception:
            conn.rollback()
            current_app.logger.exception("Worker bank update failed")
            flash("Bank details could not be updated. Please try again.", "danger")
            return redirect("/worker/bank")
        finally:
            conn.close()

        delete_pending_change("worker", worker["worker_id"], "bank_change", token)
        session.pop("pending_worker_bank_token", None)
        flash("Bank details updated successfully.", "success")
        return redirect("/worker/bank")

    return render_template("worker/verify_bank_otp.html")

# ============================================================
# SETTINGS
# ============================================================

@worker_bp.route(
    "/worker/settings"
)
@worker_required
def worker_settings():

    worker = get_current_worker()

    if not worker:

        return redirect(url_for("auth.login", role="worker"))

    return render_template(
        "worker/settings.html",
        worker=worker
    )


# ============================================================
# CHANGE PASSWORD
# ============================================================

@worker_bp.route(
    "/worker/change-password",
    methods=["GET", "POST"]
)
@worker_required
def change_password():

    worker = get_current_worker()

    if not worker:

        return redirect(url_for("auth.login", role="worker"))

    if request.method == "POST":

        current_password = request.form.get(
            "current_password",
            ""
        )

        new_password = request.form.get(
            "new_password",
            ""
        )

        confirm_password = request.form.get(
            "confirm_password",
            ""
        )

        if not check_password_hash(
            worker["password"],
            current_password
        ):

            flash(
                "Current Password is Incorrect.",
                "danger"
            )

            return redirect(
                "/worker/change-password"
            )

        if len(new_password) < 8 or not re.search(r"[A-Za-z]", new_password) or not re.search(r"\d", new_password):

            flash(
                "Password must be at least 8 characters and include a letter and a number.",
                "danger"
            )

            return redirect(
                "/worker/change-password"
            )

        if new_password != confirm_password:

            flash(
                "New Password and Confirm Password do not match.",
                "danger"
            )

            return redirect(
                "/worker/change-password"
            )

        allowed, retry_after = reserve_otp_request(
            "worker_password_change", worker["worker_id"], request.remote_addr or "unknown"
        )
        if not allowed:
            flash(f"Please wait {retry_after} seconds before requesting another OTP.", "warning")
            return redirect("/worker/change-password")

        otp = generate_otp()
        pending_token = issue_pending_change(
            "worker", worker["worker_id"], "password_change",
            {"password_hash": generate_password_hash(new_password)}, otp,
        )
        for key in ("password_otp", "password_otp_expiry", "new_password"):
            session.pop(key, None)
        session["pending_worker_password_token"] = pending_token
        try:
            send_otp(worker["email"], otp)
        except Exception:
            delete_pending_change("worker", worker["worker_id"], "password_change", pending_token)
            session.pop("pending_worker_password_token", None)
            flash("OTP could not be sent. Please try again later.", "danger")
            return redirect("/worker/change-password")
        flash("OTP sent to your email.", "success")
        return redirect("/worker/verify-password-otp")

    return render_template(
        "worker/change_password.html"
    )


# ============================================================
# VERIFY PASSWORD OTP
# ============================================================

@worker_bp.route(
    "/worker/verify-password-otp",
    methods=["GET", "POST"]
)
@worker_required
def verify_password_otp():
    worker = get_current_worker()
    token = session.get("pending_worker_password_token")
    if not worker:
        return redirect(url_for("auth.login", role="worker"))
    if not token:
        flash("Please start the password change again.", "warning")
        return redirect("/worker/change-password")

    if request.method == "POST":
        otp = request.form.get("otp", "").strip()
        status, payload, attempts = verify_pending_change(
            "worker", worker["worker_id"], "password_change", token, otp
        )
        if status != "ok":
            if status in {"missing", "expired", "locked", "invalid_state"}:
                session.pop("pending_worker_password_token", None)
                flash("OTP expired or too many attempts. Please start again.", "danger")
                return redirect("/worker/change-password")
            flash(f"Invalid OTP. Attempt {attempts} of 5.", "danger")
            return redirect("/worker/verify-password-otp")

        session.pop("pending_worker_password_token", None)
        new_password_hash = payload.get("password_hash")
        if not new_password_hash:
            session.pop("pending_worker_password_token", None)
            delete_pending_change("worker", worker["worker_id"], "password_change", token)
            flash("Password change session expired. Please try again.", "danger")
            return redirect("/worker/change-password")

        conn = get_db()
        try:
            cur = conn.execute(
                "UPDATE workers SET password=? WHERE worker_id=?",
                (new_password_hash, worker["worker_id"]),
            )
            if cur.rowcount != 1:
                conn.rollback()
                flash("Worker account could not be found.", "danger")
                return redirect("/worker/change-password")
            conn.commit()
        except Exception:
            conn.rollback()
            current_app.logger.exception("Worker password update failed")
            flash("Password could not be changed. Please try again.", "danger")
            return redirect("/worker/change-password")
        finally:
            conn.close()

        delete_pending_change("worker", worker["worker_id"], "password_change", token)
        session.pop("pending_worker_password_token", None)
        flash("Password changed successfully.", "success")
        return redirect("/worker/settings")

    return render_template("worker/verify_password_otp.html")

# ============================================================
# RESEND PASSWORD OTP
# ============================================================

@worker_bp.route(
    "/worker/resend-password-otp", methods=["POST"]
)
@worker_required
def resend_password_otp():
    worker = get_current_worker()
    token = session.get("pending_worker_password_token")
    if not worker:
        return redirect(url_for("auth.login", role="worker"))
    if not token:
        flash("Please start the password change again.", "warning")
        return redirect("/worker/change-password")

    allowed, retry_after = reserve_otp_request(
        "worker_password_change", worker["worker_id"], request.remote_addr or "unknown"
    )
    if not allowed:
        flash(f"Please wait {retry_after} seconds before requesting another OTP.", "warning")
        return redirect("/worker/verify-password-otp")

    otp = generate_otp()
    status = refresh_pending_change("worker", worker["worker_id"], "password_change", token, otp)
    if status != "ok":
        session.pop("pending_worker_password_token", None)
        flash("Password change session expired. Please start again.", "warning")
        return redirect("/worker/change-password")
    try:
        send_otp(worker["email"], otp)
    except Exception:
        delete_pending_change("worker", worker["worker_id"], "password_change", token)
        session.pop("pending_worker_password_token", None)
        flash("OTP could not be sent. Please start again.", "danger")
        return redirect("/worker/change-password")
    flash("New OTP sent successfully.", "success")
    return redirect("/worker/verify-password-otp")

# ============================================================
# ABOUT
# ============================================================

@worker_bp.route(
    "/worker/about"
)
@worker_required
def about():

    return render_template(
        "worker/about.html"
    )


# ============================================================
# PRIVACY POLICY
# ============================================================

@worker_bp.route(
    "/worker/privacy_policy"
)
@worker_required
def privacy_policy():

    return render_template(
        "worker/privacy_policy.html"
    )


# ============================================================
# TERMS
# ============================================================

@worker_bp.route(
    "/worker/terms"
)
@worker_required
def terms():

    return render_template(
        "worker/terms.html"
    )


# ============================================================
# HELP
# ============================================================

@worker_bp.route(
    "/worker/help"
)
@worker_required
def help():

    return render_template(
        "worker/help.html"
    )


# ============================================================
# RATE APP
# ============================================================

@worker_bp.route(
    "/worker/rate-app",
    methods=["GET", "POST"]
)
@worker_required
def rate_app():

    if request.method == "POST":

        rating = request.form.get(
            "rating"
        )

        feedback = request.form.get(
            "feedback",
            ""
        ).strip()

        if not rating:

            flash(
                "Please select a rating.",
                "danger"
            )

            return redirect(
                "/worker/rate-app"
            )

        try:

            rating = int(
                rating
            )

        except (
            ValueError,
            TypeError
        ):

            flash(
                "Invalid rating.",
                "danger"
            )

            return redirect(
                "/worker/rate-app"
            )

        if rating < 1 or rating > 5:

            flash(
                "Rating must be between 1 and 5.",
                "danger"
            )

            return redirect(
                "/worker/rate-app"
            )

        feedback = feedback[:1000]

        worker_id = session.get(
            "user_id"
        )

        if not worker_id:

            return redirect(url_for("auth.login", role="worker"))

        conn = get_db()

        try:

            conn.execute(
                """
                INSERT INTO worker_ratings
                (
                    worker_id,
                    rating,
                    feedback
                )
                VALUES (?, ?, ?)
                """,
                (
                    worker_id,
                    rating,
                    feedback
                )
            )

            conn.commit()

        except Exception as error:

            conn.rollback()

            print(
                "WORKER RATING ERROR:",
                error
            )

            flash(
                "Rating could not be submitted.",
                "danger"
            )

            return redirect(
                "/worker/rate-app"
            )

        finally:

            conn.close()

        flash(
            "Thank you! Your review has been submitted successfully.",
            "success"
        )

        return redirect(
            "/worker/rate-app"
        )

    return render_template(
        "worker/rate_app.html"
    )


# ============================================================
# CHECK UPDATE
# ============================================================

@worker_bp.route(
    "/worker/check-update"
)
@worker_required
def check_update():

    current_version = "1.0.0"

    latest_version = "1.0.0"

    update_available = (
        current_version != latest_version
    )

    return render_template(
        "worker/check_update.html",
        current_version=current_version,
        latest_version=latest_version,
        update_available=update_available
    )


# ============================================================
# APP VERSION
# ============================================================

@worker_bp.route(
    "/worker/app-version"
)
@worker_required
def app_version():

    version = "1.0.0"

    return render_template(
        "worker/app_version.html",
        version=version
    )


# ============================================================
# FAQ
# ============================================================

@worker_bp.route(
    "/worker/faq"
)
@worker_required
def faq():

    return render_template(
        "worker/faq.html"
    )