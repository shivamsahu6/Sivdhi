import secrets
import sqlite3
from flask import (
    Blueprint,
    render_template,
    session,
    request,
    redirect,
    flash
)
from werkzeug.security import generate_password_hash, check_password_hash
from database import get_db
from werkzeug.utils import secure_filename
from utils.email_sender import send_otp
from utils.otp_security import reserve_otp_request
from utils.pending_changes import issue_pending_change, refresh_pending_change, verify_pending_change, delete_pending_change
from routes.decorators import partner_required
from utils.assignment_monitor import ensure_assignment_monitor_schema, reassign_booking
from utils.sivdhi_finance import ensure_finance_schema, money as finance_money, settle_completed_booking
from utils.booking_schedule import india_now

import os
import random
import re

from datetime import (
    datetime,
    timedelta
)

def validate_bank_details(bank_name, account_holder, account_number, ifsc):
    """Validate partner bank details before saving or changing them."""

    bank_name = (bank_name or "").strip()
    account_holder = (account_holder or "").strip()
    account_number = (account_number or "").strip()
    ifsc = (ifsc or "").strip().upper()

    if not bank_name:
        return False, "Bank name is required."

    if len(bank_name) > 100:
        return False, "Bank name is too long."

    if not account_holder:
        return False, "Account holder name is required."

    if len(account_holder) > 100:
        return False, "Account holder name is too long."

    if not re.fullmatch(r"\d{9,18}", account_number):
        return False, "Account number must contain 9 to 18 digits."

    if not re.fullmatch(r"[A-Z]{4}0[A-Z0-9]{6}", ifsc):
        return False, "Please enter a valid IFSC code."

    return True, ""



partner_bp = Blueprint(
    "partner",
    __name__
)


