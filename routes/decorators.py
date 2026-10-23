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
            return redirect(url_for("customer.login", next=request.path))

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
            return redirect(url_for("customer.login", next=request.path))

        if "is_deleted" in customer.keys() and bool(customer["is_deleted"]):
            session.clear()
            return redirect(url_for("customer.login", next=request.path))

        if "status" in customer.keys() and str(customer["status"] or "active").strip().lower() == "suspended":
            session.clear()
            return redirect(url_for("customer.login", next=request.path))

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
            cur.execute(
                """
                SELECT is_deleted, status
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

        conn = get_db()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT admin_id
                FROM admin
                WHERE admin_id=?
                LIMIT 1
                """,
                (str(admin_id),),
            )
            admin = cur.fetchone()
        finally:
            conn.close()

        if not admin:
            session.clear()
            return redirect(url_for("admin.admin_login"))

        return view_function(*args, **kwargs)

    return wrapped_view
