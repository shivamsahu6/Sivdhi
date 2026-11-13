import sqlite3
import os
import re
import time

from flask import (
    Blueprint,
    render_template,
    request,
    redirect,
    flash,
    session,
    url_for,
)

from werkzeug.security import check_password_hash

from database import get_db
from routes.decorators import admin_required
from utils.sivdhi_finance import ensure_finance_schema, money as finance_money, add_finance_ledger_entry
from utils.assignment_monitor import ensure_assignment_monitor_schema
from utils.login_security import is_login_allowed, record_login_failure, clear_login_failures
from utils.admin_mfa import (
    ADMIN_MFA_PENDING_SECONDS,
    complete_admin_mfa_enrollment,
    consume_admin_recovery_code,
    ensure_admin_security_record,
    get_admin_security_state,
    get_or_create_pending_totp_secret,
    is_admin_authorized,
    totp_provisioning_uri,
    verify_and_consume_totp,
)


# A non-public route prefix adds a discovery barrier, but authentication and
# authorization remain the actual security boundary. Production must configure
# a unique 32+ character path in the server environment.
_ADMIN_PATH_RE = re.compile(r"^[A-Za-z0-9_-]{32,96}$")
ADMIN_PANEL_PATH = os.getenv("ADMIN_PANEL_PATH", "").strip().strip("/")
_APP_ENV = os.getenv("APP_ENV", os.getenv("FLASK_ENV", "development")).strip().lower()
if (
    not _ADMIN_PATH_RE.fullmatch(ADMIN_PANEL_PATH)
    or ADMIN_PANEL_PATH.lower().startswith(("replace-", "change-me"))
):
    if _APP_ENV == "production":
        raise RuntimeError(
            "Production requires ADMIN_PANEL_PATH: 32-96 random letters, numbers, _ or -."
        )
    # Development-only fallback. Never use this value on a public server.
    ADMIN_PANEL_PATH = "dev-only-admin-entry-do-not-deploy"
ADMIN_PANEL_BASE = "/" + ADMIN_PANEL_PATH

ADMIN_IDLE_TIMEOUT_SECONDS = 30 * 60
ADMIN_ABSOLUTE_TIMEOUT_SECONDS = 8 * 60 * 60

admin_bp = Blueprint("admin", __name__, url_prefix=ADMIN_PANEL_BASE)


def _admin_session_is_expired(now, login_at, last_seen_at):
    try:
        login_at = int(login_at)
        last_seen_at = int(last_seen_at)
    except (TypeError, ValueError):
        return True
    return (
        login_at <= 0
        or last_seen_at <= 0
        or now - last_seen_at > ADMIN_IDLE_TIMEOUT_SECONDS
        or now - login_at > ADMIN_ABSOLUTE_TIMEOUT_SECONDS
        or last_seen_at < login_at
    )


@admin_bp.before_request
def _enforce_admin_session_policy():
    """Apply short website-only Admin sessions to every Admin endpoint."""
    if session.get("role") != "admin":
        return None

    now = int(time.time())
    if _admin_session_is_expired(
        now,
        session.get("admin_login_at"),
        session.get("admin_last_seen_at"),
    ) or not is_admin_authorized(session.get("admin_id")):
        session.clear()
        flash("Admin session expired or access was revoked. Please sign in again.", "danger")
        return redirect(url_for("admin.admin_login"))

    session["admin_last_seen_at"] = now
    return None


# =========================================================
# COMMON HELPERS
# =========================================================