def _ensure_partner_worker_controls_schema(conn):
    """Add the one additive field needed for partner-managed availability."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(workers)").fetchall()}
    if "partner_availability_override" not in columns:
        conn.execute("ALTER TABLE workers ADD COLUMN partner_availability_override INTEGER DEFAULT NULL")


def _partner_booking_for_action(conn, booking_id, partner_id):
    """Return a booking only when it belongs to this partner and its assigned worker is theirs."""
    scope = _partner_booking_scope_sql()
    row = conn.execute(f"""
        SELECT b.*,
               COALESCE(NULLIF(b.booking_status,''), NULLIF(b.status,''), 'pending') AS current_status
        FROM bookings b
        WHERE b.id=? AND {scope}
        LIMIT 1
    """, (booking_id, partner_id, partner_id, partner_id)).fetchone()
    if not row:
        return None, None

    assigned_value = row["worker_id"] or row["assigned_worker_id"]
    if not assigned_value:
        return row, None

    worker = conn.execute("""
        SELECT w.id, w.worker_id, w.fullname
        FROM workers w
        JOIN partner_workers pw
          ON CAST(pw.worker_id AS TEXT)=CAST(w.worker_id AS TEXT)
          OR CAST(pw.worker_id AS TEXT)=CAST(w.id AS TEXT)
        WHERE w.id=? AND CAST(pw.partner_id AS TEXT)=CAST(? AS TEXT)
          AND COALESCE(w.is_deleted,0)=0
        LIMIT 1
    """, (assigned_value, partner_id)).fetchone()
    if not worker:
        # Compatibility with older rows that stored the worker's public ID.
        worker = conn.execute("""
            SELECT w.id, w.worker_id, w.fullname
            FROM workers w
            JOIN partner_workers pw
              ON CAST(pw.worker_id AS TEXT)=CAST(w.worker_id AS TEXT)
              OR CAST(pw.worker_id AS TEXT)=CAST(w.id AS TEXT)
            WHERE CAST(w.worker_id AS TEXT)=CAST(? AS TEXT)
              AND CAST(pw.partner_id AS TEXT)=CAST(? AS TEXT)
              AND COALESCE(w.is_deleted,0)=0
            LIMIT 1
        """, (assigned_value, partner_id)).fetchone()
    return row, worker


def _partner_booking_balance_due(booking):
    total = finance_money(booking["total_amount"] if booking["total_amount"] is not None else booking["amount"])
    advance_paid = finance_money(booking["advance_paid_amount"])
    balance_paid = finance_money(booking["balance_paid_amount"])
    saved_due = finance_money(booking["balance_due_amount"])
    return saved_due if saved_due > 0 else finance_money(max(0.0, total - advance_paid - balance_paid))


def _partner_internal_notice(conn, worker_db_id, partner_id, title, message):
    for user_type, user_id in (("worker", worker_db_id), ("partner", partner_id)):
        if user_id:
            conn.execute("""
                INSERT INTO notifications(user_type,user_id,title,message,is_read,created_at)
                VALUES(?,?,?,?,0,CURRENT_TIMESTAMP)
            """, (user_type, str(user_id), title[:160], message[:1000]))

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

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "static", "uploads")
MAX_IMAGE_SIZE = 5 * 1024 * 1024
ALLOWED_IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}

def validate_uploaded_image(photo):
    """Validate image extension, size and file signature."""
    if not photo or not photo.filename:
        return False, "Please select an image."
    if "." not in photo.filename or photo.filename.rsplit(".", 1)[1].lower() not in ALLOWED_IMAGE_EXTENSIONS:
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


# ======================================
# Partner Home
# ======================================

@partner_bp.route("/partner/home")
@partner_required
def partner_home():
    conn = get_db()
    try:
        cur = conn.cursor()

        cur.execute("""
            SELECT COUNT(DISTINCT pw.worker_id)
            FROM partner_workers pw
            JOIN workers w
              ON CAST(w.worker_id AS TEXT)=CAST(pw.worker_id AS TEXT)
              OR CAST(w.id AS TEXT)=CAST(pw.worker_id AS TEXT)
            WHERE CAST(pw.partner_id AS TEXT)=CAST(? AS TEXT)
              AND COALESCE(w.is_deleted,0)=0
        """, (session["user_id"],))
        total_workers = cur.fetchone()[0] or 0

        # Partner jobs now come from the canonical bookings system.
        cur.execute("""
            SELECT COUNT(*)
            FROM bookings
            WHERE CAST(COALESCE(partner_id, assigned_partner_id) AS TEXT)=CAST(? AS TEXT)
        """, (session["user_id"],))
        total_jobs = cur.fetchone()[0] or 0

        cur.execute("""
            SELECT COUNT(*)
            FROM bookings
            WHERE CAST(COALESCE(partner_id, assigned_partner_id) AS TEXT)=CAST(? AS TEXT)
              AND LOWER(COALESCE(booking_status,status,'pending')) IN ('pending','assigned')
        """, (session["user_id"],))
        pending_jobs = cur.fetchone()[0] or 0

        cur.execute("""
            SELECT COUNT(*)
            FROM notifications
            WHERE user_type='partner'
              AND CAST(user_id AS TEXT)=CAST(? AS TEXT)
              AND COALESCE(is_read,0)=0
        """, (session["user_id"],))
        total_notifications = cur.fetchone()[0] or 0

        cur.execute("""
            SELECT wallet
            FROM partners
            WHERE partner_id=?
            LIMIT 1
        """, (session["user_id"],))
        partner = cur.fetchone()
        wallet = float(partner["wallet"] or 0) if partner else 0

        return render_template(
            "partner/home.html",
            total_workers=total_workers,
            total_jobs=total_jobs,
            pending_jobs=pending_jobs,
            total_notifications=total_notifications,
            wallet=wallet
        )
    finally:
        conn.close()


# ======================================
# Workers
# ======================================

@partner_bp.route("/partner/workers")
@partner_required
def workers():
    conn = get_db()
    try:
        _ensure_partner_worker_controls_schema(conn)
        conn.commit()
        workers = conn.execute("""
            SELECT
                w.worker_id,
                w.fullname,
                w.mobile,
                w.status,
                COALESCE(w.is_online,0) AS is_online,
                w.last_seen_at,
                w.partner_availability_override
            FROM partner_workers pw
            JOIN workers w
              ON CAST(pw.worker_id AS TEXT)=CAST(w.worker_id AS TEXT)
              OR CAST(pw.worker_id AS TEXT)=CAST(w.id AS TEXT)
            WHERE CAST(pw.partner_id AS TEXT)=CAST(? AS TEXT)
              AND COALESCE(w.is_deleted,0)=0
            ORDER BY pw.id DESC
        """, (session["user_id"],)).fetchall()

        return render_template("partner/workers.html", workers=workers)
    finally:
        conn.close()


@partner_bp.route("/partner/worker/<worker_id>/availability", methods=["POST"])
@partner_required
def update_worker_availability(worker_id):
    desired = (request.form.get("availability") or "").strip().lower()
    if desired not in {"online", "offline"}:
        flash("Invalid worker availability status.", "danger")
        return redirect("/partner/workers")

    partner_id = session["user_id"]
    is_online = 1 if desired == "online" else 0
    conn = get_db()
    try:
        _ensure_partner_worker_controls_schema(conn)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(workers)").fetchall()}
        worker = conn.execute("""
            SELECT w.id, w.fullname
            FROM workers w
            JOIN partner_workers pw
              ON CAST(pw.worker_id AS TEXT)=CAST(w.worker_id AS TEXT)
              OR CAST(pw.worker_id AS TEXT)=CAST(w.id AS TEXT)
            WHERE CAST(pw.partner_id AS TEXT)=CAST(? AS TEXT)
              AND CAST(w.worker_id AS TEXT)=CAST(? AS TEXT)
              AND COALESCE(w.is_deleted,0)=0
            LIMIT 1
        """, (partner_id, worker_id)).fetchone()
        if not worker:
            flash("Worker not found in your partner account.", "danger")
            return redirect("/partner/workers")

        updates = ["is_online=?", "partner_availability_override=?"]
        params = [is_online, is_online]
        if "is_available" in columns:
            updates.append("is_available=?")
            params.append(is_online)
        params.append(worker["id"])
        conn.execute(f"UPDATE workers SET {', '.join(updates)} WHERE id=?", params)
        conn.commit()
        flash(f"{worker['fullname']} is now {desired.title()}.", "success")
    except Exception as error:
        conn.rollback()
        print("PARTNER WORKER AVAILABILITY ERROR:", error)
        flash("Worker availability could not be updated.", "danger")
    finally:
        conn.close()
    return redirect("/partner/workers")


# ======================================
# Worker Profile
# ======================================

@partner_bp.route("/partner/worker/<worker_id>")
@partner_required
def worker_profile(worker_id):
    conn = get_db()
    try:
        # Do not expose the worker's private email to a partner.
        worker = conn.execute("""
            SELECT
                w.id,
                w.worker_id,
                w.fullname,
                w.mobile,
                w.referral_code,
                w.status,
                w.created_at,
                w.profile_photo,
                w.skills,
                w.experience,
                w.address,
                w.city,
                w.state,
                w.pincode
            FROM workers w
            JOIN partner_workers pw
              ON CAST(w.worker_id AS TEXT)=CAST(pw.worker_id AS TEXT)
            WHERE CAST(pw.partner_id AS TEXT)=CAST(? AS TEXT)
              AND CAST(w.worker_id AS TEXT)=CAST(? AS TEXT)
              AND COALESCE(w.is_deleted,0)=0
            LIMIT 1
        """, (session["user_id"], worker_id)).fetchone()
    finally:
        conn.close()

    if not worker:
        flash("Worker not found.", "danger")
        return redirect("/partner/workers")

    return render_template("partner/worker_profile.html", worker=worker)


# ======================================
# Partner Jobs - Canonical Bookings
# ======================================

def _partner_booking_scope_sql():
    """Return the SQL condition used to prove a booking belongs to this partner."""
    return """
        (
            CAST(b.partner_id AS TEXT)=CAST(? AS TEXT)
            OR CAST(b.assigned_partner_id AS TEXT)=CAST(? AS TEXT)
            OR EXISTS (
                SELECT 1
                FROM partner_workers pw
                JOIN workers w
                  ON CAST(w.worker_id AS TEXT)=CAST(pw.worker_id AS TEXT)
                  OR CAST(w.id AS TEXT)=CAST(pw.worker_id AS TEXT)
                WHERE CAST(pw.partner_id AS TEXT)=CAST(? AS TEXT)
                  AND (
                    CAST(w.id AS TEXT)=CAST(COALESCE(b.worker_id,b.assigned_worker_id) AS TEXT)
                    OR CAST(w.worker_id AS TEXT)=CAST(COALESCE(b.worker_id,b.assigned_worker_id) AS TEXT)
                  )
            )
        )
    """


def _booking_row_for_partner(row):
    """Expose booking data using the field names expected by existing partner templates."""
    return row


@partner_bp.route("/partner/jobs")
@partner_required
def jobs():
    partner_id = session["user_id"]
    conn = get_db()
    try:
        sql = f"""
            SELECT
                b.id,
                b.booking_id,
                b.booking_id AS job_id,
                b.category,
                COALESCE(b.service,b.category) AS service,
                COALESCE(b.description,b.notes,'') AS description,
                b.preferred_date AS booking_date,
                b.preferred_time AS booking_time,
                COALESCE(b.total_amount,b.amount,0) AS amount,
                b.payment_status,
                COALESCE(b.booking_status,b.status,'pending') AS job_status,
                b.customer_id,
                COALESCE(c.fullname,'Customer') AS customer_name,
                '' AS customer_mobile,
                b.address AS customer_address,
                b.city,
                b.state,
                b.pincode,
                b.worker_id,
                b.assigned_worker_id,
                b.partner_id,
                b.assigned_partner_id,
                b.cancellation_reason,
                b.partner_acknowledged_at,
                b.created_at,
                b.updated_at
            FROM bookings b
            LEFT JOIN customers c ON c.id=b.customer_id
            WHERE {_partner_booking_scope_sql()}
            ORDER BY b.id DESC
        """
        rows = conn.execute(sql, (partner_id, partner_id, partner_id)).fetchall()
        return render_template("partner/jobs.html", jobs=rows)
    except sqlite3.Error as error:
        print("PARTNER BOOKINGS ERROR:", error)
        flash("Unable to load jobs right now.", "danger")
        return render_template("partner/jobs.html", jobs=[])
    finally:
        conn.close()


@partner_bp.route("/partner/job/<job_ref>")
@partner_required
def job_details(job_ref):
    partner_id = session["user_id"]
    conn = get_db()
    try:
        ensure_finance_schema(conn)
        conn.commit()
        scope = _partner_booking_scope_sql()
        job = conn.execute(f"""
            SELECT
                b.*,
                b.booking_id AS job_id,
                COALESCE(NULLIF(b.service,''),b.category) AS partner_service,
                COALESCE(b.description,b.notes,'') AS partner_description,
                COALESCE(NULLIF(b.preferred_date,''),b.booking_date) AS partner_booking_date,
                COALESCE(NULLIF(b.preferred_time,''),b.booking_time) AS partner_booking_time,
                COALESCE(b.total_amount,b.amount,0) AS partner_amount,
                COALESCE(NULLIF(b.booking_status,''),b.status,'pending') AS job_status,
                COALESCE(c.fullname,'Customer') AS customer_name,
                '' AS customer_mobile,
                b.address AS customer_address,
                w.fullname AS assigned_worker_name
            FROM bookings b
            LEFT JOIN customers c ON c.id=b.customer_id
            LEFT JOIN workers w ON w.id=COALESCE(b.worker_id,b.assigned_worker_id)
            WHERE (CAST(b.id AS TEXT)=CAST(? AS TEXT)
                   OR CAST(b.booking_id AS TEXT)=CAST(? AS TEXT))
              AND {scope}
            LIMIT 1
        """, (job_ref, job_ref, partner_id, partner_id, partner_id)).fetchone()
    except sqlite3.Error as error:
        print("PARTNER JOB DETAILS ERROR:", error)
        job = None
    finally:
        conn.close()

    if not job:
        flash("Job not found.", "danger")
        return redirect("/partner/jobs")

    status = str(job["job_status"] or "pending").lower()
    now = india_now()
    arrive_available_at = None
    can_arrive = False
    schedule_date = job["preferred_date"] or job["booking_date"]
    schedule_time = job["preferred_time"] or job["booking_time"]
    if schedule_date and schedule_time:
        try:
            scheduled_at = datetime.fromisoformat(str(schedule_date).strip()[:10] + "T" + str(schedule_time).strip()[:5])
            if scheduled_at.tzinfo is None and now.tzinfo is not None:
                scheduled_at = scheduled_at.replace(tzinfo=now.tzinfo)
            arrive_available_at = scheduled_at + timedelta(hours=1)
            can_arrive = now >= arrive_available_at
        except (TypeError, ValueError):
            can_arrive = False
    else:
        can_arrive = False

    balance_due = _partner_booking_balance_due(job)
    balance_status = str(job["balance_payment_status"] or "DUE").upper()
    paid_states = {"PAID", "SUCCESS", "DEMO_PAID", "COLLECTED", "COMPLETED"}
    balance_paid = balance_due <= 0 or balance_status in paid_states
    return render_template(
        "partner/job_details.html", job=job,
        can_arrive=can_arrive,
        arrive_available_at=arrive_available_at,
        balance_due=balance_due,
        balance_paid=balance_paid,
    )


@partner_bp.route("/partner/job/<int:booking_id>/accept", methods=["POST"])
@partner_required
def accept_job(booking_id):
    """Partner can accept an assigned booking on behalf of their own worker."""
    partner_id = session["user_id"]
    conn = get_db()
    try:
        ensure_assignment_monitor_schema(conn)
        ensure_finance_schema(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        row, worker = _partner_booking_for_action(conn, booking_id, partner_id)
        if not row or not worker:
            conn.rollback()
            flash("Job or assigned worker was not found in your partner account.", "danger")
            return redirect("/partner/jobs")
        status = str(row["current_status"] or "pending").lower()
        if status == "accepted":
            conn.rollback()
            flash("This worker has already accepted the booking.", "info")
            return redirect(f"/partner/job/{booking_id}")
        if status not in {"assigned", "pending"}:
            conn.rollback()
            flash("Only an assigned or pending booking can be accepted.", "warning")
            return redirect(f"/partner/job/{booking_id}")

        conn.execute("""
            UPDATE bookings
            SET booking_status='accepted', status='accepted',
                partner_id=COALESCE(NULLIF(partner_id,''),?),
                assigned_partner_id=COALESCE(NULLIF(assigned_partner_id,''),?),
                partner_acknowledged_at=COALESCE(partner_acknowledged_at,CURRENT_TIMESTAMP),
                assigned_at=COALESCE(assigned_at,CURRENT_TIMESTAMP),
                updated_at=CURRENT_TIMESTAMP
            WHERE id=?
        """, (partner_id, partner_id, booking_id))
        try:
            conn.execute("""
                UPDATE booking_assignment_offers
                SET status=CASE WHEN worker_id=? THEN 'accepted' ELSE 'cancelled' END,
                    responded_at=CURRENT_TIMESTAMP
                WHERE booking_db_id=? AND status='offered'
            """, (worker["id"], booking_id))
        except sqlite3.Error:
            pass
        _partner_internal_notice(
            conn, worker["id"], partner_id, "Booking Accepted by Partner",
            f"Your partner accepted booking {row['booking_id']} on your behalf. Follow the scheduled time and job steps.",
        )
        conn.commit()
        flash("Booking accepted successfully for your worker.", "success")
    except Exception as error:
        conn.rollback()
        print("PARTNER ACCEPT ERROR:", error)
        flash("Unable to accept this job right now.", "danger")
    finally:
        conn.close()
    return redirect(f"/partner/job/{booking_id}")


@partner_bp.route("/partner/job/<int:booking_id>/arrive", methods=["POST"])
@partner_required
def partner_arrive_job(booking_id):
    partner_id = session["user_id"]
    conn = get_db()
    try:
        ensure_finance_schema(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        row, worker = _partner_booking_for_action(conn, booking_id, partner_id)
        if not row or not worker:
            conn.rollback()
            flash("Job or assigned worker was not found in your partner account.", "danger")
            return redirect("/partner/jobs")
        if str(row["current_status"] or "").lower() != "accepted":
            conn.rollback()
            flash("Accept the booking before marking arrival.", "warning")
            return redirect(f"/partner/job/{booking_id}")
        schedule_date = row["preferred_date"] or row["booking_date"]
        schedule_time = row["preferred_time"] or row["booking_time"]
        if not schedule_date or not schedule_time:
            conn.rollback()
            flash("The booking date/time is missing. Contact support before marking arrival.", "danger")
            return redirect(f"/partner/job/{booking_id}")
        try:
            scheduled_at = datetime.fromisoformat(str(schedule_date).strip()[:10] + "T" + str(schedule_time).strip()[:5])
            now = india_now()
            if scheduled_at.tzinfo is None and now.tzinfo is not None:
                scheduled_at = scheduled_at.replace(tzinfo=now.tzinfo)
            allowed_at = scheduled_at + timedelta(hours=1)
        except (TypeError, ValueError):
            conn.rollback()
            flash("The customer's booking date/time is invalid.", "danger")
            return redirect(f"/partner/job/{booking_id}")
        if now < allowed_at:
            conn.rollback()
            flash("Arrive will be available one hour after the customer's booked time.", "warning")
            return redirect(f"/partner/job/{booking_id}")
        conn.execute("UPDATE bookings SET worker_arrived_at=COALESCE(worker_arrived_at,CURRENT_TIMESTAMP), updated_at=CURRENT_TIMESTAMP WHERE id=?", (booking_id,))
        _partner_internal_notice(conn, worker["id"], partner_id, "Worker Arrival Recorded", f"Arrival was recorded for booking {row['booking_id']} by the partner.")
        conn.commit()
        flash("Arrival recorded. The customer can now pay any remaining balance from My Bookings.", "success")
    except Exception as error:
        conn.rollback()
        print("PARTNER ARRIVE ERROR:", error)
        flash("Arrival could not be recorded.", "danger")
    finally:
        conn.close()
    return redirect(f"/partner/job/{booking_id}")


@partner_bp.route("/partner/job/<int:booking_id>/start", methods=["POST"])
@partner_required
def partner_start_job(booking_id):
    partner_id = session["user_id"]
    conn = get_db()
    try:
        ensure_finance_schema(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        row, worker = _partner_booking_for_action(conn, booking_id, partner_id)
        if not row or not worker:
            conn.rollback()
            flash("Job or assigned worker was not found in your partner account.", "danger")
            return redirect("/partner/jobs")
        if str(row["current_status"] or "").lower() != "accepted":
            conn.rollback()
            flash("Only an accepted booking can be started.", "warning")
            return redirect(f"/partner/job/{booking_id}")
        if not row["worker_arrived_at"]:
            conn.rollback()
            flash("Record worker arrival before starting the job.", "warning")
            return redirect(f"/partner/job/{booking_id}")
        schedule_date = row["preferred_date"] or row["booking_date"]
        schedule_time = row["preferred_time"] or row["booking_time"]
        if schedule_date and schedule_time:
            try:
                scheduled_at = datetime.fromisoformat(str(schedule_date).strip()[:10] + "T" + str(schedule_time).strip()[:5])
                now = india_now()
                if scheduled_at.tzinfo is None and now.tzinfo is not None:
                    scheduled_at = scheduled_at.replace(tzinfo=now.tzinfo)
                if now < scheduled_at + timedelta(hours=1):
                    conn.rollback()
                    flash("Start Job is available only after the scheduled time plus one hour.", "warning")
                    return redirect(f"/partner/job/{booking_id}")
            except (TypeError, ValueError):
                conn.rollback()
                flash("The customer's booking date/time is invalid.", "danger")
                return redirect(f"/partner/job/{booking_id}")
        due = _partner_booking_balance_due(row)
        paid_states = {"PAID", "SUCCESS", "DEMO_PAID", "COLLECTED", "COMPLETED"}
        if due > 0 and str(row["balance_payment_status"] or "").upper() not in paid_states:
            conn.rollback()
            flash(f"Customer must pay the remaining ₹{due:.2f} before the job can start.", "warning")
            return redirect(f"/partner/job/{booking_id}")
        conn.execute("UPDATE bookings SET booking_status='in_progress', status='in_progress', updated_at=CURRENT_TIMESTAMP WHERE id=?", (booking_id,))
        _partner_internal_notice(conn, worker["id"], partner_id, "Job Started by Partner", f"Booking {row['booking_id']} was started by your partner account.")
        conn.commit()
        flash("Job started successfully.", "success")
    except Exception as error:
        conn.rollback()
        print("PARTNER START ERROR:", error)
        flash("Job could not be started.", "danger")
    finally:
        conn.close()
    return redirect(f"/partner/job/{booking_id}")


@partner_bp.route("/partner/job/<int:booking_id>/complete", methods=["POST"])
@partner_required
def partner_complete_job(booking_id):
    partner_id = session["user_id"]
    conn = get_db()
    try:
        ensure_finance_schema(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        row, worker = _partner_booking_for_action(conn, booking_id, partner_id)
        if not row or not worker:
            conn.rollback()
            flash("Job or assigned worker was not found in your partner account.", "danger")
            return redirect("/partner/jobs")
        if str(row["current_status"] or "").lower() not in {"in_progress", "started"}:
            conn.rollback()
            flash("The job must be in progress before completion.", "warning")
            return redirect(f"/partner/job/{booking_id}")
        if not row["worker_arrived_at"]:
            conn.rollback()
            flash("Arrival has not been recorded.", "warning")
            return redirect(f"/partner/job/{booking_id}")
        due = _partner_booking_balance_due(row)
        paid_states = {"PAID", "SUCCESS", "DEMO_PAID", "COLLECTED", "COMPLETED"}
        advance_amount = finance_money(row["advance_amount"])
        advance_status = str(row["advance_payment_status"] or row["payment_status"] or "").upper()
        balance_status = str(row["balance_payment_status"] or "").upper()
        if (advance_amount > 0 and advance_status not in paid_states) or (due > 0 and balance_status not in paid_states):
            conn.rollback()
            flash("Advance and remaining balance must both be paid before completion.", "warning")
            return redirect(f"/partner/job/{booking_id}")
        conn.execute("UPDATE bookings SET booking_status='completed', status='completed', updated_at=CURRENT_TIMESTAMP WHERE id=?", (booking_id,))
        ok, message = settle_completed_booking(conn, booking_id)
        if not ok:
            conn.rollback()
            flash(message, "danger")
            return redirect(f"/partner/job/{booking_id}")
        _partner_internal_notice(conn, worker["id"], partner_id, "Job Completed", f"Booking {row['booking_id']} was completed by your partner account; settlement was recorded.")
        conn.commit()
        flash("Job completed and Worker/Partner/Sivdhi settlement recorded.", "success")
    except Exception as error:
        conn.rollback()
        print("PARTNER COMPLETE ERROR:", error)
        flash("Job could not be completed or settled.", "danger")
    finally:
        conn.close()
    return redirect(f"/partner/job/{booking_id}")


@partner_bp.route("/partner/job/<int:booking_id>/reject", methods=["POST"])
@partner_required
def reject_job(booking_id):
    """Reject a partner's assignment and immediately find the next eligible worker."""
    partner_id = session["user_id"]
    reason = (request.form.get("reason") or request.form.get("cancellation_reason") or "Rejected by partner").strip()
    reason = reason[:500] or "Rejected by partner"

    conn = get_db()
    try:
        ensure_assignment_monitor_schema(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        scope = _partner_booking_scope_sql()
        row = conn.execute(f"""
            SELECT b.*,
                   COALESCE(b.booking_status,b.status,'pending') AS current_status
            FROM bookings b
            WHERE b.id=? AND {scope}
            LIMIT 1
        """, (booking_id, partner_id, partner_id, partner_id)).fetchone()

        if not row:
            conn.rollback()
            flash("Job not found or not assigned to your partner account.", "danger")
            return redirect("/partner/jobs")

        current = str(row["current_status"] or "pending").lower()
        if current not in {"assigned", "pending"}:
            conn.rollback()
            flash("This booking is no longer waiting for assignment response.", "warning")
            return redirect(f"/partner/job/{booking_id}")

        old_worker_id = row["worker_id"] or row["assigned_worker_id"]
        result = reassign_booking(
            conn,
            row,
            old_worker_id,
            reason=f"Partner rejected assignment: {reason}",
            attempt_status="rejected",
        )
        conn.commit()

        if result["reassigned"]:
            flash("Job rejected. The next eligible worker and their partner have been notified.", "success")
        else:
            flash("Job rejected. No other eligible worker is available right now; the booking is waiting for another worker.", "warning")
        return redirect(f"/partner/job/{booking_id}")
    except Exception as error:
        conn.rollback()
        print("PARTNER REJECT ERROR:", error)
        flash("Unable to reject/reassign this job right now.", "danger")
        return redirect(f"/partner/job/{booking_id}")
    finally:
        conn.close()


@partner_bp.route("/partner/notifications", methods=["GET", "POST"])
@partner_required
def notifications():

    conn = get_db()
    cur = conn.cursor()

    if request.method == "POST":

        app_notification = 1 if request.form.get("app_notification") else 0
        email_notification = 1 if request.form.get("email_notification") else 0
        job_notification = 1 if request.form.get("job_notification") else 0
        withdraw_notification = 1 if request.form.get("withdraw_notification") else 0
        promo_notification = 1 if request.form.get("promo_notification") else 0

        cur.execute("""
        UPDATE partners
        SET
            app_notification=?,
            email_notification=?,
            job_notification=?,
            withdraw_notification=?,
            promo_notification=?
        WHERE partner_id=?
        """,(
            app_notification,
            email_notification,
            job_notification,
            withdraw_notification,
            promo_notification,
            session["user_id"]
        ))

        conn.commit()

        flash("Notification settings updated.","success")

        return redirect("/partner/notifications")

    cur.execute("""
    SELECT *
    FROM partners
    WHERE partner_id=?
    """,(session["user_id"],))

    partner = cur.fetchone()

    conn.close()

    return render_template(
        "partner/notifications.html",
        partner=partner
    )

# ======================================
# Partner notification inbox (separate from notification settings)
# Keep /partner/notifications and notifications.html unchanged.
# ======================================

@partner_bp.route("/partner/notification", methods=["GET"])
@partner_required
def notification_inbox():
    conn = get_db()
    try:
        open_id = request.args.get("open_id", type=int)
        if open_id:
            conn.execute("""
                UPDATE notifications
                SET is_read = 1
                WHERE id = ?
                  AND LOWER(COALESCE(user_type, '')) = 'partner'
                  AND CAST(user_id AS TEXT) = CAST(? AS TEXT)
            """, (open_id, session["user_id"]))
            conn.commit()

        notifications = conn.execute("""
            SELECT id, title, message, is_read, created_at
            FROM notifications
            WHERE LOWER(COALESCE(user_type, '')) = 'partner'
              AND CAST(user_id AS TEXT) = CAST(? AS TEXT)
            ORDER BY id DESC
            LIMIT 100
        """, (session["user_id"],)).fetchall()
        unread_count = conn.execute("""
            SELECT COUNT(*)
            FROM notifications
            WHERE LOWER(COALESCE(user_type, '')) = 'partner'
              AND CAST(user_id AS TEXT) = CAST(? AS TEXT)
              AND COALESCE(is_read, 0) = 0
        """, (session["user_id"],)).fetchone()[0] or 0
        return render_template(
            "partner/notification.html",
            notifications=notifications,
            unread_count=unread_count,
        )
    finally:
        conn.close()


@partner_bp.route("/partner/notification/<int:notification_id>/read", methods=["POST"])
@partner_required
def mark_partner_notification_read(notification_id):
    conn = get_db()
    try:
        cur = conn.execute("""
            UPDATE notifications
            SET is_read = 1
            WHERE id = ?
              AND LOWER(COALESCE(user_type, '')) = 'partner'
              AND CAST(user_id AS TEXT) = CAST(? AS TEXT)
        """, (notification_id, session["user_id"]))
        conn.commit()
        if cur.rowcount:
            flash("Notification marked as read.", "success")
        else:
            flash("Notification was not found.", "warning")
    except sqlite3.Error as error:
        conn.rollback()
        print("PARTNER NOTIFICATION READ ERROR:", error)
        flash("Could not update this notification.", "danger")
    finally:
        conn.close()
    return redirect("/partner/notification")


# ======================================
# Profile
# ======================================

@partner_bp.route("/partner/profile")
@partner_required
def profile():

    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        SELECT *
        FROM partners
        WHERE partner_id=?
    """,(session["user_id"],))

    partner = cur.fetchone()

    conn.close()

    return render_template(
        "partner/profile.html",
        partner=partner
    )


# ======================================
# Withdraw
# ======================================

@partner_bp.route("/partner/withdraw", methods=["GET","POST"])
@partner_required
def withdraw():

    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        SELECT *
        FROM partners
        WHERE partner_id=?
    """,(session["user_id"],))

    partner = cur.fetchone()

    if (
        not partner["bank_name"] or
        not partner["account_holder"] or
        not partner["account_number"] or
        not partner["ifsc"]
    ):

        flash(
            "Please add your bank details first.",
            "danger"
        )

        conn.close()

        return redirect("/partner/bank")

    if request.method == "POST":

        # Serialize wallet withdrawals so two simultaneous requests
        # cannot spend the same wallet balance.
        conn.execute("BEGIN IMMEDIATE")

        amount_text = request.form.get("amount", "").strip()

        try:
            amount = float(amount_text)
        except (ValueError, TypeError):
            flash(
                "Please enter a valid withdrawal amount.",
                "danger"
            )
            conn.close()
            return redirect("/partner/withdraw")
        wallet = float(partner["wallet"] or 0)

        try:
            setting_row = cur.execute(
                """
                SELECT setting_value
                FROM admin_settings
                WHERE setting_key = 'minimum_withdrawal'
                LIMIT 1
                """
            ).fetchone()
            minimum_withdrawal = float(
                setting_row["setting_value"]
            ) if setting_row and setting_row["setting_value"] else 0.0
        except (sqlite3.Error, TypeError, ValueError):
            minimum_withdrawal = 0.0

        if amount <= 0:

            flash(
                "Enter valid amount.",
                "danger"
            )

            conn.close()

            return redirect("/partner/withdraw")

        if minimum_withdrawal > 0 and amount < minimum_withdrawal:

            flash(
                f"Minimum withdrawal amount is ₹{minimum_withdrawal:.2f}.",
                "danger"
            )

            conn.rollback()
            conn.close()
            return redirect("/partner/withdraw")

        if amount > wallet:

            flash(
                "Insufficient wallet balance.",
                "danger"
            )

            conn.close()

            return redirect("/partner/withdraw")

        # Deduct only if the balance is still sufficient.
        # BEGIN IMMEDIATE above prevents another withdrawal from
        # changing this partner's balance concurrently.
        cur.execute("""
            UPDATE partners
            SET wallet = wallet - ?
            WHERE partner_id = ?
            AND wallet >= ?
        """,(
            amount,
            session["user_id"],
            amount
        ))

        if cur.rowcount != 1:
            conn.rollback()
            conn.close()

            flash(
                "Insufficient wallet balance.",
                "danger"
            )

            return redirect("/partner/withdraw")

        cur.execute("""
            INSERT INTO withdraw_requests
            (
                partner_id,
                amount,
                bank_name,
                account_holder,
                account_number,
                ifsc,
                status
            )
            VALUES(?,?,?,?,?,?,?)
        """,(
            session["user_id"],
            amount,
            partner["bank_name"],
            partner["account_holder"],
            partner["account_number"],
            partner["ifsc"],
            "Pending"
        ))

        # Record the withdrawal in the wallet ledger.
        # The amount is stored as a positive value and the
        # transaction type identifies it as a withdrawal/debit.
        cur.execute("""
            INSERT INTO wallet_transactions
            (
                user_type,
                user_id,
                amount,
                transaction_type,
                description
            )
            VALUES(?,?,?,?,?)
        """,(
            "partner",
            session["user_id"],
            amount,
            "withdraw",
            "Partner wallet withdrawal"
        ))

        conn.commit()
        conn.close()

        flash(
            "Withdraw request submitted successfully.",
            "success"
        )

        return redirect("/partner/wallet")

    conn.close()

    return render_template(
        "partner/withdraw.html",
        partner=partner
    )


