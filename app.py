import re
import hmac
import sqlite3
from datetime import datetime
from werkzeug.security import generate_password_hash
from flask import render_template, abort, redirect, url_for

import os
from pathlib import Path

from flask import Flask, request, jsonify, current_app, session
from dotenv import load_dotenv
from werkzeug.middleware.proxy_fix import ProxyFix
from flask_wtf.csrf import CSRFProtect

load_dotenv(Path(__file__).resolve().parent / ".env", override=False)

from config import Config
from database import create_tables, get_db
from routes.push import push_bp
from utils.push_notifications import ensure_push_schema
from utils.app_persistent_sessions import (
    AppAwareSecureCookieSessionInterface,
    after_app_request,
    before_app_request,
    ensure_app_session_schema,
)
from utils.admin_mfa import configure_admin_mfa_encryption, ensure_admin_security_schema

# ==========================
# Blueprints
# ==========================

from routes.auth import auth_bp
from routes.worker import worker_bp
from routes.partner import partner_bp
from routes.admin import admin_bp, _ensure_admin_catalog_schema, ADMIN_PANEL_BASE
from routes.customer import customer_bp

# ==========================
# Flask App
# ==========================

app = Flask(__name__)
app.config.from_object(Config)
# Customer, worker and partner sessions are long-lived and server-revocable.
# Admin keeps its separate short MFA session policy.
app.session_interface = AppAwareSecureCookieSessionInterface()
app.before_request(before_app_request)
app.after_request(after_app_request)

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


@app.route(f"{ADMIN_PANEL_BASE}/bootstrap", methods=["GET", "POST"])
def temporary_admin_setup():
    """One-time, secret-protected admin bootstrap; remove after use."""
    setup_key = os.getenv("ADMIN_SETUP_KEY", "").strip()

    if (
        len(setup_key) < 32
        or setup_key.lower().startswith(("replace-", "change-me"))
    ):
        abort(404)

    conn = get_db()

    try:
        # ÓñòÓÑçÓñÁÓñ▓ Óñ¬Óñ╣Óñ▓ÓÑç Admin account ÓñòÓÑç Óñ▓Óñ┐ÓñÅÓÑñ
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


@app.get("/")
def customer_entrypoint():
    """Send the public Sivdhi URL to the public customer home page."""
    return redirect(url_for("customer.home"))


@app.get("/robots.txt")
def robots_txt():
    """Publish crawler rules for public pages only."""
    content = (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /admin\n"
        "Disallow: /customer/login\n"
        "Disallow: /customer/signup\n"
        "Disallow: /customer/bookings\n"
        "Disallow: /customer/profile\n"
        "Sitemap: https://sivdhi.duckdns.org/sitemap.xml\n"
    )
    return current_app.response_class(content, mimetype="text/plain")


@app.get("/sitemap.xml")
def sitemap_xml():
    """Expose the public customer page and role-specific login pages."""
    from xml.sax.saxutils import escape

    public_urls = (
        "https://sivdhi.duckdns.org/customer/",
        "https://sivdhi.duckdns.org/login?role=worker",
        "https://sivdhi.duckdns.org/login?role=partner",
    )
    last_modified = datetime.utcnow().date().isoformat()
    url_entries = "".join(
        f'<url><loc>{escape(public_url)}</loc>'
        f'<lastmod>{last_modified}</lastmod>'
        '<changefreq>weekly</changefreq><priority>0.7</priority></url>'
        for public_url in public_urls
    )
    content = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f'{url_entries}'
        '</urlset>'
    )
    return current_app.response_class(
        content,
        mimetype="application/xml",
    )


@app.get("/worker-partner/login")
def worker_partner_entrypoint():
    """Canonical entry point for the shared Worker/Partner Android app."""
    return redirect(url_for("auth.choose_role"))


@app.get("/worker/login")
def worker_entrypoint():
    return redirect(url_for("auth.login", role="worker"))


@app.get("/partner/login")
def partner_entrypoint():
    return redirect(url_for("auth.login", role="partner"))


@app.after_request
def add_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    role = (request.args.get("role") or "").strip().lower()
    is_public_role_login = request.path == "/login" and role in {"worker", "partner"}
    is_private_worker_partner_page = (
        request.path.startswith(("/worker/", "/partner/"))
        and request.path not in {"/worker/login", "/partner/login"}
    )
    if (
        request.blueprint == "admin"
        or request.path == f"{ADMIN_PANEL_BASE}/bootstrap"
        or request.path in {"/customer/login", "/customer/signup", "/signup"}
        or (request.path == "/login" and not is_public_role_login)
        or is_private_worker_partner_page
    ):
        response.headers.setdefault("X-Robots-Tag", "noindex, nofollow, noarchive")
    if request.blueprint == "admin" or request.path == f"{ADMIN_PANEL_BASE}/bootstrap":
        response.headers.setdefault("Cache-Control", "no-store, max-age=0, private")
        response.headers.setdefault("Pragma", "no-cache")
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