def _ensure_admin_catalog_schema(conn):
    """Add missing catalog columns without deleting existing records."""
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS service_categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            price REAL NOT NULL DEFAULT 0,
            commission REAL NOT NULL DEFAULT 0,
            icon TEXT DEFAULT '✦',
            description TEXT DEFAULT '',
            is_active INTEGER NOT NULL DEFAULT 1,
            normal_enabled INTEGER NOT NULL DEFAULT 1,
            full_day_enabled INTEGER NOT NULL DEFAULT 0,
            full_day_price REAL NOT NULL DEFAULT 0,
            visit_fee REAL NOT NULL DEFAULT 0,
            included_work TEXT DEFAULT '',
            excluded_work TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS services (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            icon TEXT DEFAULT '✦',
            description TEXT DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1,
            status TEXT DEFAULT 'active',
            category TEXT DEFAULT '',
            price REAL NOT NULL DEFAULT 0,
            commission REAL NOT NULL DEFAULT 0,
            full_day_price REAL NOT NULL DEFAULT 0,
            visit_fee REAL NOT NULL DEFAULT 0,
            included_work TEXT DEFAULT '',
            excluded_work TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS customer_service_problems (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            service_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            description TEXT DEFAULT '',
            price REAL NOT NULL DEFAULT 0,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(service_id, name)
        )
    """)

    def columns(table):
        return {
            row[1]
            for row in cur.execute(
                f"PRAGMA table_info({table})"
            ).fetchall()
        }

    additions = {
        "service_categories": {
            "price": "REAL NOT NULL DEFAULT 0",
            "commission": "REAL NOT NULL DEFAULT 0",
            "icon": "TEXT DEFAULT '✦'",
            "description": "TEXT DEFAULT ''",
            "is_active": "INTEGER NOT NULL DEFAULT 1",
            "normal_enabled": "INTEGER NOT NULL DEFAULT 1",
            "full_day_enabled": "INTEGER NOT NULL DEFAULT 0",
            "full_day_price": "REAL NOT NULL DEFAULT 0",
            "visit_fee": "REAL NOT NULL DEFAULT 0",
            "included_work": "TEXT DEFAULT ''",
            "excluded_work": "TEXT DEFAULT ''",
        },
        "services": {
            "icon": "TEXT DEFAULT '✦'",
            "description": "TEXT DEFAULT ''",
            "active": "INTEGER NOT NULL DEFAULT 1",
            "status": "TEXT DEFAULT 'active'",
            "category": "TEXT DEFAULT ''",
            "price": "REAL NOT NULL DEFAULT 0",
            "commission": "REAL NOT NULL DEFAULT 0",
            "partner_commission_percent": "REAL NOT NULL DEFAULT 10",
            "platform_commission_percent": "REAL NOT NULL DEFAULT 10",
            "full_day_price": "REAL NOT NULL DEFAULT 0",
            "visit_fee": "REAL NOT NULL DEFAULT 0",
            "included_work": "TEXT DEFAULT ''",
            "excluded_work": "TEXT DEFAULT ''",
        },
        "customer_service_problems": {
            "description": "TEXT DEFAULT ''",
            "price": "REAL NOT NULL DEFAULT 0",
            "is_active": "INTEGER NOT NULL DEFAULT 1",
            "created_at": "TEXT",
        },
    }

    for table, table_additions in additions.items():
        existing = columns(table)

        for column, definition in table_additions.items():
            if column not in existing:
                cur.execute(
                    f"ALTER TABLE {table} ADD COLUMN "
                    f"{column} {definition}"
                )

    conn.commit()


def _admin_catalog_money(raw, field_label, default=0.0):
    raw = "" if raw is None else str(raw).strip()

    if raw == "":
        return default

    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"Enter a valid {field_label}.")

    if value < 0:
        raise ValueError(
            f"{field_label.capitalize()} cannot be negative."
        )

    return round(value, 2)


def _catalog_redirect(tab="category", extra=""):
    url = ADMIN_PANEL_BASE + "/admin/settings"

    if extra:
        url += "?" + extra

    return redirect(f"{url}#{tab}")


# =========================================================
# ADMIN LOGIN
# =========================================================

def _admin_mfa_pending_context():
    admin_id = session.get("admin_mfa_pending_id")
    started = session.get("admin_mfa_pending_at")
    try:
        started = int(started)
    except (TypeError, ValueError):
        started = 0

    if not admin_id or started <= 0 or int(time.time()) - started > ADMIN_MFA_PENDING_SECONDS:
        session.clear()
        flash("Your verification session expired. Please sign in again.", "danger")
        return None, None

    state = get_admin_security_state(str(admin_id))
    if not state or not int(state["is_active"] or 0):
        session.clear()
        flash("Admin access is unavailable. Please contact the site operator.", "danger")
        return None, None

    return str(admin_id), state


def _start_admin_mfa_pending(admin_id):
    session.clear()
    session.permanent = True
    session["admin_mfa_pending_id"] = str(admin_id)
    session["admin_mfa_pending_at"] = int(time.time())


def _establish_admin_session(admin_id):
    """Create the full Admin website session only after successful MFA."""
    if not is_admin_authorized(str(admin_id)):
        session.clear()
        return False

    conn = get_db()
    try:
        admin = conn.execute(
            "SELECT admin_id, fullname FROM admin WHERE admin_id=? LIMIT 1",
            (str(admin_id),),
        ).fetchone()
    finally:
        conn.close()

    if not admin:
        session.clear()
        return False

    session.clear()
    session.permanent = True
    session["admin_id"] = admin["admin_id"]
    session["admin_name"] = admin["fullname"]
    session["role"] = "admin"
    now = int(time.time())
    session["admin_login_at"] = now
    session["admin_last_seen_at"] = now
    return True


def _record_admin_mfa_failure(admin_id, client_ip):
    record_login_failure("admin_mfa", str(admin_id), client_ip)


@admin_bp.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if session.get("role") == "admin":
        return redirect(ADMIN_PANEL_BASE + "/admin/dashboard")

    pending_id = session.get("admin_mfa_pending_id")
    if pending_id:
        checked_id, state = _admin_mfa_pending_context()
        if checked_id and state:
            target = "setup" if not int(state["totp_enabled"] or 0) else "verify"
            return redirect(ADMIN_PANEL_BASE + "/admin/mfa/" + target)

    if request.method == "POST":
        admin_id = request.form.get("admin_id", "").strip()
        password = request.form.get("password", "")

        if not admin_id or not password:
            flash("Please enter Admin ID and Password.", "danger")
            return redirect(ADMIN_PANEL_BASE + "/admin/login")

        client_ip = request.remote_addr or "unknown"
        if not is_login_allowed("admin", admin_id, client_ip):
            flash("Too many failed login attempts. Please try again later.", "danger")
            return redirect(ADMIN_PANEL_BASE + "/admin/login")

        conn = get_db()
        try:
            admin = conn.execute(
                "SELECT admin_id, fullname, password FROM admin WHERE admin_id=? LIMIT 1",
                (admin_id,),
            ).fetchone()
        finally:
            conn.close()

        password_valid = False
        if admin:
            try:
                password_valid = check_password_hash(admin["password"], password)
            except Exception:
                password_valid = False

        if not password_valid:
            record_login_failure("admin", admin_id, client_ip)
            flash("Invalid Admin ID or Password.", "danger")
            return redirect(ADMIN_PANEL_BASE + "/admin/login")

        state = ensure_admin_security_record(admin["admin_id"])
        if not state or not int(state["is_active"] or 0):
            record_login_failure("admin", admin_id, client_ip)
            flash("Invalid Admin ID or Password.", "danger")
            return redirect(ADMIN_PANEL_BASE + "/admin/login")

        _start_admin_mfa_pending(admin["admin_id"])
        if not int(state["totp_enabled"] or 0):
            return redirect(ADMIN_PANEL_BASE + "/admin/mfa/setup")
        return redirect(ADMIN_PANEL_BASE + "/admin/mfa/verify")

    return render_template("admin/login.html")


@admin_bp.route("/admin/mfa/setup", methods=["GET", "POST"])
def admin_mfa_setup():
    # A browser refresh/back-navigation after enrollment must not clear the
    # newly established full Admin session. Recovery codes were rendered once.
    if session.get("role") == "admin":
        return redirect(ADMIN_PANEL_BASE + "/admin/dashboard")

    admin_id, state = _admin_mfa_pending_context()
    if not admin_id:
        return redirect(ADMIN_PANEL_BASE + "/admin/login")
    if int(state["totp_enabled"] or 0):
        return redirect(ADMIN_PANEL_BASE + "/admin/mfa/verify")

    secret = get_or_create_pending_totp_secret(admin_id)
    conn = get_db()
    try:
        row = conn.execute("SELECT email FROM admin WHERE admin_id=? LIMIT 1", (admin_id,)).fetchone()
    finally:
        conn.close()
    provisioning_uri = totp_provisioning_uri(admin_id, row["email"] if row else admin_id, secret)

    if request.method == "POST":
        client_ip = request.remote_addr or "unknown"
        if not is_login_allowed("admin_mfa", admin_id, client_ip):
            session.clear()
            flash("Too many verification attempts. Please try again after 15 minutes.", "danger")
            return redirect(ADMIN_PANEL_BASE + "/admin/login")

        recovery_codes = complete_admin_mfa_enrollment(admin_id, request.form.get("code", ""))
        if not recovery_codes:
            _record_admin_mfa_failure(admin_id, client_ip)
            flash("The verification code is incorrect or expired. Try the current 6-digit code.", "danger")
            return render_template("admin/mfa_setup.html", secret=secret, provisioning_uri=provisioning_uri)

        clear_login_failures("admin", admin_id, client_ip)
        clear_login_failures("admin_mfa", admin_id, client_ip)
        if not _establish_admin_session(admin_id):
            session.clear()
            flash("Admin access could not be established. Please sign in again.", "danger")
            return redirect(ADMIN_PANEL_BASE + "/admin/login")
        return render_template("admin/mfa_recovery_codes.html", recovery_codes=recovery_codes)

    return render_template("admin/mfa_setup.html", secret=secret, provisioning_uri=provisioning_uri)


@admin_bp.route("/admin/mfa/verify", methods=["GET", "POST"])
def admin_mfa_verify():
    # Keep an authenticated session intact if the browser revisits this URL.
    if session.get("role") == "admin":
        return redirect(ADMIN_PANEL_BASE + "/admin/dashboard")

    admin_id, state = _admin_mfa_pending_context()
    if not admin_id:
        return redirect(ADMIN_PANEL_BASE + "/admin/login")
    if not int(state["totp_enabled"] or 0):
        return redirect(ADMIN_PANEL_BASE + "/admin/mfa/setup")

    if request.method == "POST":
        client_ip = request.remote_addr or "unknown"
        if not is_login_allowed("admin_mfa", admin_id, client_ip):
            session.clear()
            flash("Too many verification attempts. Please try again after 15 minutes.", "danger")
            return redirect(ADMIN_PANEL_BASE + "/admin/login")

        supplied_code = request.form.get("code", "").strip()
        verified = verify_and_consume_totp(admin_id, supplied_code)
        if not verified:
            verified = consume_admin_recovery_code(admin_id, supplied_code)

        if not verified:
            _record_admin_mfa_failure(admin_id, client_ip)
            flash("Invalid or already-used verification code.", "danger")
            return render_template("admin/mfa_verify.html")

        clear_login_failures("admin", admin_id, client_ip)
        clear_login_failures("admin_mfa", admin_id, client_ip)
        if not _establish_admin_session(admin_id):
            session.clear()
            flash("Admin access could not be established. Please sign in again.", "danger")
            return redirect(ADMIN_PANEL_BASE + "/admin/login")

        flash("Admin verification successful.", "success")
        return redirect(ADMIN_PANEL_BASE + "/admin/dashboard")

    return render_template("admin/mfa_verify.html")


# =========================================================
# ADMIN GLOBAL SEARCH
# =========================================================

@admin_bp.route("/admin/search")
@admin_required
def admin_search():

    # Bound user-controlled search size to avoid oversized LIKE scans.
    q = (request.args.get("q", "") or "").strip()[:100]

    if len(q) < 2:
        return {"results": []}

    like = f"%{q}%"
    conn = get_db()

    try:
        workers = conn.execute("""
            SELECT id, worker_id AS public_id,
                   fullname AS name, mobile, 'Worker' AS type
            FROM workers
            WHERE is_deleted = 0
              AND (
                  fullname LIKE ?
                  OR worker_id LIKE ?
                  OR mobile LIKE ?
                  OR email LIKE ?
              )
            ORDER BY id DESC LIMIT 5
        """, (like, like, like, like)).fetchall()

        partners = conn.execute("""
            SELECT id, partner_id AS public_id,
                   fullname AS name, mobile, 'Partner' AS type
            FROM partners
            WHERE is_deleted = 0
              AND (
                  fullname LIKE ?
                  OR partner_id LIKE ?
                  OR mobile LIKE ?
                  OR email LIKE ?
              )
            ORDER BY id DESC LIMIT 5
        """, (like, like, like, like)).fetchall()

        jobs = conn.execute("""
            SELECT id, booking_id AS public_id,
                   category AS name, 'Job' AS type
            FROM bookings
            WHERE booking_id LIKE ?
               OR category LIKE ?
               OR address LIKE ?
            ORDER BY id DESC LIMIT 5
        """, (like, like, like)).fetchall()

        results = []

        for row in workers:
            results.append({
                "type": row["type"],
                "name": row["name"],
                "id": row["public_id"],
                "url": f"{ADMIN_PANEL_BASE}/admin/worker/{row['id']}",
            })

        for row in partners:
            results.append({
                "type": row["type"],
                "name": row["name"],
                "id": row["public_id"],
                "url": f"{ADMIN_PANEL_BASE}/admin/partner/{row['id']}",
            })

        for row in jobs:
            results.append({
                "type": row["type"],
                "name": row["name"] or "Job",
                "id": row["public_id"],
                "url": f"{ADMIN_PANEL_BASE}/admin/job/{row['id']}",
            })

        return {"results": results[:12]}

    finally:
        conn.close()


# =========================================================
# CUSTOMER ACCOUNT MANAGEMENT SCHEMA
# =========================================================

def _ensure_customer_admin_schema(conn):
    """Add admin-managed account fields without deleting customer data."""
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(customers)").fetchall()
    }
    if not columns:
        return
    if "status" not in columns:
        conn.execute("ALTER TABLE customers ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")
    if "is_deleted" not in columns:
        conn.execute("ALTER TABLE customers ADD COLUMN is_deleted INTEGER NOT NULL DEFAULT 0")
    conn.execute("UPDATE customers SET status='active' WHERE status IS NULL OR TRIM(status)=''")
    conn.execute("UPDATE customers SET is_deleted=0 WHERE is_deleted IS NULL")


# =========================================================
# DASHBOARD
# =========================================================

@admin_bp.route("/admin/dashboard")
@admin_required
def admin_dashboard():

    conn = get_db()

    try:
        total_workers = conn.execute("""
            SELECT COUNT(*) FROM workers WHERE is_deleted = 0
        """).fetchone()[0]

        total_partners = conn.execute("""
            SELECT COUNT(*) FROM partners WHERE is_deleted = 0
        """).fetchone()[0]

        # Customer management columns are migrated safely for older databases.
        _ensure_customer_admin_schema(conn)
        total_customers = conn.execute("""
            SELECT COUNT(*) FROM customers WHERE is_deleted = 0
        """).fetchone()[0]

        total_jobs = conn.execute("""
            SELECT COUNT(*) FROM bookings
        """).fetchone()[0]

        total_withdrawals = conn.execute("""
            SELECT COUNT(*) FROM withdraw_requests
            WHERE LOWER(status) = 'pending'
        """).fetchone()[0]

        total_job_requests = conn.execute("""
            SELECT COUNT(*) FROM bookings
            WHERE LOWER(COALESCE(booking_status, status, 'pending'))
                  = 'pending'
        """).fetchone()[0]

        total_notifications = conn.execute("""
            SELECT COUNT(*) FROM notifications
        """).fetchone()[0]

        # The dashboard bell must reflect unread database notifications,
        # rather than showing a decorative dot unconditionally.
        unread_notifications = conn.execute("""
            SELECT COUNT(*) FROM notifications
            WHERE COALESCE(is_read, 0) = 0
        """).fetchone()[0]

        deleted_workers = conn.execute("""
            SELECT COUNT(*) FROM workers WHERE is_deleted = 1
        """).fetchone()[0]

        deleted_partners = conn.execute("""
            SELECT COUNT(*) FROM partners WHERE is_deleted = 1
        """).fetchone()[0]

        deleted_customers = conn.execute("""
            SELECT COUNT(*) FROM customers WHERE is_deleted = 1
        """).fetchone()[0]

    finally:
        conn.close()

    return render_template(
        "admin/dashboard.html",
        total_workers=total_workers,
        total_partners=total_partners,
        total_customers=total_customers,
        total_jobs=total_jobs,
        total_withdrawals=total_withdrawals,
        total_job_requests=total_job_requests,
        total_notifications=total_notifications,
        unread_notifications=unread_notifications,
        deleted_workers=deleted_workers,
        deleted_partners=deleted_partners,
        deleted_customers=deleted_customers,
        deleted_accounts=deleted_workers + deleted_partners + deleted_customers,
    )


# =========================================================
# PARTNERS
# =========================================================

@admin_bp.route("/admin/partners")
@admin_required
def admin_partners():

    conn = get_db()

    try:
        partners = conn.execute("""
            SELECT *
            FROM partners
            WHERE is_deleted = 0
            ORDER BY id DESC
        """).fetchall()
    finally:
        conn.close()

    return render_template(
        "admin/partners.html",
        partners=partners,
    )


@admin_bp.route("/admin/partner/<int:partner_id>")
@admin_required
def admin_partner_details(partner_id):

    conn = get_db()

    try:
        partner = conn.execute("""
            SELECT * FROM partners WHERE id = ?
        """, (partner_id,)).fetchone()
    finally:
        conn.close()

    if not partner:
        flash("Partner not found.", "danger")
        return redirect(ADMIN_PANEL_BASE + "/admin/partners")

    return render_template(
        "admin/partner_details.html",
        partner=partner,
    )


@admin_bp.route("/admin/partner/<int:id>/suspend", methods=["POST"])
@admin_required
def suspend_partner(id):

    conn = get_db()

    try:
        conn.execute(
            "UPDATE partners SET status = 'suspended' WHERE id = ?",
            (id,),
        )
        conn.commit()
    finally:
        conn.close()

    flash("Partner suspended successfully.", "success")
    return redirect(f"{ADMIN_PANEL_BASE}/admin/partner/{id}")


@admin_bp.route("/admin/partner/<int:id>/activate", methods=["POST"])
@admin_required
def activate_partner(id):

    conn = get_db()

    try:
        conn.execute(
            "UPDATE partners SET status = 'active' WHERE id = ?",
            (id,),
        )
        conn.commit()
    finally:
        conn.close()

    flash("Partner activated successfully.", "success")
    return redirect(f"{ADMIN_PANEL_BASE}/admin/partner/{id}")


@admin_bp.route("/admin/partner/delete/<partner_id>", methods=["POST"])
@admin_required
def delete_partner(partner_id):

    conn = get_db()

    try:
        conn.execute("""
            UPDATE partners
            SET is_deleted = 1
            WHERE partner_id = ?
        """, (partner_id,))
        conn.commit()
    finally:
        conn.close()

    flash("Partner moved to Deleted Accounts.", "success")
    return redirect(ADMIN_PANEL_BASE + "/admin/partners")


# =========================================================
# WORKERS
# =========================================================

@admin_bp.route("/admin/workers")
@admin_required
def admin_workers():

    conn = get_db()

    try:
        workers = conn.execute("""
            SELECT *
            FROM workers
            WHERE is_deleted = 0
            ORDER BY id DESC
        """).fetchall()
    finally:
        conn.close()

    return render_template("admin/workers.html", workers=workers)


@admin_bp.route("/admin/worker/<int:worker_id>")
@admin_required
def admin_worker_details(worker_id):

    conn = get_db()

    try:
        worker = conn.execute("""
            SELECT * FROM workers WHERE id = ?
        """, (worker_id,)).fetchone()
    finally:
        conn.close()

    if not worker:
        flash("Worker not found.", "danger")
        return redirect(ADMIN_PANEL_BASE + "/admin/workers")

    return render_template(
        "admin/worker_details.html",
        worker=worker,
    )


@admin_bp.route(
    "/admin/worker/<int:worker_id>/suspend",
    methods=["POST"],
)
@admin_required
def suspend_worker(worker_id):

    conn = get_db()

    try:
        conn.execute("""
            UPDATE workers SET status = 'suspended'
            WHERE id = ?
        """, (worker_id,))
        conn.commit()
    finally:
        conn.close()

    flash("Worker account suspended successfully.", "success")
    return redirect(f"{ADMIN_PANEL_BASE}/admin/worker/{worker_id}")


@admin_bp.route(
    "/admin/worker/<int:worker_id>/activate",
    methods=["POST"],
)
@admin_required
def activate_worker(worker_id):

    conn = get_db()

    try:
        conn.execute("""
            UPDATE workers SET status = 'active'
            WHERE id = ?
        """, (worker_id,))
        conn.commit()
    finally:
        conn.close()

    flash("Worker account activated successfully.", "success")
    return redirect(f"{ADMIN_PANEL_BASE}/admin/worker/{worker_id}")


@admin_bp.route("/admin/worker/delete/<worker_id>", methods=["POST"])
@admin_required
def delete_worker(worker_id):

    conn = get_db()

    try:
        conn.execute("""
            UPDATE workers SET is_deleted = 1
            WHERE worker_id = ?
        """, (worker_id,))
        conn.commit()
    finally:
        conn.close()

    flash("Worker moved to Deleted Accounts.", "success")
    return redirect(ADMIN_PANEL_BASE + "/admin/workers")


# =========================================================
# DELETED ACCOUNTS
# =========================================================

@admin_bp.route("/admin/deleted-accounts")
@admin_required
def deleted_accounts():

    conn = get_db()

    try:
        deleted_workers = conn.execute("""
            SELECT * FROM workers
            WHERE is_deleted = 1
            ORDER BY id DESC
        """).fetchall()

        deleted_partners = conn.execute("""
            SELECT * FROM partners
            WHERE is_deleted = 1
            ORDER BY id DESC
        """).fetchall()

        _ensure_customer_admin_schema(conn)
        deleted_customers = conn.execute("""
            SELECT * FROM customers
            WHERE is_deleted = 1
            ORDER BY id DESC
        """).fetchall()

    finally:
        conn.close()

    return render_template(
        "admin/deleted_accounts.html",
        deleted_workers=deleted_workers,
        deleted_partners=deleted_partners,
        deleted_customers=deleted_customers,
    )


@admin_bp.route(
    "/admin/customer/<int:customer_id>/restore",
    methods=["POST"],
)
@admin_required
def restore_customer(customer_id):
    conn = get_db()
    try:
        _ensure_customer_admin_schema(conn)
        cur = conn.execute("UPDATE customers SET is_deleted=0 WHERE id=? AND is_deleted=1", (customer_id,))
        conn.commit()
    finally:
        conn.close()
    flash("Customer account restored." if cur.rowcount else "Deleted customer not found.", "success" if cur.rowcount else "danger")
    return redirect(ADMIN_PANEL_BASE + "/admin/deleted-accounts")


@admin_bp.route(
    "/admin/customer/<int:customer_id>/permanent-delete",
    methods=["POST"],
)
@admin_required
def permanent_delete_customer(customer_id):
    conn = get_db()
    try:
        _ensure_customer_admin_schema(conn)
        # Preserve customer booking history: never hard-delete a customer with bookings.
        booking_columns = {row[1] for row in conn.execute("PRAGMA table_info(bookings)").fetchall()}
        if "customer_id" in booking_columns:
            related_booking = conn.execute(
                "SELECT 1 FROM bookings WHERE CAST(customer_id AS TEXT)=CAST(? AS TEXT) LIMIT 1",
                (customer_id,),
            ).fetchone()
            if related_booking:
                flash("This customer has booking history. The account remains in Deleted Accounts to preserve records.", "danger")
                return redirect(ADMIN_PANEL_BASE + "/admin/deleted-accounts")
        cur = conn.execute("DELETE FROM customers WHERE id=? AND is_deleted=1", (customer_id,))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        flash("Customer has related records and cannot be permanently deleted. Keep it in Deleted Accounts instead.", "danger")
        return redirect(ADMIN_PANEL_BASE + "/admin/deleted-accounts")
    finally:
        conn.close()
    flash("Customer permanently deleted." if cur.rowcount else "Deleted customer not found.", "success" if cur.rowcount else "danger")
    return redirect(ADMIN_PANEL_BASE + "/admin/deleted-accounts")


@admin_bp.route(
    "/admin/worker/<int:worker_id>/restore",
    methods=["POST"],
)
@admin_required
def restore_worker(worker_id):

    conn = get_db()

    try:
        conn.execute("""
            UPDATE workers SET is_deleted = 0 WHERE id = ?
        """, (worker_id,))
        conn.commit()
    finally:
        conn.close()

    flash("Worker account restored successfully.", "success")
    return redirect(ADMIN_PANEL_BASE + "/admin/deleted-accounts")


@admin_bp.route(
    "/admin/partner/<int:partner_id>/restore",
    methods=["POST"],
)
@admin_required
def restore_partner(partner_id):

    conn = get_db()

    try:
        conn.execute("""
            UPDATE partners SET is_deleted = 0 WHERE id = ?
        """, (partner_id,))
        conn.commit()
    finally:
        conn.close()

    flash("Partner account restored successfully.", "success")
    return redirect(ADMIN_PANEL_BASE + "/admin/deleted-accounts")


@admin_bp.route(
    "/admin/worker/<int:worker_id>/permanent-delete",
    methods=["POST"],
)
@admin_required
def permanent_delete_worker(worker_id):

    conn = get_db()

    try:
        conn.execute("""
            DELETE FROM workers
            WHERE id = ? AND is_deleted = 1
        """, (worker_id,))
        conn.commit()
    finally:
        conn.close()

    flash("Worker permanently deleted.", "success")
    return redirect(ADMIN_PANEL_BASE + "/admin/deleted-accounts")


@admin_bp.route(
    "/admin/partner/<int:partner_id>/permanent-delete",
    methods=["POST"],
)
@admin_required
def permanent_delete_partner(partner_id):

    conn = get_db()

    try:
        conn.execute("""
            DELETE FROM partners
            WHERE id = ? AND is_deleted = 1
        """, (partner_id,))
        conn.commit()
    finally:
        conn.close()

    flash("Partner permanently deleted.", "success")
    return redirect(ADMIN_PANEL_BASE + "/admin/deleted-accounts")


# =========================================================
# JOBS AND JOB DETAILS
# =========================================================

@admin_bp.route("/admin/jobs")
@admin_required
def admin_jobs():

    conn = get_db()

    try:
        jobs = conn.execute("""
            SELECT
                b.id,
                b.booking_id,
                COALESCE(b.category, b.service, 'Job') AS title,
                b.address AS location,
                COALESCE(b.total_amount, b.amount, 0) AS budget,
                COALESCE(
                    NULLIF(b.booking_status, ''),
                    NULLIF(b.status, ''),
                    'pending'
                ) AS status,
                b.partner_id,
                b.assigned_partner_id,
                b.worker_id,
                b.assigned_worker_id,
                p.fullname AS partner_name,
                p.partner_id AS partner_public_id,
                w.fullname AS worker_name,
                w.worker_id AS worker_public_id
            FROM bookings b
            LEFT JOIN workers w
              ON CAST(w.id AS TEXT) =
                 CAST(COALESCE(b.worker_id, b.assigned_worker_id) AS TEXT)
            LEFT JOIN partners p
              ON CAST(p.partner_id AS TEXT) =
                 CAST(COALESCE(b.assigned_partner_id, b.partner_id) AS TEXT)
            ORDER BY b.id DESC
        """).fetchall()
    finally:
        conn.close()

    return render_template("admin/jobs.html", jobs=jobs)


@admin_bp.route("/admin/job/<int:job_id>")
@admin_required
def admin_job_details(job_id):

    conn = get_db()

    try:
        job = conn.execute("""
            SELECT
                b.id,
                b.booking_id,
                b.category AS title,
                b.description,
                b.address,
                b.city,
                b.state,
                b.pincode,
                b.total_amount AS amount,
                b.total_amount,
                COALESCE(
                    NULLIF(b.booking_status, ''),
                    NULLIF(b.status, ''),
                    'pending'
                ) AS status,
                b.payment_status,
                b.created_at,
                b.partner_id,
                b.assigned_partner_id,
                b.worker_id,
                b.assigned_worker_id,
                p.fullname AS partner_name,
                p.partner_id AS partner_public_id,
                w.fullname AS worker_name,
                w.worker_id AS worker_public_id
            FROM bookings b
            LEFT JOIN partners p
              ON CAST(p.partner_id AS TEXT) =
                 CAST(COALESCE(b.assigned_partner_id, b.partner_id) AS TEXT)
            LEFT JOIN workers w
              ON CAST(w.id AS TEXT) =
                 CAST(COALESCE(b.worker_id, b.assigned_worker_id) AS TEXT)
            WHERE b.id = ?
        """, (job_id,)).fetchone()
    finally:
        conn.close()

    if not job:
        flash("Job not found.", "danger")
        return redirect(ADMIN_PANEL_BASE + "/admin/jobs")

    return render_template("admin/job_details.html", job=job)


@admin_bp.route("/admin/job-requests")
@admin_required
def admin_job_requests():

    conn = get_db()

    try:
        requests_list = conn.execute("""
            SELECT
                'booking' AS source,
                b.id,
                b.booking_id AS job_id,
                COALESCE(b.partner_id, b.assigned_partner_id) AS partner_id,
                COALESCE(w.worker_id, CAST(b.assigned_worker_id AS TEXT)) AS worker_id,
                COALESCE(
                    NULLIF(b.booking_status, ''),
                    NULLIF(b.status, ''),
                    'pending'
                ) AS request_status,
                b.created_at,
                COALESCE(b.category, b.service, 'Job Request') AS job_service,
                COALESCE(b.total_amount, b.amount, 0) AS job_amount,
                p.fullname AS partner_name,
                w.fullname AS worker_name
            FROM bookings b
            LEFT JOIN partners p
              ON CAST(p.partner_id AS TEXT) =
                 CAST(COALESCE(b.assigned_partner_id, b.partner_id) AS TEXT)
            LEFT JOIN workers w
              ON CAST(w.id AS TEXT) =
                 CAST(COALESCE(b.worker_id, b.assigned_worker_id) AS TEXT)
            WHERE LOWER(COALESCE(b.booking_status, b.status, 'pending'))
                  IN ('pending', 'assigned', 'accepted')

            UNION ALL

            SELECT
                'legacy' AS source,
                jr.id,
                jr.job_id,
                jr.partner_id,
                jr.worker_id,
                jr.request_status,
                jr.created_at,
                COALESCE(j.service, 'Job Request') AS job_service,
                COALESCE(j.amount, 0) AS job_amount,
                p.fullname AS partner_name,
                w.fullname AS worker_name
            FROM job_requests jr
            LEFT JOIN jobs j
              ON CAST(j.job_id AS TEXT) = CAST(jr.job_id AS TEXT)
            LEFT JOIN partners p
              ON CAST(p.partner_id AS TEXT) = CAST(jr.partner_id AS TEXT)
            LEFT JOIN workers w
              ON CAST(w.worker_id AS TEXT) = CAST(jr.worker_id AS TEXT)
            ORDER BY 7 DESC
        """).fetchall()
    finally:
        conn.close()

    return render_template(
        "admin/job_requests.html",
        requests=requests_list,
        page_title="Job Requests",
    )


# =========================================================
# WALLET
# =========================================================

@admin_bp.route("/admin/wallet")
@admin_required
def admin_wallet():

    conn = get_db()

    try:
        worker_wallet = conn.execute("""
            SELECT COALESCE(SUM(wallet), 0)
            FROM workers
            WHERE is_deleted = 0
        """).fetchone()[0] or 0

        partner_wallet = conn.execute("""
            SELECT COALESCE(SUM(wallet), 0)
            FROM partners
            WHERE is_deleted = 0
        """).fetchone()[0] or 0

        total_withdrawn = conn.execute("""
            SELECT COALESCE(SUM(amount), 0)
            FROM withdraw_requests
            WHERE LOWER(status) = 'completed'
        """).fetchone()[0] or 0

        try:
            platform_commission_earned = conn.execute("""
                SELECT COALESCE(SUM(amount), 0)
                FROM wallet_transactions
                WHERE LOWER(COALESCE(user_type,''))='platform'
                  AND LOWER(COALESCE(transaction_type,''))='platform_commission'
            """).fetchone()[0] or 0
        except sqlite3.Error:
            platform_commission_earned = 0

        try:
            transactions = conn.execute("""
                SELECT id, user_id, user_type, transaction_type,
                       amount, description, created_at
                FROM wallet_transactions
                ORDER BY id DESC
            """).fetchall()
        except sqlite3.Error:
            transactions = []

    finally:
        conn.close()

    return render_template(
        "admin/wallet.html",
        total_wallet=worker_wallet + partner_wallet,
        worker_wallet=worker_wallet,
        partner_wallet=partner_wallet,
        total_withdrawn=total_withdrawn,
        platform_commission_earned=platform_commission_earned,
        transactions=transactions,
    )


# =========================================================
# CUSTOMERS
# =========================================================

@admin_bp.route("/admin/customers")
@admin_required
def admin_customers():
    conn = get_db()
    try:
        _ensure_customer_admin_schema(conn)
        conn.commit()
        customers = conn.execute("""
            SELECT * FROM customers
            WHERE COALESCE(is_deleted, 0) = 0
            ORDER BY id DESC
        """).fetchall()
    finally:
        conn.close()
    return render_template("admin/customers.html", customers=customers)


@admin_bp.route("/admin/customer/<int:customer_id>")
@admin_required
def admin_customer_details(customer_id):
    conn = get_db()
    try:
        _ensure_customer_admin_schema(conn)
        conn.commit()
        customer = conn.execute("""
            SELECT * FROM customers WHERE id = ? AND COALESCE(is_deleted, 0) = 0
        """, (customer_id,)).fetchone()
    finally:
        conn.close()
    if not customer:
        flash("Customer not found or moved to Deleted Accounts.", "danger")
        return redirect(ADMIN_PANEL_BASE + "/admin/customers")
    return render_template("admin/customer_details.html", customer=customer)


@admin_bp.route("/admin/customer/<int:customer_id>/suspend", methods=["POST"])
@admin_required
def suspend_customer(customer_id):
    conn = get_db()
    try:
        _ensure_customer_admin_schema(conn)
        cur = conn.execute("""
            UPDATE customers SET status='suspended'
            WHERE id=? AND COALESCE(is_deleted, 0)=0
        """, (customer_id,))
        conn.commit()
    finally:
        conn.close()
    flash("Customer account suspended." if cur.rowcount else "Customer not found.", "success" if cur.rowcount else "danger")
    return redirect(f"{ADMIN_PANEL_BASE}/admin/customer/{customer_id}" if cur.rowcount else ADMIN_PANEL_BASE + "/admin/customers")


@admin_bp.route("/admin/customer/<int:customer_id>/activate", methods=["POST"])
@admin_required
def activate_customer(customer_id):
    conn = get_db()
    try:
        _ensure_customer_admin_schema(conn)
        cur = conn.execute("""
            UPDATE customers SET status='active'
            WHERE id=? AND COALESCE(is_deleted, 0)=0
        """, (customer_id,))
        conn.commit()
    finally:
        conn.close()
    flash("Customer account activated." if cur.rowcount else "Customer not found.", "success" if cur.rowcount else "danger")
    return redirect(f"{ADMIN_PANEL_BASE}/admin/customer/{customer_id}" if cur.rowcount else ADMIN_PANEL_BASE + "/admin/customers")


@admin_bp.route("/admin/customer/delete/<int:customer_id>", methods=["POST"])
@admin_required
def delete_customer(customer_id):
    conn = get_db()
    try:
        _ensure_customer_admin_schema(conn)
        cur = conn.execute("""
            UPDATE customers SET is_deleted=1
            WHERE id=? AND COALESCE(is_deleted, 0)=0
        """, (customer_id,))
        conn.commit()
    finally:
        conn.close()
    flash("Customer moved to Deleted Accounts." if cur.rowcount else "Customer not found.", "success" if cur.rowcount else "danger")
    return redirect(ADMIN_PANEL_BASE + "/admin/customers")


# =========================================================
# BOOKINGS
# =========================================================

@admin_bp.route("/admin/bookings")
@admin_required
def admin_bookings():

    conn = get_db()

    try:
        bookings = conn.execute("""
            SELECT
                b.*,
                c.fullname AS customer_name,
                c.mobile AS customer_mobile,
                s.name AS service_name
            FROM bookings b
            LEFT JOIN customers c ON b.customer_id = c.id
            LEFT JOIN services s ON b.service_id = s.id
            ORDER BY b.id DESC
        """).fetchall()
    finally:
        conn.close()

    return render_template(
        "admin/bookings.html",
        bookings=bookings,
    )


@admin_bp.route("/admin/booking/<int:booking_id>")
@admin_required
def admin_booking_details(booking_id):

    conn = get_db()

    refund_paid_total = 0.0
    try:
        ensure_finance_schema(conn)
        conn.commit()
        booking = conn.execute("""
            SELECT
                b.*,
                c.fullname AS customer_name,
                c.mobile AS customer_mobile,
                c.email AS customer_email,
                s.name AS service_name
            FROM bookings b
            LEFT JOIN customers c ON b.customer_id = c.id
            LEFT JOIN services s ON b.service_id = s.id
            WHERE b.id = ?
        """, (booking_id,)).fetchone()
        if booking:
            refund_paid_total = finance_money(booking["advance_paid_amount"]) + finance_money(booking["balance_paid_amount"])
            if refund_paid_total <= 0:
                try:
                    row = conn.execute("""
                        SELECT COALESCE(SUM(amount),0) FROM customer_payment_records
                        WHERE CAST(booking_id AS TEXT)=CAST(? AS TEXT)
                          AND UPPER(COALESCE(status,'')) IN ('PAID','SUCCESS','DEMO_PAID','COLLECTED','COMPLETED')
                    """, (booking["booking_id"],)).fetchone()
                    refund_paid_total = finance_money(row[0] if row else 0)
                except sqlite3.Error:
                    pass
            if refund_paid_total <= 0 and "payment_id" in booking.keys() and booking["payment_id"]:
                try:
                    row = conn.execute("""
                        SELECT amount FROM payments WHERE transaction_id=?
                          AND UPPER(COALESCE(status,'')) IN ('PAID','SUCCESS','DEMO_PAID','COLLECTED','COMPLETED')
                        ORDER BY id DESC LIMIT 1
                    """, (str(booking["payment_id"]),)).fetchone()
                    refund_paid_total = finance_money(row[0] if row else 0)
                except sqlite3.Error:
                    pass
    finally:
        conn.close()

    if not booking:
        flash("Booking not found.", "danger")
        return redirect(ADMIN_PANEL_BASE + "/admin/bookings")

    return render_template(
        "admin/booking_details.html",
        booking=booking,
        refund_paid_total=refund_paid_total,
    )


@admin_bp.route(
    "/admin/booking/<int:booking_id>/status",
    methods=["POST"],
)
@admin_required
def admin_update_booking_status(booking_id):
    status = request.form.get("status", "").strip().lower()
    allowed_statuses = {
        "pending", "confirmed", "assigned", "in_progress", "completed", "cancelled",
    }
    if status not in allowed_statuses:
        flash("Invalid booking status.", "danger")
        return redirect(f"{ADMIN_PANEL_BASE}/admin/booking/{booking_id}")

    reason = (request.form.get("cancellation_reason") or "Cancelled by Sivdhi Admin").strip()[:500]
    conn = get_db()
    try:
        ensure_finance_schema(conn)
        ensure_assignment_monitor_schema(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        booking = conn.execute("""
            SELECT booking_id, customer_id, worker_id, assigned_worker_id,
                   partner_id, assigned_partner_id, advance_paid_amount,
                   balance_paid_amount,
                   COALESCE(NULLIF(booking_status,''), NULLIF(status,''), 'pending') AS old_status
            FROM bookings WHERE id=? LIMIT 1
        """, (booking_id,)).fetchone()
        if not booking:
            conn.rollback()
            flash("Booking not found.", "danger")
            return redirect(ADMIN_PANEL_BASE + "/admin/bookings")

        old_status = str(booking["old_status"] or "").lower()
        cancellation_paid_amount = (
            finance_money(booking["advance_paid_amount"])
            + finance_money(booking["balance_paid_amount"])
        )
        conn.execute("""
            UPDATE bookings
            SET status=?, booking_status=?,
                cancellation_reason=CASE WHEN ?='cancelled' THEN ? ELSE cancellation_reason END,
                refund_status=CASE WHEN ?='cancelled' AND ?>0 THEN 'pending_admin' WHEN ?='cancelled' THEN 'not_required' ELSE refund_status END,
                refund_amount=CASE WHEN ?='cancelled' THEN ? ELSE refund_amount END,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=?
        """, (
            status, status, status, reason,
            status, cancellation_paid_amount, status,
            status, cancellation_paid_amount, booking_id,
        ))

        internal_recipients = []
        worker_id = booking["worker_id"] or booking["assigned_worker_id"]
        partner_id = booking["assigned_partner_id"] or booking["partner_id"]
        if worker_id:
            internal_recipients.append(("worker", str(worker_id)))
        if partner_id:
            internal_recipients.append(("partner", str(partner_id)))

        for user_type, user_id in internal_recipients:
            conn.execute("""
                INSERT INTO notifications(user_type,user_id,title,message,is_read,created_at)
                VALUES(?,?,?,?,0,CURRENT_TIMESTAMP)
            """, (
                user_type, user_id,
                "Booking Cancelled by Admin" if status == "cancelled" else "Booking Status Updated",
                f"Booking {booking['booking_id']} status is now {status.replace('_',' ').title()}.",
            ))

        # Customer receives only the cancellation notice here; assignment and
        # intermediate lifecycle events remain internal to worker/partner.
        if status == "cancelled" and old_status not in {"cancelled", "canceled"} and booking["customer_id"]:
            conn.execute("""
                INSERT INTO notifications(user_type,user_id,title,message,is_read,created_at)
                VALUES('customer',?,?,?,0,CURRENT_TIMESTAMP)
            """, (
                str(booking["customer_id"]), "Booking Cancelled",
                f"Your Sivdhi booking {booking['booking_id']} has been cancelled. {reason}"
                " If you have paid an amount, Admin will record the bank refund after the transfer is completed.",
            ))

        conn.commit()
        flash("Booking status updated successfully.", "success")
    except Exception as error:
        conn.rollback()
        print("ADMIN BOOKING STATUS ERROR:", error)
        flash("Booking status could not be updated.", "danger")
    finally:
        conn.close()
    return redirect(f"{ADMIN_PANEL_BASE}/admin/booking/{booking_id}")


@admin_bp.route("/admin/booking/<int:booking_id>/refund", methods=["POST"])
@admin_required
def admin_process_booking_refund(booking_id):
    """Record a manual bank refund only after Admin has sent the money."""
    reference = (request.form.get("refund_reference") or "").strip()[:120]
    conn = get_db()
    try:
        ensure_finance_schema(conn)
        ensure_assignment_monitor_schema(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        booking = conn.execute("SELECT * FROM bookings WHERE id=? LIMIT 1", (booking_id,)).fetchone()
        if not booking:
            conn.rollback()
            flash("Booking not found.", "danger")
            return redirect(ADMIN_PANEL_BASE + "/admin/bookings")
        cashfree_order = conn.execute("""
            SELECT order_id FROM cashfree_payment_orders
            WHERE CAST(booking_id AS TEXT)=CAST(? AS TEXT)
            LIMIT 1
        """, (str(booking["booking_id"]),)).fetchone()
        if cashfree_order:
            conn.rollback()
            flash(
                "This booking was paid through Cashfree. Use the Cashfree Refunds page "
                "to record the refund after processing it in the Cashfree Dashboard.",
                "warning",
            )
            return redirect(f"{ADMIN_PANEL_BASE}/admin/booking/{booking_id}")

        booking_status = str(booking["booking_status"] or booking["status"] or "").lower()
        if booking_status not in {"cancelled", "canceled"}:
            conn.rollback()
            flash("Cancel the booking before recording its refund.", "warning")
            return redirect(f"{ADMIN_PANEL_BASE}/admin/booking/{booking_id}")
        if str(booking["refund_status"] or "").lower() == "refunded":
            conn.rollback()
            flash("This booking's refund has already been recorded.", "info")
            return redirect(f"{ADMIN_PANEL_BASE}/admin/booking/{booking_id}")

        refund_amount = finance_money(booking["advance_paid_amount"]) + finance_money(booking["balance_paid_amount"])
        if refund_amount <= 0:
            # Support older payment rows whose totals were saved only in the
            # payment-record table; include successful payments only.
            try:
                payment_total = conn.execute("""
                    SELECT COALESCE(SUM(amount),0) FROM customer_payment_records
                    WHERE CAST(booking_id AS TEXT)=CAST(? AS TEXT)
                      AND UPPER(COALESCE(status,'')) IN ('PAID','SUCCESS','DEMO_PAID','COLLECTED','COMPLETED')
                """, (booking["booking_id"],)).fetchone()
                refund_amount = finance_money(payment_total[0] if payment_total else 0)
            except sqlite3.Error:
                refund_amount = 0.0
        if refund_amount <= 0 and "payment_id" in booking.keys() and booking["payment_id"]:
            try:
                legacy_payment = conn.execute("""
                    SELECT amount FROM payments
                    WHERE transaction_id=?
                      AND UPPER(COALESCE(status,'')) IN ('PAID','SUCCESS','DEMO_PAID','COLLECTED','COMPLETED')
                    ORDER BY id DESC LIMIT 1
                """, (str(booking["payment_id"]),)).fetchone()
                refund_amount = finance_money(legacy_payment[0] if legacy_payment else 0)
            except sqlite3.Error:
                refund_amount = 0.0
        refund_amount = finance_money(refund_amount)
        if refund_amount <= 0:
            conn.rollback()
            flash("No successfully paid amount was found. There is nothing to refund.", "warning")
            return redirect(f"{ADMIN_PANEL_BASE}/admin/booking/{booking_id}")

        conn.execute("""
            UPDATE bookings
            SET refund_status='refunded', refund_amount=?, refund_reference=?,
                refund_processed_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
            WHERE id=? AND LOWER(COALESCE(refund_status,'not_required'))<>'refunded'
        """, (refund_amount, reference or None, booking_id))
        add_finance_ledger_entry(
            conn,
            booking["booking_id"],
            "customer_refund",
            refund_amount,
            "manual_recorded",
            "manual_bank_transfer",
            reference or None,
            "Admin recorded the manual customer bank refund after transferring funds.",
        )
        if booking["customer_id"]:
            message = (
                f"Your refund of ₹{refund_amount:.2f} for cancelled booking {booking['booking_id']} "
                "has been recorded by Sivdhi Admin as a completed bank transfer."
            )
            if reference:
                message += f" Bank transaction reference: {reference}."
            conn.execute("""
                INSERT INTO notifications(user_type,user_id,title,message,is_read,created_at)
                VALUES('customer',?,?,?,0,CURRENT_TIMESTAMP)
            """, (str(booking["customer_id"]), "Refund Processed", message))
        conn.commit()
        flash(f"Manual refund of ₹{refund_amount:.2f} recorded. Customer notification and email have been queued.", "success")
    except Exception as error:
        conn.rollback()
        print("ADMIN BOOKING REFUND ERROR:", error)
        flash("Refund could not be recorded. No refund status was changed.", "danger")
    finally:
        conn.close()
    return redirect(f"{ADMIN_PANEL_BASE}/admin/booking/{booking_id}")


# =========================================================
# CASHFREE DASHBOARD REFUND RECONCILIATION
# =========================================================

@admin_bp.route("/admin/cashfree-refunds")
@admin_required
def admin_cashfree_refunds():
    """Show Cashfree payments that need manual refund reconciliation."""
    conn = get_db()
    try:
        ensure_finance_schema(conn)
        conn.commit()
        orders = conn.execute("""
            SELECT
                o.order_id, o.customer_id, o.amount, o.currency, o.status,
                o.environment, o.cf_payment_id, o.payment_method, o.booking_id,
                o.payment_type, o.created_at,
                c.fullname AS customer_name,
                c.email AS customer_email,
                b.status AS booking_status,
                b.booking_status AS booking_lifecycle,
                (SELECT r.status FROM cashfree_manual_refunds r
                 WHERE r.order_id=o.order_id ORDER BY r.id DESC LIMIT 1) AS latest_refund_status,
                (SELECT r.refund_id FROM cashfree_manual_refunds r
                 WHERE r.order_id=o.order_id ORDER BY r.id DESC LIMIT 1) AS latest_refund_id
            FROM cashfree_payment_orders o
            LEFT JOIN customers c ON c.id=o.customer_id
            LEFT JOIN bookings b
              ON CAST(b.booking_id AS TEXT)=CAST(o.booking_id AS TEXT)
            WHERE o.status='PAID_NO_WORKER'
               OR (
                    o.status='SUCCESS'
                    AND o.booking_id IS NULL
               )
               OR (
                    o.status='BOOKED'
                    AND LOWER(COALESCE(NULLIF(b.booking_status,''), b.status, ''))
                        IN ('cancelled','canceled')
               )
               OR EXISTS (
                    SELECT 1 FROM cashfree_manual_refunds r
                    WHERE r.order_id=o.order_id
               )
            ORDER BY o.created_at DESC
            LIMIT 300
        """).fetchall()
        refunds = conn.execute("""
            SELECT r.*, o.environment, c.fullname AS customer_name
            FROM cashfree_manual_refunds r
            JOIN cashfree_payment_orders o ON o.order_id=r.order_id
            LEFT JOIN customers c ON c.id=o.customer_id
            ORDER BY r.id DESC
            LIMIT 300
        """).fetchall()
    finally:
        conn.close()

    return render_template(
        "admin/cashfree_refunds.html",
        orders=orders,
        refunds=refunds,
        page_title="Cashfree Refunds",
    )


@admin_bp.route(
    "/admin/cashfree-refunds/<order_id>/record",
    methods=["POST"],
)
@admin_required
def admin_record_cashfree_refund(order_id):
    """Record a refund already initiated manually in the Cashfree Dashboard."""
    refund_id = (request.form.get("refund_id") or "").strip()[:120]
    submitted_environment = (request.form.get("environment") or "").strip().lower()
    reason = (request.form.get("reason") or "").strip()[:500]

    if not refund_id:
        flash("Enter the Cashfree refund ID shown in the Dashboard.", "danger")
        return redirect(ADMIN_PANEL_BASE + "/admin/cashfree-refunds")

    conn = get_db()
    try:
        ensure_finance_schema(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        order = conn.execute("""
            SELECT * FROM cashfree_payment_orders
            WHERE order_id=? LIMIT 1
        """, (order_id,)).fetchone()
        if not order:
            conn.rollback()
            flash("Cashfree order not found.", "danger")
            return redirect(ADMIN_PANEL_BASE + "/admin/cashfree-refunds")

        if order["status"] not in {"PAID_NO_WORKER", "SUCCESS", "BOOKED"}:
            conn.rollback()
            flash("This Cashfree order is not eligible for manual refund reconciliation.", "warning")
            return redirect(ADMIN_PANEL_BASE + "/admin/cashfree-refunds")

        if order["status"] == "SUCCESS" and order["booking_id"]:
            conn.rollback()
            flash("This successful Cashfree order is already linked to a booking. Use the booking refund workflow.", "warning")
            return redirect(ADMIN_PANEL_BASE + "/admin/cashfree-refunds")

        if order["status"] == "BOOKED":
            booking = conn.execute("""
                SELECT status, booking_status FROM bookings
                WHERE CAST(booking_id AS TEXT)=CAST(? AS TEXT)
                LIMIT 1
            """, (str(order["booking_id"] or ""),)).fetchone()
            lifecycle = str(
                (booking["booking_status"] or booking["status"] or "") if booking else ""
            ).strip().lower()
            if lifecycle not in {"cancelled", "canceled"}:
                conn.rollback()
                flash("Cancel the booking before recording its Cashfree refund.", "warning")
                return redirect(ADMIN_PANEL_BASE + "/admin/cashfree-refunds")

        if not order["cf_payment_id"]:
            conn.rollback()
            flash("Verified Cashfree payment ID is missing. Do not initiate a refund until it is reconciled.", "danger")
            return redirect(ADMIN_PANEL_BASE + "/admin/cashfree-refunds")

        refund_amount = finance_money(order["amount"])
        if refund_amount <= 0:
            conn.rollback()
            flash("The Cashfree order amount is invalid.", "danger")
            return redirect(ADMIN_PANEL_BASE + "/admin/cashfree-refunds")

        stored_environment = str(order["environment"] or "unknown").strip().lower()
        if stored_environment == "unknown":
            if submitted_environment not in {"sandbox", "production"}:
                conn.rollback()
                flash("Confirm whether this order belongs to Cashfree Sandbox or Production.", "danger")
                return redirect(ADMIN_PANEL_BASE + "/admin/cashfree-refunds")
            conn.execute("""
                UPDATE cashfree_payment_orders SET environment=?, updated_at=CURRENT_TIMESTAMP
                WHERE order_id=? AND environment='unknown'
            """, (submitted_environment, order_id))
            stored_environment = submitted_environment
        elif submitted_environment != stored_environment:
            conn.rollback()
            flash("Selected environment does not match the environment stored for this order.", "danger")
            return redirect(ADMIN_PANEL_BASE + "/admin/cashfree-refunds")

        latest = conn.execute("""
            SELECT status FROM cashfree_manual_refunds
            WHERE order_id=? ORDER BY id DESC LIMIT 1
        """, (order_id,)).fetchone()
        if latest and str(latest["status"]).upper() in {"INITIATED", "SUCCESS"}:
            conn.rollback()
            flash("A refund is already initiated or completed for this Cashfree order.", "warning")
            return redirect(ADMIN_PANEL_BASE + "/admin/cashfree-refunds")

        duplicate = conn.execute("""
            SELECT 1 FROM cashfree_manual_refunds WHERE refund_id=? LIMIT 1
        """, (refund_id,)).fetchone()
        if duplicate:
            conn.rollback()
            flash("This Cashfree refund ID is already recorded.", "danger")
            return redirect(ADMIN_PANEL_BASE + "/admin/cashfree-refunds")

        conn.execute("""
            INSERT INTO cashfree_manual_refunds
                (order_id, refund_id, amount, status, reason, admin_id)
            VALUES (?, ?, ?, 'INITIATED', ?, ?)
        """, (
            order_id, refund_id, refund_amount, reason or None,
            str(session.get("admin_id") or ""),
        ))
        # Lock a verified-but-unbooked order while its external refund is
        # pending, so the customer cannot consume the same payment as a booking.
        if order["status"] == "SUCCESS":
            cur = conn.execute("""
                UPDATE cashfree_payment_orders
                SET status='REFUND_PENDING', updated_at=CURRENT_TIMESTAMP
                WHERE order_id=? AND status='SUCCESS' AND booking_id IS NULL
            """, (order_id,))
            if cur.rowcount != 1:
                conn.rollback()
                flash("This payment changed while the refund was being recorded. Refresh and verify its state.", "warning")
                return redirect(ADMIN_PANEL_BASE + "/admin/cashfree-refunds")
        conn.commit()
        flash(
            f"Cashfree refund {refund_id} recorded as initiated. "
            "Mark it successful only after Cashfree Dashboard confirms success.",
            "success",
        )
    except Exception as error:
        conn.rollback()
        print("ADMIN CASHFREE REFUND RECORD ERROR:", error)
        flash("Cashfree refund reference could not be recorded.", "danger")
    finally:
        conn.close()

    return redirect(ADMIN_PANEL_BASE + "/admin/cashfree-refunds")


@admin_bp.route(
    "/admin/cashfree-refunds/<int:refund_attempt_id>/status",
    methods=["POST"],
)
@admin_required
def admin_update_cashfree_refund_status(refund_attempt_id):
    """Reconcile an initiated Cashfree Dashboard refund after checking its status."""
    new_status = (request.form.get("status") or "").strip().upper()
    if new_status not in {"SUCCESS", "FAILED"}:
        flash("Choose a valid Cashfree refund status.", "danger")
        return redirect(ADMIN_PANEL_BASE + "/admin/cashfree-refunds")

    conn = get_db()
    try:
        ensure_finance_schema(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        refund = conn.execute("""
            SELECT r.*, o.customer_id, o.environment, o.booking_id, o.status AS order_status
            FROM cashfree_manual_refunds r
            JOIN cashfree_payment_orders o ON o.order_id=r.order_id
            WHERE r.id=? LIMIT 1
        """, (refund_attempt_id,)).fetchone()
        if not refund:
            conn.rollback()
            flash("Cashfree refund record not found.", "danger")
            return redirect(ADMIN_PANEL_BASE + "/admin/cashfree-refunds")

        if str(refund["status"] or "").upper() != "INITIATED":
            conn.rollback()
            flash("Only an initiated refund can be reconciled. Duplicate status changes are blocked.", "warning")
            return redirect(ADMIN_PANEL_BASE + "/admin/cashfree-refunds")

        cur = conn.execute("""
            UPDATE cashfree_manual_refunds
            SET status=?, updated_at=CURRENT_TIMESTAMP,
                completed_at=CASE WHEN ?='SUCCESS' THEN CURRENT_TIMESTAMP ELSE NULL END
            WHERE id=? AND status='INITIATED'
        """, (new_status, new_status, refund_attempt_id))
        if cur.rowcount != 1:
            conn.rollback()
            flash("Refund status changed in another session. Refresh and verify it.", "warning")
            return redirect(ADMIN_PANEL_BASE + "/admin/cashfree-refunds")

        if refund["order_status"] == "REFUND_PENDING":
            restored_order_status = "REFUNDED" if new_status == "SUCCESS" else "SUCCESS"
            conn.execute("""
                UPDATE cashfree_payment_orders
                SET status=?, updated_at=CURRENT_TIMESTAMP
                WHERE order_id=? AND status='REFUND_PENDING'
            """, (restored_order_status, str(refund["order_id"])))

        if new_status == "SUCCESS":
            amount = finance_money(refund["amount"])
            order_id_value = str(refund["order_id"])
            refund_id_value = str(refund["refund_id"])
            booking_id_value = str(refund["booking_id"] or "")
            if booking_id_value:
                booking = conn.execute("""
                    SELECT id FROM bookings
                    WHERE CAST(booking_id AS TEXT)=CAST(? AS TEXT)
                    LIMIT 1
                """, (booking_id_value,)).fetchone()
                if booking:
                    conn.execute("""
                        UPDATE bookings
                        SET refund_status='refunded',
                            refund_amount=COALESCE(refund_amount,0)+?,
                            refund_reference=?, refund_processed_at=CURRENT_TIMESTAMP,
                            updated_at=CURRENT_TIMESTAMP
                        WHERE id=?
                    """, (amount, refund_id_value, booking["id"]))
                    payment_mode = (
                        "cashfree_sandbox"
                        if str(refund["environment"] or "").lower() == "sandbox"
                        else "live"
                    )
                    add_finance_ledger_entry(
                        conn, booking_id_value, "customer_refund", amount,
                        "manual_recorded", payment_mode, refund_id_value,
                        "Cashfree Dashboard refund manually verified by Sivdhi Admin.",
                    )

            if refund["customer_id"]:
                message = (
                    f"Your Cashfree refund of ₹{amount:.2f} for order {order_id_value} "
                    "has been marked successful by Sivdhi Admin after checking Cashfree Dashboard. "
                    "Your bank may take additional time to credit the amount."
                )
                message += f" Refund reference: {refund_id_value}."
                conn.execute("""
                    INSERT INTO notifications(user_type,user_id,title,message,is_read,created_at)
                    VALUES('customer',?,?,?,0,CURRENT_TIMESTAMP)
                """, (str(refund["customer_id"]), "Cashfree Refund Successful", message))

        conn.commit()
        flash(
            "Cashfree refund status updated to " + new_status + "."
            if new_status == "SUCCESS"
            else "Cashfree refund marked failed. A new attempt can be recorded after checking the Dashboard.",
            "success" if new_status == "SUCCESS" else "warning",
        )
    except Exception as error:
        conn.rollback()
        print("ADMIN CASHFREE REFUND STATUS ERROR:", error)
        flash("Cashfree refund status could not be updated.", "danger")
    finally:
        conn.close()

    return redirect(ADMIN_PANEL_BASE + "/admin/cashfree-refunds")


# =========================================================
# SERVICE CATALOG ROUTES
# =========================================================

@admin_bp.route("/admin/services")
@admin_required
def admin_services():
    return redirect(ADMIN_PANEL_BASE + "/admin/settings#subcategory")


@admin_bp.route("/admin/services/add", methods=["GET", "POST"])
@admin_required
def admin_add_service():

    if request.method == "GET":
        return redirect(ADMIN_PANEL_BASE + "/admin/settings#subcategory")

    conn = get_db()

    try:
        _ensure_admin_catalog_schema(conn)

        name = request.form.get("name", "").strip()
        icon = request.form.get("icon", "✦").strip() or "✦"
        description = request.form.get("description", "").strip()
        category = request.form.get("category", "").strip()
        active = 1 if request.form.get("active", "1") == "1" else 0

        if not name:
            flash("Subcategory name is required.", "danger")
            return _catalog_redirect("subcategory")

        category_row = conn.execute("""
            SELECT 1 FROM service_categories
            WHERE LOWER(name) = LOWER(?)
              AND COALESCE(is_active, 1) = 1
        """, (category,)).fetchone()

        if not category_row:
            flash("Please choose an active parent category.", "danger")
            return _catalog_redirect("subcategory")

        try:
            price = _admin_catalog_money(
                request.form.get("price"), "price"
            )
            commission = _admin_catalog_money(
                request.form.get("commission"), "commission"
            )
            partner_commission_percent = _admin_catalog_money(
                request.form.get("partner_commission_percent", "10"), "Partner commission"
            )
            platform_commission_percent = _admin_catalog_money(
                request.form.get("platform_commission_percent", "10"), "Sivdhi commission"
            )
            full_day_price = _admin_catalog_money(
                request.form.get("full_day_price"), "full-day price"
            )
            visit_fee = _admin_catalog_money(
                request.form.get("visit_fee"), "visit fee"
            )

            if commission > 100 or partner_commission_percent > 100 or platform_commission_percent > 100:
                raise ValueError("Commission must be between 0 and 100%.")
            if partner_commission_percent + platform_commission_percent > 100:
                raise ValueError("Partner and Sivdhi commission together cannot exceed 100%.")

        except ValueError as error:
            flash(str(error), "danger")
            return _catalog_redirect("subcategory")

        conn.execute("""
            INSERT INTO services
                (name, icon, description, active, status, category,
                 price, commission, partner_commission_percent, platform_commission_percent,
                 full_day_price, visit_fee, included_work, excluded_work)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            name,
            icon,
            description,
            active,
            "active" if active else "inactive",
            category,
            price,
            commission,
            partner_commission_percent,
            platform_commission_percent,
            full_day_price,
            visit_fee,
            request.form.get("included_work", "").strip(),
            request.form.get("excluded_work", "").strip(),
        ))

        # Availability flags are derived from configured subcategory prices.
        # This keeps Full-day categories from remaining hidden after an Admin
        # adds a priced Full-day subcategory.
        conn.execute("""
            UPDATE service_categories
            SET normal_enabled = CASE WHEN ? > 0 THEN 1 ELSE normal_enabled END,
                full_day_enabled = CASE WHEN ? > 0 THEN 1 ELSE full_day_enabled END
            WHERE LOWER(name)=LOWER(?)
        """, (price, full_day_price, category))
        conn.commit()

        service_id = conn.execute(
            "SELECT last_insert_rowid()"
        ).fetchone()[0]

        flash(
            "Subcategory added with Normal price, Full-day price, and commission settings.",
            "success",
        )

        return _catalog_redirect("subcategory")

    except sqlite3.IntegrityError:
        conn.rollback()
        flash(
            "Could not add this subcategory. Check whether it already exists.",
            "danger",
        )
        return _catalog_redirect("subcategory")

    finally:
        conn.close()


