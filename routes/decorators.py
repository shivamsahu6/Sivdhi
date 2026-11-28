from functools import wraps
from flask import session, redirect, url_for, request
from database import get_db


def _is_active_account(row):
    """Return True only for an existing, non-deleted, non-suspended account."""
    if not row:
        return False
    if bool(row["is_deleted"]):
        return False
    return str(row["status"] or "active").strip().lower() != "suspended"


# ==========================================================
# WORKER LOGIN REQUIRED
# ==========================================================

def worker_required(view_function):
    @wraps(view_function)
    def wrapped_view(*args, **kwargs):
        if session.get("role") != "worker":
            return redirect(url_for("auth.login", role="worker"))

        user_id = session.get("user_id")
        if not user_id:
            session.clear()
            return redirect(url_for("auth.login", role="worker"))

        conn = get_db()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT is_deleted, status
                FROM workers
                WHERE worker_id=?
                LIMIT 1
                """,
                (str(user_id),),
            )
            worker = cur.fetchone()
        finally:
            conn.close()

        if not _is_active_account(worker):
            session.clear()
            return redirect(url_for("auth.login", role="worker"))

        return view_function(*args, **kwargs)

    return wrapped_view


# ==========================================================
# CUSTOMER LOGIN REQUIRED
# ==========================================================

def customer_login_required(view_function):
    """Require a logged-in customer without using worker/partner role state."""
    @wraps(view_function)
    def wrapped_view(*args, **kwargs):
        if not session.get("customer_id"):
            return redirect(url_for("customer.login", next=request.full_path))

        conn = get_db()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT *
                FROM customers
                WHERE id=?
                LIMIT 1
                """,
                (session.get("customer_id"),),
            )
            customer = cur.fetchone()
        finally:
            conn.close()

        if not customer:
            session.clear()
            return redirect(url_for("customer.login", next=request.full_path))

        if "is_deleted" in customer.keys() and bool(customer["is_deleted"]):
            session.clear()
            return redirect(url_for("customer.login", next=request.full_path))

        if "status" in customer.keys() and str(customer["status"] or "active").strip().lower() == "suspended":
            session.clear()
            return redirect(url_for("customer.login", next=request.full_path))

        return view_function(*args, **kwargs)

    return wrapped_view


# ==========================================================
# PARTNER LOGIN REQUIRED
# ==========================================================

def partner_required(view_function):
    @wraps(view_function)
    def wrapped_view(*args, **kwargs):
        if session.get("role") != "partner":
            return redirect(url_for("auth.login", role="partner"))

        user_id = session.get("user_id")
        if not user_id:
            session.clear()
            return redirect(url_for("auth.login", role="partner"))

        conn = get_db()
        try:
            cur = conn.cursor()
            partner_columns = {
                row[1] for row in cur.execute("PRAGMA table_info(partners)").fetchall()
            }
            internal_expr = (
                "COALESCE(is_internal,0)"
                if "is_internal" in partner_columns else "0"
            )
            referral_expr = (
                "referral_code"
                if "referral_code" in partner_columns else "NULL"
            )
            cur.execute(
                f"""
                SELECT is_deleted, status,
                       {internal_expr} AS is_internal,
                       {referral_expr} AS referral_code
                FROM partners
                WHERE partner_id=?
                LIMIT 1
                """,
                (str(user_id),),
            )
            partner = cur.fetchone()
        finally:
            conn.close()

        if not _is_active_account(partner):
            session.clear()
            return redirect(url_for("auth.login", role="partner"))

        session["is_internal_partner"] = bool(partner["is_internal"] or 0)

        # Repair missing referral-team rows for older and newly registered
        # workers without stealing workers already attributed to another Partner.
        referral_code = str(partner["referral_code"] or "").strip()
        if referral_code:
            table_names = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            worker_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(workers)").fetchall()
            } if "workers" in table_names else set()
            if (
                "partner_workers" in table_names
                and {"worker_id", "referred_by", "is_deleted", "id"}.issubset(worker_columns)
            ):
                linked = conn.execute("""
                    INSERT INTO partner_workers(partner_id, worker_id)
                    SELECT ?, w.worker_id
                    FROM workers w
                    WHERE UPPER(TRIM(COALESCE(w.referred_by,'')))=UPPER(?)
                      AND w.worker_id IS NOT NULL
                      AND COALESCE(w.is_deleted,0)=0
                      AND NOT EXISTS (
                          SELECT 1 FROM partner_workers pw
                          WHERE CAST(pw.worker_id AS TEXT) IN (
                              CAST(w.worker_id AS TEXT), CAST(w.id AS TEXT)
                          )
                      )
                """, (str(user_id), referral_code))
                if linked.rowcount:
                    conn.commit()

        return view_function(*args, **kwargs)

    return wrapped_view


# ==========================================================
# ADMIN LOGIN REQUIRED
# ==========================================================

def admin_required(view_function):
    @wraps(view_function)
    def wrapped_view(*args, **kwargs):
        if session.get("role") != "admin":
            return redirect(url_for("admin.admin_login"))

        admin_id = session.get("admin_id")
        if not admin_id:
            session.clear()
            return redirect(url_for("admin.admin_login"))

        # Admin access requires an active account and completed website MFA.
        from utils.admin_mfa import is_admin_authorized
        if not is_admin_authorized(str(admin_id)):
            session.clear()
            return redirect(url_for("admin.admin_login"))

        return view_function(*args, **kwargs)

    return wrapped_view
