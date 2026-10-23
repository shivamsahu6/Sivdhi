from functools import wraps

from flask import redirect, session, url_for

from database import get_db


def customer_required(view):
    """Require a valid customer session tied to an existing customer record."""

    @wraps(view)
    def wrapped(*args, **kwargs):
        customer_id = session.get("customer_id")

        # customer_id is the internal customers.id value set at signup/login.
        # Reject missing, malformed, or non-positive values before querying.
        try:
            customer_db_id = int(customer_id)
        except (TypeError, ValueError):
            session.clear()
            return redirect(url_for("customer.login"))

        if customer_db_id <= 0:
            session.clear()
            return redirect(url_for("customer.login"))

        conn = get_db()
        try:
            customer = conn.execute(
                """
                SELECT id, customer_id, fullname, email, mobile
                FROM customers
                WHERE id = ?
                LIMIT 1
                """,
                (customer_db_id,),
            ).fetchone()
        finally:
            conn.close()

        if not customer:
            session.clear()
            return redirect(url_for("customer.login"))

        # Refresh non-sensitive session identity values from the database.
        # This prevents stale name/email/code values after a profile update.
        session["customer_id"] = customer["id"]
        session["customer_code"] = customer["customer_id"]
        session["customer_name"] = customer["fullname"]
        session["customer_email"] = customer["email"]
        session["customer_mobile"] = customer["mobile"]

        return view(*args, **kwargs)

    return wrapped