@admin_bp.route(
    "/admin/services/<int:service_id>/edit",
    methods=["GET", "POST"],
)
@admin_required
def admin_edit_service(service_id):

    if request.method == "GET":
        return _catalog_redirect(
            "subcategory",
            f"edit_service_id={service_id}",
        )

    conn = get_db()

    try:
        _ensure_admin_catalog_schema(conn)

        service = conn.execute("""
            SELECT * FROM services WHERE id = ?
        """, (service_id,)).fetchone()

        if not service:
            flash("Subcategory not found.", "danger")
            return _catalog_redirect("subcategory")

        name = request.form.get("name", "").strip()
        icon = request.form.get("icon", "✦").strip() or "✦"
        description = request.form.get("description", "").strip()
        category = request.form.get("category", "").strip()
        active = 1 if request.form.get("active") == "1" else 0

        if not name:
            flash("Subcategory name is required.", "danger")
            return _catalog_redirect(
                "subcategory",
                f"edit_service_id={service_id}",
            )

        category_row = conn.execute("""
            SELECT 1 FROM service_categories
            WHERE LOWER(name) = LOWER(?)
        """, (category,)).fetchone()

        if not category_row:
            flash("Please select a valid parent category.", "danger")
            return _catalog_redirect(
                "subcategory",
                f"edit_service_id={service_id}",
            )

        try:
            price = _admin_catalog_money(
                request.form.get("price"), "price"
            )
            commission = _admin_catalog_money(
                request.form.get("commission"), "commission"
            )
            partner_commission_percent = _admin_catalog_money(
                request.form.get("partner_commission_percent", service["partner_commission_percent"] if "partner_commission_percent" in service.keys() else 10), "Partner commission"
            )
            platform_commission_percent = _admin_catalog_money(
                request.form.get("platform_commission_percent", service["platform_commission_percent"] if "platform_commission_percent" in service.keys() else 10), "Sivdhi commission"
            )
            full_day_price = _admin_catalog_money(
                request.form.get("full_day_price"), "full-day price"
            )
            visit_fee = _admin_catalog_money(
                request.form.get("visit_fee"), "visit fee"
            )

            if commission > 100 or partner_commission_percent > 100 or platform_commission_percent > 100:
                raise ValueError("Commission must be between 0 and 100%.")
            if partner_commission_percent + platform_commission_percent > 100:
                raise ValueError("Partner and Sivdhi commission together cannot exceed 100%.")

        except ValueError as error:
            flash(str(error), "danger")
            return _catalog_redirect(
                "subcategory",
                f"edit_service_id={service_id}",
            )

        conn.execute("""
            UPDATE services
            SET name=?, icon=?, description=?, active=?, status=?,
                category=?, price=?, commission=?, partner_commission_percent=?,
                platform_commission_percent=?, full_day_price=?, visit_fee=?,
                included_work=?, excluded_work=?
            WHERE id=?
        """, (
            name,
            icon,
            description,
            active,
            "active" if active else "inactive",
            category,
            price,
            commission,
            partner_commission_percent,
            platform_commission_percent,
            full_day_price,
            visit_fee,
            request.form.get("included_work", "").strip(),
            request.form.get("excluded_work", "").strip(),
            service_id,
        ))

        conn.execute("""
            UPDATE service_categories
            SET normal_enabled = CASE WHEN EXISTS (
                    SELECT 1 FROM services WHERE LOWER(category)=LOWER(service_categories.name)
                    AND COALESCE(active,1)=1 AND COALESCE(price,0)>0
                ) THEN 1 ELSE normal_enabled END,
                full_day_enabled = CASE WHEN EXISTS (
                    SELECT 1 FROM services WHERE LOWER(category)=LOWER(service_categories.name)
                    AND COALESCE(active,1)=1 AND COALESCE(full_day_price,0)>0
                ) THEN 1 ELSE full_day_enabled END
            WHERE LOWER(name)=LOWER(?)
        """, (category,))
        conn.commit()
        flash("Subcategory details saved.", "success")

    except sqlite3.IntegrityError:
        conn.rollback()
        flash("Could not save this subcategory.", "danger")

    finally:
        conn.close()

    return _catalog_redirect(
        "subcategory",
        f"edit_service_id={service_id}",
    )