@app.after_request
def add_public_sivdhi_footer(response):
    """Show consistent owner, contact, social and legal links across site pages."""
    if (
        response.mimetype != "text/html"
        or response.status_code < 200
        or response.status_code >= 300
        or response.is_streamed
        or request.blueprint == "admin"
        or request.path == f"{ADMIN_PANEL_BASE}/bootstrap"
    ):
        return response

    try:
        html = response.get_data(as_text=True)
    except (RuntimeError, UnicodeDecodeError):
        return response

    # The customer landing page already contains this exact shared section.
    if 'class="public-contact"' in html or "</body>" not in html.lower():
        return response

    footer = """
    <style>
      .public-contact{margin:34px 0 0;padding:30px 18px 24px;color:#f8fafc;background:#171329;border-radius:24px 24px 0 0}
      .public-contact-inner{width:100%;max-width:1040px;margin:0 auto}
      .public-contact-kicker{margin:0 0 8px;color:#c4b5fd;font-size:10px;font-weight:800;letter-spacing:1.5px}
      .public-contact h2{margin:0 0 9px;font-size:21px}
      .public-contact-description{max-width:620px;margin:0 0 20px;color:#cbd5e1;font-size:13px;line-height:1.7}
      .public-contact-details{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin-bottom:22px}
      .public-contact-details div{min-width:0;padding:13px;background:rgba(255,255,255,.07);border:1px solid rgba(255,255,255,.1);border-radius:13px}
      .public-contact-details span{display:block;margin-bottom:6px;color:#c4b5fd;font-size:11px}
      .public-contact-details strong,.public-contact-details a{overflow-wrap:anywhere;color:#fff;font-size:13px;font-weight:700;text-decoration:none}
      .public-contact h3{margin:0 0 10px;font-size:14px}
      .public-social-links{display:flex;flex-wrap:wrap;gap:9px;margin-bottom:20px}
      .public-social-links a{display:inline-flex;flex-direction:column;gap:3px;padding:10px 13px;color:#fff;background:rgba(255,255,255,.08);border:1px solid rgba(255,255,255,.12);border-radius:11px;text-decoration:none;font-size:12px;font-weight:700}
      .public-social-links span{color:#c4b5fd;font-size:10px;font-weight:500}
      .public-policy-links{display:flex;flex-wrap:wrap;gap:16px;padding-top:15px;border-top:1px solid rgba(255,255,255,.14)}
      .public-policy-links a{color:#ddd6fe;font-size:12px;text-decoration:none}
      .public-contact-note{margin:18px 0 0;color:#a1a1aa;font-size:10px}
      @media(max-width:600px){.public-contact-details{grid-template-columns:1fr}.public-contact{padding:25px 15px 22px}}
    </style>
    <section class="public-contact" aria-labelledby="publicContactHeading">
      <div class="public-contact-inner">
        <p class="public-contact-kicker">OFFICIAL WEBSITE</p>
        <h2 id="publicContactHeading">Contact &amp; Company Information</h2>
        <p class="public-contact-description">Sivdhi is operated by Shivam Sahu. For business, booking or platform-related questions, contact us using the details below.</p>
        <div class="public-contact-details">
          <div><span>Legal / Owner Name</span><strong>Shivam Sahu</strong></div>
          <div><span>Email</span><a href="mailto:shivamsahu.co@gmail.com">shivamsahu.co@gmail.com</a></div>
          <div><span>Phone</span><a href="tel:9286557611">9286557611</a></div>
        </div>
        <h3>Follow Sivdhi</h3>
        <div class="public-social-links">
          <a href="https://www.instagram.com/sivdhiapp?dlrf=MWo4OWw0dmdwZG50eA==" target="_blank" rel="noopener noreferrer">Instagram <span>@sivdhiapp</span></a>
          <a href="https://www.facebook.com/share/1ALe4RqBKM/" target="_blank" rel="noopener noreferrer">Facebook <span>Sivdhi</span></a>
          <a href="https://www.threads.com/@sivdhiapp" target="_blank" rel="noopener noreferrer">Threads <span>@sivdhiapp</span></a>
        </div>
        <nav class="public-policy-links" aria-label="Legal information">
          <a href="/customer/about">About Sivdhi</a>
          <a href="/customer/privacy">Privacy Policy</a>
          <a href="/customer/terms">Terms &amp; Conditions</a>
        </nav>
        <p class="public-contact-note">© 2026 Sivdhi. All rights reserved.</p>
      </div>
    </section>
    """;
    closing_body = html.lower().rfind("</body>")
    html = html[:closing_body] + footer + html[closing_body:]
    response.set_data(html);
    return response


app.secret_key = Config.SECRET_KEY


# ==========================
# Database initialization
# ==========================

# Create base tables first.
create_tables()

# Admin MFA uses an independent persistent encryption key and additive tables.
# In production the key must be supplied through ADMIN_MFA_ENCRYPTION_KEY.
configure_admin_mfa_encryption()
_admin_security_conn = get_db()
try:
    ensure_admin_security_schema(_admin_security_conn)
    _admin_security_conn.commit()
finally:
    _admin_security_conn.close()

# Server-side revocation ledger used only by the native Android app sessions.
_app_session_schema_conn = get_db()
try:
    ensure_app_session_schema(_app_session_schema_conn)
    _app_session_schema_conn.commit()
finally:
    _app_session_schema_conn.close()

# Add push-token and transactional outbox tables without altering existing users.
_push_schema_conn = get_db()
try:
    ensure_push_schema(_push_schema_conn)
    _push_schema_conn.commit()
finally:
    _push_schema_conn.close()

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

        return {
            "service_categories_global": rows,
            "admin_base_path": ADMIN_PANEL_BASE,
        }

    except Exception:
        current_app.logger.exception(
            "Could not load global service categories"
        )
        return {
            "service_categories_global": [],
            "admin_base_path": ADMIN_PANEL_BASE,
        }

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
app.register_blueprint(push_bp)


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