# ======================================
# Wallet
# ======================================

@partner_bp.route("/partner/wallet")
@partner_required
def wallet():

    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        SELECT *
        FROM partners
        WHERE partner_id=?
    """,(session["user_id"],))

    partner = cur.fetchone()

    cur.execute("""
        SELECT *
        FROM withdraw_requests
        WHERE partner_id=?
        ORDER BY id DESC
    """,(session["user_id"],))

    withdraws = cur.fetchall()

    conn.close()

    return render_template(
        "partner/wallet.html",
        partner=partner,
        withdraws=withdraws
    )

# ======================================
# Edit Profile
# ======================================

@partner_bp.route("/partner/edit-profile", methods=["GET", "POST"])
@partner_required
def edit_profile():

    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        SELECT *
        FROM partners
        WHERE partner_id=?
    """,(session["user_id"],))

    partner = cur.fetchone()

    if request.method == "POST":

        fullname = request.form["fullname"]
        # Contact identity is immutable from Partner Edit Profile. Keep the
        # database values even if a crafted POST submits different values.
        email = partner["email"]
        mobile = partner["mobile"]
        address = request.form["address"]
        city = request.form["city"]
        state = request.form["state"]
        pincode = request.form["pincode"]

        photo = request.files.get("profile_photo")
        photo_name = partner["profile_photo"]

        if photo and photo.filename != "":

            valid_photo, photo_error = validate_uploaded_image(photo)
            if not valid_photo:
                flash(photo_error, "danger")
                conn.close()
                return redirect("/partner/edit-profile")

            os.makedirs(UPLOAD_FOLDER, exist_ok=True)

            original_filename = secure_filename(photo.filename)
            if not original_filename or "." not in original_filename:
                flash("Invalid image filename.", "danger")
                conn.close()
                return redirect("/partner/edit-profile")

            extension = original_filename.rsplit(".", 1)[1].lower()
            filename = (
                "partner_" + str(partner["id"]) + "_"
                + secrets.token_hex(8) + "." + extension
            )

            photo_path = os.path.join(UPLOAD_FOLDER, filename)
            try:
                photo.save(photo_path)
            except Exception as error:
                print("PARTNER PHOTO SAVE ERROR:", error)
                flash("Profile photo could not be uploaded.", "danger")
                conn.close()
                return redirect("/partner/edit-profile")

            photo_name = filename

        cur.execute("""
            UPDATE partners
            SET
                fullname=?,
                address=?,
                city=?,
                state=?,
                pincode=?,
                profile_photo=?
            WHERE partner_id=?
        """,(
            fullname,
            address,
            city,
            state,
            pincode,
            photo_name,
            session["user_id"]
        ))

        conn.commit()
        conn.close()

        flash(
            "Profile Updated Successfully.",
            "success"
        )

        return redirect("/partner/edit-profile")

    conn.close()

    return render_template(
        "partner/edit_profile.html",
        partner=partner
    )