@admin_bp.route(
    "/admin/services/<int:service_id>/toggle",
    methods=["POST"],
)
@admin_required
def admin_toggle_service(service_id):

    conn = get_db()

    try:
        service = conn.execute("""
            SELECT active FROM services WHERE id = ?
        """, (service_id,)).fetchone()

        if not service:
            flash("Subcategory not found.", "danger")
            return _catalog_redirect("subcategory")

        new_status = 0 if service["active"] else 1

        conn.execute("""
            UPDATE services
            SET active = ?, status = ?
            WHERE id = ?
        """, (
            new_status,
            "active" if new_status else "inactive",
            service_id,
        ))

        conn.commit()

    finally:
        conn.close()

    flash("Subcategory status updated.", "success")
    return _catalog_redirect("subcategory")


@admin_bp.route(
    "/admin/services/<int:service_id>/delete",
    methods=["POST"],
)
@admin_required
def admin_delete_service(service_id):

    conn = get_db()

    try:
        service = conn.execute("""
            SELECT id FROM services WHERE id = ?
        """, (service_id,)).fetchone()

        if not service:
            flash("Subcategory not found.", "danger")
            return _catalog_redirect("subcategory")

        booking_count = conn.execute("""
            SELECT COUNT(*) FROM bookings WHERE service_id = ?
        """, (service_id,)).fetchone()[0]

        if booking_count:
            flash(
                "This service is already used in bookings. Disable it instead.",
                "danger",
            )
            return _catalog_redirect("subcategory")

        conn.execute("""
            DELETE FROM customer_service_problems
            WHERE service_id = ?
        """, (service_id,))

        conn.execute("""
            DELETE FROM services WHERE id = ?
        """, (service_id,))

        conn.commit()

    finally:
        conn.close()

    flash("Subcategory deleted.", "success")
    return _catalog_redirect("subcategory")


