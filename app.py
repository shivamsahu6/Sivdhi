import re
import hmac
import sqlite3
from datetime import datetime

from werkzeug.security import generate_password_hash
from flask import render_template, abort, request

import os
from pathlib import Path

from flask import Flask, request, jsonify, current_app, session
from dotenv import load_dotenv
from werkzeug.middleware.proxy_fix import ProxyFix
from flask_wtf.csrf import CSRFProtect

load_dotenv(Path(__file__).resolve().parent / ".env", override=False)

from config import Config
from database import create_tables, get_db

# ==========================
# Blueprints
# ==========================

from routes.auth import auth_bp
from routes.worker import worker_bp
from routes.partner import partner_bp
from routes.admin import admin_bp, _ensure_admin_catalog_schema
from routes.customer import customer_bp

# ==========================
# Flask App
# ==========================

app = Flask(__name__)
app.config.from_object(Config)

# Trust proxy headers only when explicitly configured.
try:
    trusted_proxy_hops = max(
        0,
        min(3, int(os.getenv("TRUSTED_PROXY_HOPS", "0")))
    )
except (TypeError, ValueError):
    trusted_proxy_hops = 0

if trusted_proxy_hops:
    app.wsgi_app = ProxyFix(
        app.wsgi_app,
        x_for=trusted_proxy_hops,
        x_proto=trusted_proxy_hops,
        x_host=0,
        x_port=0,
        x_prefix=0,
    )

# CSRF protection for browser form submissions.
csrf = CSRFProtect(app)
@app.route("/setup-admin", methods=["GET", "POST"])
def temporary_admin_setup():
    """One-time, secret-protected admin bootstrap; remove after use."""
    setup_key = os.getenv("ADMIN_SETUP_KEY", "").strip()

    if len(setup_key) < 32:
        abort(404)

    conn = get_db()

    try:
        # केवल पहले Admin account के लिए।
        if conn.execute(
            "SELECT 1 FROM admin LIMIT 1"
        ).fetchone():
            abort(404)

        error = ""

        if request.method == "POST":
            supplied_key = request.form.get("setup_key", "")
            admin_id = request.form.get("admin_id", "").strip()
            fullname = request.form.get("fullname", "").strip()
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")
            confirm = request.form.get("confirm_password", "")

            if not hmac.compare_digest(supplied_key, setup_key):
                error = "Setup key is incorrect."

            elif not admin_id or len(admin_id) > 64:
                error = "Admin ID is required (maximum 64 characters)."

            elif not fullname or len(fullname) > 100:
                error = "Full name is required (maximum 100 characters)."

            elif (
                not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email)
                or len(email) > 254
            ):
                error = "Enter a valid email address."

            elif (
                len(password) < 12
                or not re.search(r"[A-Za-z]", password)
                or not re.search(r"\d", password)
            ):
                error = (
                    "Password must be at least 12 characters "
                    "and contain a letter and a number."
                )

            elif password != confirm:
                error = "Passwords do not match."

            else:
                try:
                    conn.execute(
                        """
                        INSERT INTO admin
                        (admin_id, fullname, email, password, created_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            admin_id,
                            fullname,
                            email,
                            generate_password_hash(password),
                            datetime.now().isoformat(),
                        ),
                    )

                    conn.commit()

                    return render_template(
                        "setup_admin.html",
                        success=True,
                        error="",
                    )

                except sqlite3.IntegrityError:
                    conn.rollback()
                    error = "Admin ID or email already exists."

        return render_template(
            "setup_admin.html",
            success=False,
            error=error,
        )

    finally:
        conn.close()

@app.get("/health")
def health_check():
    """Check app/database availability without exposing database details."""
    try:
        conn = get_db()
        try:
            conn.execute("SELECT 1").fetchone()
        finally:
            conn.close()

        return jsonify({"status": "ok"}), 200

    except Exception:
        current_app.logger.exception("Health check failed")
        return jsonify({"status": "unavailable"}), 503


@app.after_request
def add_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault(
        "Referrer-Policy",
        "strict-origin-when-cross-origin",
    )
    response.headers.setdefault(
        "Permissions-Policy",
        "camera=(), microphone=(), geolocation=(self)",
    )
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "base-uri 'self'; "
        "form-action 'self' https: mailto:; "
        "frame-ancestors 'self'; "
        "object-src 'none'; "
        "img-src 'self' data: blob: https:; "
        "font-src 'self' data: https:; "
        "style-src 'self' 'unsafe-inline' https:; "
        "script-src 'self' 'unsafe-inline' https:; "
        "connect-src 'self' https:",
    )

    production = (
        os.getenv("APP_ENV", os.getenv("FLASK_ENV", "development"))
        .strip()
        .lower()
        == "production"
    )

    if production or request.is_secure:
        response.headers.setdefault(
            "Strict-Transport-Security",
            "max-age=31536000; includeSubDomains",
        )

    # Avoid caching authenticated pages containing private account details.
    if any(
        session.get(key)
        for key in (
            "user_id",
            "customer_id",
            "admin_id",
            "customer_logged_in",
            "admin_logged_in",
        )
    ):
        response.headers.setdefault(
            "Cache-Control",
            "no-store, max-age=0, private",
        )
        response.headers.setdefault("Pragma", "no-cache")

    return response


app.secret_key = Config.SECRET_KEY


# ==========================
# Database initialization
# ==========================

# Create base tables first.
create_tables()

# Upgrade legacy service/category tables before any page tries to query them.
# This migration adds missing columns and preserves existing records.
_catalog_conn = get_db()
try:
    _ensure_admin_catalog_schema(_catalog_conn)
    _catalog_conn.commit()
finally:
    _catalog_conn.close()


# Add assignment-monitor metadata without deleting existing data.
from utils.assignment_monitor import (
    ensure_assignment_monitor_schema,
    start_assignment_monitor,
)

_assignment_schema_conn = get_db()
try:
    ensure_assignment_monitor_schema(_assignment_schema_conn)
    _assignment_schema_conn.commit()
finally:
    _assignment_schema_conn.close()


# ==========================
# Global service categories
# ==========================

@app.context_processor
def inject_service_categories():
    """Make admin-managed categories available to every template."""
    conn = None
    try:
        conn = get_db()
        rows = conn.execute(
            """
            SELECT id, name, icon, description, is_active,
                   normal_enabled, full_day_enabled, full_day_price
            FROM service_categories
            ORDER BY name COLLATE NOCASE ASC
            """
        ).fetchall()

        return {"service_categories_global": rows}

    except Exception:
        current_app.logger.exception(
            "Could not load global service categories"
        )
        return {"service_categories_global": []}

    finally:
        if conn is not None:
            conn.close()


# ==========================
# Register Blueprints
# ==========================

app.register_blueprint(auth_bp)
app.register_blueprint(worker_bp)
app.register_blueprint(partner_bp)
app.register_blueprint(admin_bp)
app.register_blueprint(customer_bp)


# ==========================
# Run Server
# ==========================

if __name__ == "__main__":
    start_assignment_monitor(app)

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "5000"))

    production = (
        os.getenv("APP_ENV", os.getenv("FLASK_ENV", "development"))
        .strip()
        .lower()
        == "production"
    )

    if production:
        from waitress import serve

        serve(
            app,
            host=host,
            port=port,
            threads=max(4, int(os.getenv("WEB_THREADS", "8"))),
        )
    else:
        app.run(
            host=host,
            port=port,
            debug=False,
        )
