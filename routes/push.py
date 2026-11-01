"""Authenticated Android FCM token registration."""
from flask import Blueprint, current_app, jsonify, request, session

from database import get_db
from utils.push_notifications import ensure_customer_push_columns

push_bp = Blueprint("push", __name__)

_ALLOWED_PACKAGES = {"com.sivdhi.customer", "com.sivdhi.workpartner"}


@push_bp.post("/api/push/register")
def register_device_token():
    """Bind the caller's FCM token to the currently authenticated account."""
    if not request.is_json:
        return jsonify(success=False, error="JSON request required"), 415

    payload = request.get_json(silent=True) or {}
    token = str(payload.get("token") or "").strip()
    app_package = str(payload.get("app_package") or "").strip()

    if not 80 <= len(token) <= 4096 or any(ch.isspace() for ch in token):
        return jsonify(success=False, error="Invalid device token"), 400
    if app_package not in _ALLOWED_PACKAGES:
        return jsonify(success=False, error="Unsupported Sivdhi app"), 400

    customer_id = session.get("customer_id")
    role = str(session.get("role") or "").strip().lower()
    user_id = session.get("user_id")

    if customer_id:
        if app_package != "com.sivdhi.customer":
            return jsonify(success=False, error="Customer app required"), 403
        user_type = "customer"
        owner_id = str(customer_id)
        table, key = "customers", "id"
    elif role == "worker" and user_id:
        if app_package != "com.sivdhi.workpartner":
            return jsonify(success=False, error="Worker Partner app required"), 403
        user_type = "worker"
        owner_id = str(user_id)
        table, key = "workers", "worker_id"
    elif role == "partner" and user_id:
        if app_package != "com.sivdhi.workpartner":
            return jsonify(success=False, error="Worker Partner app required"), 403
        user_type = "partner"
        owner_id = str(user_id)
        table, key = "partners", "partner_id"
    else:
        return jsonify(success=False, error="Login required"), 401

    conn = get_db()
    try:
        if user_type == "customer":
            ensure_customer_push_columns(conn)

        account = conn.execute(
            f'SELECT status, is_deleted FROM "{table}" '
            f'WHERE CAST("{key}" AS TEXT)=? LIMIT 1',
            (owner_id,),
        ).fetchone()
        if not account:
            return jsonify(success=False, error="Account not found"), 401
        if bool(account["is_deleted"]) or str(account["status"] or "active").lower() == "suspended":
            return jsonify(success=False, error="Account is inactive"), 403

        conn.execute(
            """
            INSERT INTO push_device_tokens
                (user_type, user_id, token, app_package, platform,
                 is_active, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'android', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT(token) DO UPDATE SET
                user_type=excluded.user_type,
                user_id=excluded.user_id,
                app_package=excluded.app_package,
                platform='android',
                is_active=1,
                updated_at=CURRENT_TIMESTAMP
            """,
            (user_type, owner_id, token, app_package),
        )
        conn.commit()
        return jsonify(success=True, registered=True), 200
    except Exception:
        conn.rollback()
        current_app.logger.exception("Sivdhi FCM device-token registration failed")
        return jsonify(success=False, error="Registration failed"), 500
    finally:
        conn.close()