# =========================================================
# PROBLEM AND PRICE MANAGEMENT
# =========================================================

@admin_bp.route(
    "/admin/services/<int:service_id>/problems/add",
    methods=["POST"],
)
@admin_required
def admin_add_service_problem(service_id):
    flash("Problem-level pricing is retired. Set Normal and Full-day prices on the subcategory instead.", "info")
    return _catalog_redirect("subcategory")


    conn = get_db()

    try:
        _ensure_admin_catalog_schema(conn)

        service = conn.execute("""
            SELECT id FROM services WHERE id = ?
        """, (service_id,)).fetchone()

        if not service:
            flash("Subcategory not found.", "danger")
            return _catalog_redirect("subcategory")

        name = request.form.get("name", "").strip()
        description = request.form.get("description", "").strip()

        if not name:
            flash("Problem name is required.", "danger")
            return _catalog_redirect("subcategory")

        try:
            price = _admin_catalog_money(
                request.form.get("price"),
                "problem price",
            )
        except ValueError as error:
            flash(str(error), "danger")
            return _catalog_redirect("subcategory")

        conn.execute("""
            INSERT INTO customer_service_problems
                (service_id, name, description, price, is_active)
            VALUES (?, ?, ?, ?, 1)
        """, (service_id, name, description, price))

        conn.commit()
        flash("Problem and customer price added.", "success")

    except sqlite3.IntegrityError:
        conn.rollback()
        flash(
            "This problem already exists under the subcategory.",
            "danger",
        )

    finally:
        conn.close()

    return _catalog_redirect(
        "problem",
        f"service_id={service_id}",
    )