# ======================================
# Settings
# ======================================

@partner_bp.route("/partner/settings")
@partner_required
def settings():

    return render_template(
        "partner/settings.html"
    )


# ======================================
# Bank
# ======================================

@partner_bp.route("/partner/bank", methods=["GET"])
@partner_required
def bank():
    user_id = session.get("user_id")
    if not user_id:
        return redirect("/login?role=partner")
    conn = get_db()
    try:
        partner = conn.execute(
            "SELECT * FROM partners WHERE partner_id=?", (user_id,)
        ).fetchone()
    finally:
        conn.close()
    if not partner:
        session.clear()
        return redirect("/login?role=partner")
    return render_template("partner/bank.html", partner=partner, indian_banks=INDIAN_BANKS)


# ======================================
# Change Bank
# ======================================

@partner_bp.route("/partner/change-bank", methods=["POST"])
@partner_required
def change_bank():

    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        SELECT email
        FROM partners
        WHERE partner_id=?
    """,(session["user_id"],))

    partner = cur.fetchone()
    if not partner:
        conn.close()
        session.clear()
        return redirect("/login?role=partner")

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

    valid, message = validate_bank_details(
        bank_name,
        account_holder,
        account_number,
        ifsc
    )

    if not valid:
        conn.close()

        flash(
            message,
            "danger"
        )

        return redirect("/partner/bank")

    conn.close()
    allowed, retry_after = reserve_otp_request(
        "partner_bank_change", session["user_id"], request.remote_addr or "unknown"
    )
    if not allowed:
        flash(f"Please wait {retry_after} seconds before requesting another OTP.", "warning")
        return redirect("/partner/bank")

    otp = str(secrets.randbelow(900000) + 100000)
    token = issue_pending_change(
        "partner", session["user_id"], "bank_change",
        {
            "bank_name": bank_name,
            "account_holder": account_holder,
            "account_number": account_number,
            "ifsc": ifsc,
        },
        otp,
    )
    for key in ("bank_otp", "bank_expiry", "new_bank", "bank_otp_attempts", "bank_otp_last_sent"):
        session.pop(key, None)
    session["pending_partner_bank_token"] = token

    try:
        send_otp(partner["email"], otp)
    except Exception:
        delete_pending_change("partner", session["user_id"], "bank_change", token)
        session.pop("pending_partner_bank_token", None)
        flash("OTP could not be sent. Please try again later.", "danger")
        return redirect("/partner/bank")

    flash("OTP sent to your email.", "success")
    return redirect("/partner/verify-bank-otp")

# ======================================
# Verify Bank OTP
# ======================================

@partner_bp.route("/partner/verify-bank-otp", methods=["GET","POST"])
@partner_required
def verify_bank_otp():
    user_id = session.get("user_id")
    token = session.get("pending_partner_bank_token")
    if not token or not user_id:
        flash("Please start the bank update again.", "warning")
        return redirect("/partner/bank")

    if request.method == "POST":
        user_otp = request.form.get("otp", "").strip()
        status, bank, attempts = verify_pending_change(
            "partner", user_id, "bank_change", token, user_otp
        )
        if status != "ok":
            if status in {"missing", "expired", "locked", "invalid_state"}:
                session.pop("pending_partner_bank_token", None)
                flash("OTP expired or too many attempts. Please start the bank update again.", "danger")
                return redirect("/partner/bank")
            flash(f"Invalid OTP. Attempt {attempts} of 5.", "danger")
            return redirect("/partner/verify-bank-otp")

        session.pop("pending_partner_bank_token", None)
        conn = get_db()
        try:
            cur = conn.execute(
                """UPDATE partners SET bank_name=?, account_holder=?, account_number=?, ifsc=?
                   WHERE partner_id=?""",
                (bank["bank_name"], bank["account_holder"], bank["account_number"], bank["ifsc"], user_id),
            )
            if cur.rowcount != 1:
                conn.rollback()
                flash("Partner account could not be found.", "danger")
                return redirect("/partner/bank")
            conn.commit()
        except Exception:
            conn.rollback()
            flash("Bank details could not be updated. Please try again.", "danger")
            return redirect("/partner/bank")
        finally:
            conn.close()

        delete_pending_change("partner", user_id, "bank_change", token)
        session.pop("pending_partner_bank_token", None)
        flash("Bank details updated successfully.", "success")
        return redirect("/partner/bank")

    return render_template("partner/verify_bank_otp.html")


# ======================================
# Resend Bank OTP
# ======================================

@partner_bp.route("/partner/resend-bank-otp", methods=["POST"])
@partner_required
def resend_bank_otp():
    user_id = session.get("user_id")
    token = session.get("pending_partner_bank_token")
    if not user_id or not token:
        flash("Please start the bank update again.", "warning")
        return redirect("/partner/bank")

    conn = get_db()
    try:
        partner = conn.execute("SELECT email FROM partners WHERE partner_id=?", (user_id,)).fetchone()
    finally:
        conn.close()
    if not partner:
        session.pop("pending_partner_bank_token", None)
        return redirect("/partner/bank")

    allowed, retry_after = reserve_otp_request(
        "partner_bank_change", user_id, request.remote_addr or "unknown"
    )
    if not allowed:
        flash(f"Please wait {retry_after} seconds before requesting another OTP.", "warning")
        return redirect("/partner/verify-bank-otp")

    otp = str(secrets.randbelow(900000) + 100000)
    status = refresh_pending_change("partner", user_id, "bank_change", token, otp)
    if status != "ok":
        session.pop("pending_partner_bank_token", None)
        flash("Bank update session expired. Please start again.", "warning")
        return redirect("/partner/bank")
    try:
        send_otp(partner["email"], otp)
    except Exception:
        delete_pending_change("partner", user_id, "bank_change", token)
        session.pop("pending_partner_bank_token", None)
        flash("OTP could not be sent. Please start the bank update again.", "danger")
        return redirect("/partner/bank")
    flash("New OTP sent successfully.", "success")
    return redirect("/partner/verify-bank-otp")


# ======================================
# Help
# ======================================

@partner_bp.route("/partner/help")
@partner_required
def help():

    return render_template("partner/help.html")


# ======================================
# FAQ
# ======================================

@partner_bp.route("/partner/faq")
@partner_required
def faq():

    return render_template("partner/faq.html")


# ======================================
# Privacy
# ======================================

@partner_bp.route("/partner/privacy")
@partner_required
def privacy():

    return render_template("partner/privacy.html")


# ======================================
# Terms
# ======================================

@partner_bp.route("/partner/terms")
@partner_required
def terms():

    return render_template("partner/terms.html")


# ======================================
# About Sivdhi
# ======================================

@partner_bp.route("/partner/about")
@partner_required
def about():

    return render_template("partner/about.html")



# ======================================
# Language
# ======================================

@partner_bp.route(
    "/partner/language",
    methods=["GET", "POST"]
)
@partner_required
def language():

    conn = get_db()
    cur = conn.cursor()

    if request.method == "POST":

        language = request.form["language"]

        cur.execute("""
        UPDATE partners
        SET language=?
        WHERE partner_id=?
        """, (
            language,
            session["user_id"]
        ))

        conn.commit()

        flash(
            "Language Updated Successfully.",
            "success"
        )

        conn.close()

        return redirect("/partner/language")

    cur.execute("""
    SELECT language
    FROM partners
    WHERE partner_id=?
    """, (session["user_id"],))

    partner = cur.fetchone()

    conn.close()

    return render_template(
        "partner/language.html",
        partner=partner
    )


# ======================================
# Change Password
# ======================================

@partner_bp.route("/partner/change-password", methods=["GET", "POST"])
@partner_required
def change_password():
    user_id = session.get("user_id")
    if not user_id:
        return redirect("/login?role=partner")

    conn = get_db()
    try:
        partner = conn.execute("SELECT * FROM partners WHERE partner_id=?", (user_id,)).fetchone()
    finally:
        conn.close()
    if not partner:
        session.clear()
        return redirect("/login?role=partner")

    if request.method == "POST":
        current_password = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")

        if not check_password_hash(partner["password"], current_password):
            flash("Current password is incorrect.", "danger")
            return redirect("/partner/change-password")
        if len(new_password) < 8 or not re.search(r"[A-Za-z]", new_password) or not re.search(r"\d", new_password):
            flash("New password must be at least 8 characters and include a letter and a number.", "danger")
            return redirect("/partner/change-password")
        if new_password != confirm_password:
            flash("Passwords do not match.", "danger")
            return redirect("/partner/change-password")

        allowed, retry_after = reserve_otp_request(
            "partner_password_change", user_id, request.remote_addr or "unknown"
        )
        if not allowed:
            flash(f"Please wait {retry_after} seconds before requesting another OTP.", "warning")
            return redirect("/partner/change-password")

        otp = str(secrets.randbelow(900000) + 100000)
        token = issue_pending_change(
            "partner", user_id, "password_change",
            {"password_hash": generate_password_hash(new_password)}, otp,
        )
        for key in ("password_otp", "password_expiry", "new_password", "password_otp_attempts", "password_otp_last_sent"):
            session.pop(key, None)
        session["pending_partner_password_token"] = token
        try:
            send_otp(partner["email"], otp)
        except Exception:
            delete_pending_change("partner", user_id, "password_change", token)
            session.pop("pending_partner_password_token", None)
            flash("OTP could not be sent. Please try again later.", "danger")
            return redirect("/partner/change-password")
        flash("OTP sent to your email.", "success")
        return redirect("/partner/verify-password-otp")

    return render_template("partner/change_password.html")

# ======================================
# Verify Change Password OTP
# ======================================

@partner_bp.route("/partner/verify-password-otp", methods=["GET", "POST"])
@partner_required
def verify_password_otp():
    user_id = session.get("user_id")
    token = session.get("pending_partner_password_token")
    if not user_id or not token:
        flash("Please start the password change again.", "warning")
        return redirect("/partner/change-password")

    if request.method == "POST":
        user_otp = request.form.get("otp", "").strip()
        status, payload, attempts = verify_pending_change(
            "partner", user_id, "password_change", token, user_otp
        )
        if status != "ok":
            if status in {"missing", "expired", "locked", "invalid_state"}:
                session.pop("pending_partner_password_token", None)
                flash("OTP expired or too many attempts. Please start again.", "danger")
                return redirect("/partner/change-password")
            flash(f"Invalid OTP. Attempt {attempts} of 5.", "danger")
            return redirect("/partner/verify-password-otp")

        session.pop("pending_partner_password_token", None)
        password_hash = payload.get("password_hash")
        if not password_hash:
            delete_pending_change("partner", user_id, "password_change", token)
            session.pop("pending_partner_password_token", None)
            flash("Password change session expired. Please try again.", "danger")
            return redirect("/partner/change-password")

        conn = get_db()
        try:
            cur = conn.execute("UPDATE partners SET password=? WHERE partner_id=?", (password_hash, user_id))
            if cur.rowcount != 1:
                conn.rollback()
                flash("Partner account could not be found.", "danger")
                return redirect("/partner/change-password")
            conn.commit()
        except Exception:
            conn.rollback()
            flash("Password could not be changed. Please try again.", "danger")
            return redirect("/partner/change-password")
        finally:
            conn.close()

        delete_pending_change("partner", user_id, "password_change", token)
        session.pop("pending_partner_password_token", None)
        flash("Password changed successfully.", "success")
        return redirect("/partner/settings")

    return render_template("partner/verify_password_otp.html")

# ======================================
# Resend Change Password OTP
# ======================================

@partner_bp.route("/partner/resend-password-otp", methods=["POST"])
@partner_required
def resend_password_otp():
    user_id = session.get("user_id")
    token = session.get("pending_partner_password_token")
    if not user_id or not token:
        flash("Please start the password change again.", "warning")
        return redirect("/partner/change-password")

    conn = get_db()
    try:
        partner = conn.execute("SELECT email FROM partners WHERE partner_id=?", (user_id,)).fetchone()
    finally:
        conn.close()
    if not partner:
        session.pop("pending_partner_password_token", None)
        return redirect("/partner/change-password")

    allowed, retry_after = reserve_otp_request(
        "partner_password_change", user_id, request.remote_addr or "unknown"
    )
    if not allowed:
        flash(f"Please wait {retry_after} seconds before requesting another OTP.", "warning")
        return redirect("/partner/verify-password-otp")

    otp = str(secrets.randbelow(900000) + 100000)
    status = refresh_pending_change("partner", user_id, "password_change", token, otp)
    if status != "ok":
        session.pop("pending_partner_password_token", None)
        flash("Password change session expired. Please start again.", "warning")
        return redirect("/partner/change-password")
    try:
        send_otp(partner["email"], otp)
    except Exception:
        delete_pending_change("partner", user_id, "password_change", token)
        session.pop("pending_partner_password_token", None)
        flash("OTP could not be sent. Please start again.", "danger")
        return redirect("/partner/change-password")
    flash("New OTP sent successfully.", "success")
    return redirect("/partner/verify-password-otp")

# ======================================
# Rate App
# ======================================

@partner_bp.route(
    "/partner/rate-app",
    methods=["GET", "POST"]
)
@partner_required
def rate_app():

    conn = get_db()
    cur = conn.cursor()

    if request.method == "POST":

        rating = request.form["rating"]
        feedback = request.form.get("feedback", "").strip()

        cur.execute("""
        INSERT INTO app_ratings(
            partner_id,
            rating,
            feedback
        )
        VALUES(?,?,?)
        """, (
            session["user_id"],
            rating,
            feedback
        ))

        conn.commit()
        conn.close()

        flash(
            "Thank you for your feedback ❤️",
            "success"
        )

        return redirect("/partner/rate-app")

    conn.close()

    return render_template(
        "partner/rate_app.html"
    )

# ======================================
# Check for Update
# ======================================

@partner_bp.route("/partner/check-update")
@partner_required
def check_update():

    current_version = "1.0.0"
    latest_version = "1.0.0"

    if current_version == latest_version:
        update_available = False
    else:
        update_available = True

    return render_template(
        "partner/check_update.html",
        current_version=current_version,
        latest_version=latest_version,
        update_available=update_available
    )


# ======================================
# App Version
# ======================================

@partner_bp.route("/partner/app-version")
@partner_required
def app_version():

    version = "1.0.0"

    return render_template(
        "partner/app_version.html",
        version=version
    )