@admin_bp.route(
    "/admin/problems/<int:problem_id>/edit",
    methods=["POST"],
)
@admin_required
def admin_edit_service_problem(problem_id):
    flash("Problem-level pricing is retired. Set Normal and Full-day prices on the subcategory instead.", "info")
    return _catalog_redirect("subcategory")


    conn = get_db()
    service_id = None

    try:
        _ensure_admin_catalog_schema(conn)

        problem = conn.execute("""
            SELECT id, service_id
            FROM customer_service_problems
            WHERE id = ?
        """, (problem_id,)).fetchone()

        if not problem:
            flash("Problem not found.", "danger")
            return _catalog_redirect("subcategory")

        service_id = problem["service_id"]
        name = request.form.get("name", "").strip()
        description = request.form.get("description", "").strip()
        active = 1 if request.form.get("is_active", "1") == "1" else 0

        if not name:
            flash("Problem name is required.", "danger")
            return _catalog_redirect("subcategory")

        try:
            price = _admin_catalog_money(
                request.form.get("price"),
                "problem price",
            )
        except ValueError as error:
            flash(str(error), "danger")
            return _catalog_redirect("subcategory")

        conn.execute("""
            UPDATE customer_service_problems
            SET name = ?, description = ?, price = ?, is_active = ?
            WHERE id = ?
        """, (name, description, price, active, problem_id))

        conn.commit()
        flash("Problem details updated.", "success")

    except sqlite3.IntegrityError:
        conn.rollback()
        flash(
            "This problem name already exists under the subcategory.",
            "danger",
        )

    finally:
        conn.close()

    extra = f"service_id={service_id}" if service_id else ""
    return _catalog_redirect("subcategory")


@admin_bp.route(
    "/admin/problems/<int:problem_id>/delete",
    methods=["POST"],
)
@admin_required
def admin_delete_service_problem(problem_id):
    flash("Problem-level pricing is retired. Set Normal and Full-day prices on the subcategory instead.", "info")
    return _catalog_redirect("subcategory")


    conn = get_db()
    service_id = None

    try:
        _ensure_admin_catalog_schema(conn)

        problem = conn.execute("""
            SELECT id, service_id
            FROM customer_service_problems
            WHERE id = ?
        """, (problem_id,)).fetchone()

        if not problem:
            flash("Problem not found.", "danger")
            return _catalog_redirect("subcategory")

        service_id = problem["service_id"]

        conn.execute("""
            DELETE FROM customer_service_problems
            WHERE id = ?
        """, (problem_id,))

        conn.commit()
        flash("Problem removed from new customer bookings.", "success")

    finally:
        conn.close()

    return _catalog_redirect(
        "problem",
        f"service_id={service_id}",
    )


# =========================================================
# WITHDRAWALS
# =========================================================

@admin_bp.route("/admin/withdrawals")
@admin_required
def admin_withdrawals():

    conn = get_db()

    try:
        withdrawals = conn.execute("""
            SELECT
                wr.*,
                p.fullname AS partner_name,
                p.company_name AS company_name,
                p.mobile AS partner_mobile
            FROM withdraw_requests wr
            LEFT JOIN partners p
              ON CAST(p.partner_id AS TEXT) = CAST(wr.partner_id AS TEXT)
            ORDER BY wr.id DESC
        """).fetchall()

    finally:
        conn.close()

    total = len(withdrawals)
    pending = sum(
        1 for row in withdrawals
        if str(row["status"] or "Pending").lower()
        in {"pending", "processing"}
    )

    completed_amount = sum(
        float(row["amount"] or 0)
        for row in withdrawals
        if str(row["status"] or "").lower() == "completed"
    )

    return render_template(
        "admin/withdrawals.html",
        withdrawals=withdrawals,
        total=total,
        pending=pending,
        completed_amount=completed_amount,
        page_title="Withdrawals",
    )


@admin_bp.route(
    "/admin/withdrawals/<int:withdrawal_id>/status",
    methods=["POST"],
)
@admin_required
def admin_withdrawal_status(withdrawal_id):

    allowed = {
        "Pending",
        "Processing",
        "Completed",
        "Rejected",
        "Cancelled",
    }

    new_status = request.form.get(
        "status", "Pending"
    ).strip().title()

    if new_status not in allowed:
        flash("Invalid withdrawal status.", "danger")
        return redirect(ADMIN_PANEL_BASE + "/admin/withdrawals")

    conn = get_db()

    try:
        conn.execute("BEGIN IMMEDIATE")

        withdrawal = conn.execute("""
            SELECT * FROM withdraw_requests
            WHERE id = ?
        """, (withdrawal_id,)).fetchone()

        if not withdrawal:
            conn.rollback()
            flash("Withdrawal request not found.", "danger")
            return redirect(ADMIN_PANEL_BASE + "/admin/withdrawals")

        old_status = str(
            withdrawal["status"] or "Pending"
        ).title()

        if old_status in {"Completed", "Rejected", "Cancelled"}:
            conn.rollback()
            flash(
                "This withdrawal request is already final.",
                "warning",
            )
            return redirect(ADMIN_PANEL_BASE + "/admin/withdrawals")

        conn.execute("""
            UPDATE withdraw_requests
            SET status = ?
            WHERE id = ?
        """, (new_status, withdrawal_id))

        if new_status in {"Rejected", "Cancelled"}:
            partner_id = withdrawal["partner_id"]
            amount = float(withdrawal["amount"] or 0)

            conn.execute("""
                UPDATE partners
                SET wallet = COALESCE(wallet, 0) + ?
                WHERE partner_id = ?
            """, (amount, partner_id))

            conn.execute("""
                INSERT INTO wallet_transactions
                    (user_type, user_id, amount,
                     transaction_type, description)
                VALUES (?, ?, ?, ?, ?)
            """, (
                "partner",
                partner_id,
                amount,
                "withdrawal_refund",
                f"Withdrawal #{withdrawal_id} "
                f"{new_status.lower()} refund",
            ))

        conn.commit()
        flash(f"Withdrawal marked as {new_status}.", "success")

    except (sqlite3.Error, ValueError) as error:
        conn.rollback()
        print("ADMIN WITHDRAWAL STATUS ERROR:", error)
        flash("Withdrawal status could not be updated.", "danger")

    finally:
        conn.close()

    return redirect(ADMIN_PANEL_BASE + "/admin/withdrawals")


# =========================================================
# RATINGS
# =========================================================

@admin_bp.route("/admin/ratings")
@admin_required
def admin_ratings():

    conn = get_db()

    try:
        ratings = conn.execute("""
            SELECT
                wr.*,
                COALESCE(w.fullname, 'Unknown Worker') AS worker_name
            FROM worker_ratings wr
            LEFT JOIN workers w ON w.worker_id = wr.worker_id
            ORDER BY wr.id DESC
        """).fetchall()

    except sqlite3.Error as error:
        print("ADMIN RATINGS ERROR:", error)
        ratings = []

    finally:
        conn.close()

    return render_template(
        "admin/ratings.html",
        ratings=ratings,
    )


# =========================================================
# NOTIFICATIONS
# =========================================================

@admin_bp.route("/admin/notifications/unread-count")
@admin_required
def admin_unread_notification_count():
    """Return the current unread notification count for the admin bell."""
    conn = get_db()

    try:
        unread_count = conn.execute("""
            SELECT COUNT(*)
            FROM notifications
            WHERE COALESCE(is_read, 0) = 0
        """).fetchone()[0]
        return {"count": int(unread_count or 0)}

    except sqlite3.Error as error:
        print("ADMIN NOTIFICATION COUNT ERROR:", error)
        return {"count": 0}, 500

    finally:
        conn.close()


@admin_bp.route("/admin/notifications")
@admin_required
def admin_notifications():

    conn = get_db()

    try:
        notifications = conn.execute("""
            SELECT * FROM notifications
            ORDER BY id DESC
        """).fetchall()

    except sqlite3.Error as error:
        print("ADMIN NOTIFICATIONS ERROR:", error)
        notifications = []

    finally:
        conn.close()

    return render_template(
        "admin/notifications.html",
        notifications=notifications,
    )


@admin_bp.route(
    "/admin/notifications/<int:notification_id>/read",
    methods=["POST"],
)
@admin_required
def admin_mark_notification_read(notification_id):

    conn = get_db()

    try:
        cur = conn.execute("""
            UPDATE notifications
            SET is_read = 1
            WHERE id = ?
        """, (notification_id,))

        conn.commit()

        flash(
            "Notification marked as read."
            if cur.rowcount
            else "Notification not found.",
            "success" if cur.rowcount else "warning",
        )

    except sqlite3.Error as error:
        conn.rollback()
        print("ADMIN NOTIFICATION ERROR:", error)
        flash("Notification could not be updated.", "danger")

    finally:
        conn.close()

    return redirect(ADMIN_PANEL_BASE + "/admin/notifications")


# =========================================================
# SETTINGS PAGE — ALL THREE TABS IN ONE PAGE
# =========================================================

@admin_bp.route("/admin/settings", methods=["GET", "POST"])
@admin_required
def admin_settings():
    """The service catalogue and internal commission settings only."""
    conn = get_db()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS admin_settings (
                setting_key TEXT PRIMARY KEY,
                setting_value TEXT NOT NULL
            )
        """)
        defaults = {
            "partner_commission_percent": "10",
            "platform_commission_percent": "10",
        }
        for key, value in defaults.items():
            conn.execute(
                "INSERT OR IGNORE INTO admin_settings(setting_key, setting_value) VALUES (?, ?)",
                (key, value),
            )

        if request.method == "POST":
            try:
                partner_percent = _admin_catalog_money(
                    request.form.get("partner_commission_percent"),
                    "partner commission percentage",
                )
                platform_percent = _admin_catalog_money(
                    request.form.get("platform_commission_percent"),
                    "platform commission percentage",
                )
                if partner_percent > 100 or platform_percent > 100:
                    raise ValueError("Each commission must be between 0 and 100%.")
                if partner_percent + platform_percent > 100:
                    raise ValueError("Partner and Sivdhi commission together cannot exceed 100%.")
            except ValueError as error:
                conn.rollback()
                flash(str(error), "danger")
                return _catalog_redirect("commission")

            conn.execute("""
                INSERT INTO admin_settings(setting_key, setting_value)
                VALUES ('partner_commission_percent', ?)
                ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value
            """, (str(partner_percent),))
            conn.execute("""
                INSERT INTO admin_settings(setting_key, setting_value)
                VALUES ('platform_commission_percent', ?)
                ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value
            """, (str(platform_percent),))
            conn.commit()
            flash("Commission settings saved for new bookings. Customer-facing prices do not show a separate platform fee.", "success")
            return _catalog_redirect("commission")

        _ensure_admin_catalog_schema(conn)
        service_categories = conn.execute("""
            SELECT * FROM service_categories
            ORDER BY name COLLATE NOCASE
        """).fetchall()
        services = conn.execute("""
            SELECT s.*, COALESCE(sc.name, s.category, '') AS category_name
            FROM services s
            LEFT JOIN service_categories sc
              ON LOWER(sc.name) = LOWER(COALESCE(s.category, ''))
            ORDER BY COALESCE(sc.name, s.category, '') COLLATE NOCASE,
                     s.name COLLATE NOCASE
        """).fetchall()
        problems = conn.execute("""
            SELECT p.*, s.name AS service_name,
                   COALESCE(sc.name, s.category, '') AS category_name
            FROM customer_service_problems p
            JOIN services s ON s.id = p.service_id
            LEFT JOIN service_categories sc
              ON LOWER(sc.name) = LOWER(COALESCE(s.category, ''))
            ORDER BY COALESCE(sc.name, s.category, '') COLLATE NOCASE,
                     s.name COLLATE NOCASE, p.name COLLATE NOCASE
        """).fetchall()
        settings = {
            row["setting_key"]: row["setting_value"]
            for row in conn.execute("SELECT setting_key, setting_value FROM admin_settings").fetchall()
        }
        return render_template(
            "admin/settings.html",
            settings=settings,
            service_categories=service_categories,
            services=services,
            problems=problems,
            page_title="Service Catalogue & Commission",
        )
    finally:
        conn.close()


# =========================================================
# CREATE CATEGORY
# =========================================================

@admin_bp.route("/admin/categories/add", methods=["POST"])
@admin_required
def admin_add_category():

    name = request.form.get("name", "").strip()

    if not name:
        flash("Category name is required.", "danger")
        return _catalog_redirect("category")

    conn = get_db()

    try:
        _ensure_admin_catalog_schema(conn)

        exists = conn.execute("""
            SELECT 1 FROM service_categories
            WHERE LOWER(name) = LOWER(?)
        """, (name,)).fetchone()

        if exists:
            flash("This category already exists.", "danger")
            return _catalog_redirect("category")

        try:
            price = _admin_catalog_money(
                request.form.get("price"), "base price"
            )
            commission = _admin_catalog_money(
                request.form.get("commission"), "commission"
            )
            full_day_price = _admin_catalog_money(
                request.form.get("full_day_price"), "full-day price"
            )
            visit_fee = _admin_catalog_money(
                request.form.get("visit_fee"), "visit fee"
            )

            if commission > 100:
                raise ValueError(
                    "Commission must be between 0 and 100%."
                )

        except ValueError as error:
            flash(str(error), "danger")
            return _catalog_redirect("category")

        normal_enabled = (
            1 if request.form.get("normal_enabled", "1") == "1" else 0
        )

        full_day_enabled = (
            1 if request.form.get("full_day_enabled", "0") == "1" else 0
        )

        if full_day_enabled and full_day_price <= 0:
            flash(
                "Enter a full-day price before enabling FULL-DAY mode.",
                "danger",
            )
            return _catalog_redirect("category")

        conn.execute("""
            INSERT INTO service_categories
                (name, price, commission, icon, description, is_active,
                 normal_enabled, full_day_enabled, full_day_price,
                 visit_fee, included_work, excluded_work)
            VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)
        """, (
            name,
            price,
            commission,
            request.form.get("icon", "✦").strip() or "✦",
            request.form.get("description", "").strip(),
            normal_enabled,
            full_day_enabled,
            full_day_price,
            visit_fee,
            request.form.get("included_work", "").strip(),
            request.form.get("excluded_work", "").strip(),
        ))

        conn.commit()
        flash(
            "Category added. Open the Subcategory tab to add services.",
            "success",
        )

    except sqlite3.IntegrityError:
        conn.rollback()
        flash("This category already exists.", "danger")

    finally:
        conn.close()

    return _catalog_redirect("category")


# =========================================================
# EDIT CATEGORY
# =========================================================

@admin_bp.route(
    "/admin/categories/<int:category_id>/edit",
    methods=["POST"],
)
@admin_required
def admin_edit_category(category_id):

    name = request.form.get("name", "").strip()

    if not name:
        flash("Category name is required.", "danger")
        return _catalog_redirect("category")

    conn = get_db()

    try:
        _ensure_admin_catalog_schema(conn)

        old = conn.execute("""
            SELECT * FROM service_categories WHERE id = ?
        """, (category_id,)).fetchone()

        if not old:
            flash("Category not found.", "danger")
            return _catalog_redirect("category")

        duplicate = conn.execute("""
            SELECT 1
            FROM service_categories
            WHERE LOWER(name) = LOWER(?) AND id <> ?
        """, (name, category_id)).fetchone()

        if duplicate:
            flash("Another category already uses this name.", "danger")
            return _catalog_redirect("category")

        try:
            price = _admin_catalog_money(
                request.form.get("price", old["price"]),
                "base price",
            )
            commission = _admin_catalog_money(
                request.form.get("commission", old["commission"]),
                "commission",
            )
            full_day_price = _admin_catalog_money(
                request.form.get("full_day_price", old["full_day_price"]),
                "full-day price",
            )
            visit_fee = _admin_catalog_money(
                request.form.get("visit_fee", old["visit_fee"]),
                "visit fee",
            )

            if commission > 100:
                raise ValueError(
                    "Commission must be between 0 and 100%."
                )

        except ValueError as error:
            flash(str(error), "danger")
            return _catalog_redirect("category")

        normal_enabled = (
            1 if request.form.get("normal_enabled") == "1" else 0
        ) if "normal_enabled" in request.form else old["normal_enabled"]

        full_day_enabled = (
            1 if request.form.get("full_day_enabled") == "1" else 0
        ) if "full_day_enabled" in request.form else old["full_day_enabled"]

        if full_day_enabled and full_day_price <= 0:
            flash(
                "Enter a full-day price before enabling FULL-DAY mode.",
                "danger",
            )
            return _catalog_redirect("category")

        old_name = old["name"]

        conn.execute("""
            UPDATE service_categories
            SET name=?, price=?, commission=?, icon=?, description=?,
                is_active=?, normal_enabled=?, full_day_enabled=?,
                full_day_price=?, visit_fee=?, included_work=?, excluded_work=?
            WHERE id=?
        """, (
            name,
            price,
            commission,
            request.form.get("icon", old["icon"] or "✦").strip() or "✦",
            request.form.get(
                "description", old["description"] or ""
            ).strip(),
            1 if request.form.get(
                "is_active", str(old["is_active"] or 1)
            ) == "1" else 0,
            normal_enabled,
            full_day_enabled,
            full_day_price,
            visit_fee,
            request.form.get(
                "included_work", old["included_work"] or ""
            ).strip(),
            request.form.get(
                "excluded_work", old["excluded_work"] or ""
            ).strip(),
            category_id,
        ))

        if old_name.lower() != name.lower():
            conn.execute("""
                UPDATE services
                SET category = ?
                WHERE LOWER(category) = LOWER(?)
            """, (name, old_name))

        conn.commit()
        flash(
            "Category settings saved. Its subcategories remain linked.",
            "success",
        )

    except sqlite3.IntegrityError:
        conn.rollback()
        flash("Could not save this category.", "danger")

    finally:
        conn.close()

    return _catalog_redirect("category")


# =========================================================
# DELETE / DISABLE CATEGORY
# =========================================================

@admin_bp.route(
    "/admin/categories/<int:category_id>/delete",
    methods=["POST"],
)
@admin_required
def admin_delete_category(category_id):

    conn = get_db()

    try:
        _ensure_admin_catalog_schema(conn)

        category = conn.execute("""
            SELECT id, name
            FROM service_categories
            WHERE id = ?
        """, (category_id,)).fetchone()

        if not category:
            flash("Category not found.", "danger")
            return _catalog_redirect("category")

        linked_count = conn.execute("""
            SELECT COUNT(*)
            FROM services
            WHERE LOWER(COALESCE(category, '')) = LOWER(?)
        """, (category["name"],)).fetchone()[0]

        if linked_count:
            conn.execute("""
                UPDATE service_categories
                SET is_active = 0,
                    normal_enabled = 0,
                    full_day_enabled = 0
                WHERE id = ?
            """, (category_id,))

            conn.commit()

            flash(
                "Category has subcategories, so it was disabled instead of deleted.",
                "warning",
            )

        else:
            conn.execute("""
                DELETE FROM service_categories WHERE id = ?
            """, (category_id,))

            conn.commit()
            flash("Category deleted successfully.", "success")

    finally:
        conn.close()

    return _catalog_redirect("category")


# =========================================================
# REFERRAL OFFER MANAGEMENT
# =========================================================

@admin_bp.route("/admin/referral-offers/create", methods=["POST"])
@admin_required
def admin_create_referral_offer():

    partner_id = request.form.get("partner_id", "").strip()
    duration = request.form.get("duration", "").strip().lower()

    allowed_days = {
        "7": 7,
        "15": 15,
        "30": 30,
        "60": 60,
        "90": 90,
    }

    if duration == "custom":
        try:
            days = int(request.form.get("custom_days", "0"))
        except (TypeError, ValueError):
            days = 0

        if days < 1 or days > 3650:
            flash(
                "Custom offer duration must be between 1 and 3650 days.",
                "danger",
            )
            return _catalog_redirect("category")

    else:
        days = allowed_days.get(duration, 0)

    if not partner_id or not days:
        flash(
            "Please select a partner and offer duration.",
            "danger",
        )
        return _catalog_redirect("category")

    conn = get_db()

    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS referral_offers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                partner_id TEXT NOT NULL,
                referral_code TEXT NOT NULL,
                starts_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)

        partner = conn.execute("""
            SELECT partner_id, referral_code, fullname
            FROM partners
            WHERE CAST(partner_id AS TEXT) = CAST(? AS TEXT)
              AND COALESCE(is_deleted, 0) = 0
            LIMIT 1
        """, (partner_id,)).fetchone()

        if not partner:
            flash("Selected partner was not found.", "danger")
            return _catalog_redirect("category")

        referral_code = (
            partner["referral_code"] or ""
        ).strip().upper()

        if not referral_code:
            flash(
                "This partner does not have a referral code.",
                "danger",
            )
            return _catalog_redirect("category")

        conn.execute("""
            UPDATE referral_offers SET active = 0 WHERE active = 1
        """)

        conn.execute("""
            INSERT INTO referral_offers
                (partner_id, referral_code, starts_at, expires_at, active)
            VALUES (?, ?, CURRENT_TIMESTAMP, datetime('now', ?), 1)
        """, (
            partner_id,
            referral_code,
            f"+{days} days",
        ))

        conn.commit()

    except sqlite3.Error as error:
        conn.rollback()
        print("ADMIN REFERRAL OFFER ERROR:", error)
        flash("Referral offer could not be activated.", "danger")
        return _catalog_redirect("category")

    finally:
        conn.close()

    flash(
        f"Referral offer activated for {days} day(s). "
        f"Active signup code is {referral_code}.",
        "success",
    )
    return _catalog_redirect("category")


@admin_bp.route(
    "/admin/referral-offers/<int:offer_id>/toggle",
    methods=["POST"],
)
@admin_required
def admin_toggle_referral_offer(offer_id):

    conn = get_db()

    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS referral_offers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                partner_id TEXT NOT NULL,
                referral_code TEXT NOT NULL,
                starts_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)

        offer = conn.execute("""
            SELECT id, active, expires_at
            FROM referral_offers
            WHERE id = ?
            LIMIT 1
        """, (offer_id,)).fetchone()

        if not offer:
            flash("Referral offer not found.", "danger")
            return _catalog_redirect("category")

        if offer["active"]:
            conn.execute("""
                UPDATE referral_offers SET active = 0 WHERE id = ?
            """, (offer_id,))
            message = (
                "Referral offer disabled. New signups will use SV00000."
            )

        else:
            valid_offer = conn.execute("""
                SELECT id
                FROM referral_offers
                WHERE id = ?
                  AND datetime(expires_at) > datetime('now')
                LIMIT 1
            """, (offer_id,)).fetchone()

            if not valid_offer:
                flash("This referral offer has already expired.", "danger")
                return _catalog_redirect("category")

            conn.execute("""
                UPDATE referral_offers SET active = 0 WHERE active = 1
            """)

            conn.execute("""
                UPDATE referral_offers SET active = 1 WHERE id = ?
            """, (offer_id,))

            message = "Referral offer activated."

        conn.commit()

    except sqlite3.Error as error:
        conn.rollback()
        print("ADMIN REFERRAL TOGGLE ERROR:", error)
        flash("Referral offer could not be updated.", "danger")
        return _catalog_redirect("category")

    finally:
        conn.close()

    flash(message, "success")
    return _catalog_redirect("category")


# =========================================================
# ADMIN LOGOUT
# =========================================================

@admin_bp.route("/admin/logout", methods=["POST"])
def admin_logout():

    session.clear()
    flash("Admin logged out successfully.", "success")

    return redirect(ADMIN_PANEL_BASE + "/admin/login")