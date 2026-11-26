from datetime import datetime, timezone
import hashlib
import hmac
import os
import re
from pathlib import Path

from flask import (
    Flask, request, jsonify, current_app, session, redirect, url_for,
    render_template, flash,
)
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
from routes.customer import customer_bp, customer_service_areas, list_customer_catalogue
from routes.cashfree_webhook import cashfree_webhook_bp

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


@app.get("/favicon.ico")
def favicon():
    """Serve the existing Sivdhi logo as the site-wide browser favicon."""
    return current_app.send_static_file("pwa/customer/icon-512.png")


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
    """Serve the same public customer homepage at the domain root."""
    return current_app.view_functions["customer.home"]()


@app.get("/partner-opportunity")
def partner_opportunity():
    """Public information page for Sivdhi Partner role."""
    return render_template("partner_opportunity.html")

@app.get("/robots.txt")
def robots_txt():
    """Publish crawler rules for public pages only."""
    content = (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /admin\n"
        "Sitemap: https://sivdhi.duckdns.org/sitemap.xml\n"
    )
    return current_app.response_class(content, mimetype="text/plain")


@app.get("/sitemap.xml")
def sitemap_xml():
    """Publish canonical, indexable public pages and data-backed service URLs."""
    from xml.sax.saxutils import escape

    public_urls = [
        "https://sivdhi.duckdns.org/",
        "https://sivdhi.duckdns.org/partner-opportunity",
        "https://sivdhi.duckdns.org/customer/reviews",
        "https://sivdhi.duckdns.org/customer/about",
        "https://sivdhi.duckdns.org/customer/faq",
        "https://sivdhi.duckdns.org/customer/help",
        "https://sivdhi.duckdns.org/worker/about",
        "https://sivdhi.duckdns.org/partner/about",
    ]

    db = get_db()
    try:
        # Category URLs are public catalogue pages with real, priced services.
        categories = list_customer_catalogue(db, mode="normal")
        public_urls.extend(
            f"https://sivdhi.duckdns.org/customer/category/{int(item['id'])}"
            for item in categories
            if item.get("id")
        )

        # Only publish the service-area hub and city pages backed by active
        # worker profiles whose listed skills match live customer categories.
        areas = customer_service_areas(db)
        if areas:
            public_urls.append(
                "https://sivdhi.duckdns.org/customer/service-areas"
            )
            public_urls.extend(
                "https://sivdhi.duckdns.org/customer/service-areas/"
                + item["slug"]
                for item in areas
            )
            for area in areas:
                public_urls.extend(
                    "https://sivdhi.duckdns.org/customer/service-areas/"
                    + area["slug"] + "/" + category["slug"]
                    for category in area["categories"]
                )
    finally:
        db.close()

    url_entries = "".join(
        f'<url><loc>{escape(public_url)}</loc></url>'
        for public_url in sorted(set(public_urls))
    )
    content = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f'{url_entries}'
        '</urlset>'
    )
    return current_app.response_class(content, mimetype="application/xml")


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


def _record_role_signup_consent(role, email, mobile):
    """Persist consent with a keyed identifier hash; never store signup contact details."""
    if not email or not mobile:
        raise ValueError("Email and mobile are required to record signup consent.")

    identity = "|".join((role, email, mobile))
    secret = current_app.secret_key
    secret_bytes = secret.encode("utf-8") if isinstance(secret, str) else secret
    identity_hash = hmac.new(
        secret_bytes,
        identity.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    db = get_db()
    try:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS role_signup_consents (
                role TEXT NOT NULL CHECK (role IN ('worker', 'partner')),
                identity_hash TEXT NOT NULL,
                terms_version TEXT NOT NULL,
                privacy_policy_version TEXT NOT NULL,
                accepted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (role, identity_hash)
            )
            """
        )
        db.execute(
            """
            INSERT INTO role_signup_consents (
                role, identity_hash, terms_version, privacy_policy_version,
                accepted_at
            )
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(role, identity_hash) DO UPDATE SET
                terms_version=excluded.terms_version,
                privacy_policy_version=excluded.privacy_policy_version,
                accepted_at=CURRENT_TIMESTAMP
            """,
            (role, identity_hash, "2026-11-26", "2026-11-26"),
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@app.before_request
def require_worker_partner_signup_consent():
    """Enforce and record consent before the shared signup handler can create an account."""
    if request.endpoint != "auth.signup" or request.method != "POST":
        return None

    role = (request.args.get("role") or request.form.get("role") or "").strip().lower()
    if role not in {"worker", "partner"}:
        return None

    if request.form.get("terms") != "accepted":
        flash(
            "Please agree to the Terms & Conditions and Privacy Policy before creating your account.",
            "warning",
        )
        return redirect(url_for("auth.signup", role=role))

    email = (request.form.get("email") or "").strip().lower()
    mobile = "".join(
        character for character in (request.form.get("mobile") or "")
        if character.isdigit()
    )
    if email and mobile:
        try:
            _record_role_signup_consent(role, email, mobile)
        except Exception:
            current_app.logger.exception("Could not save Worker/Partner signup consent")
            return current_app.response_class(
                "We could not record your consent. Please try again.",
                status=503,
                mimetype="text/plain",
            )
    return None


def _inject_sivdhi_site_footer(response):
    """Add the shared role-aware footer to every public website HTML page."""
    if (
        response.mimetype != "text/html"
        or response.status_code != 200
        or request.method != "GET"
        or response.is_streamed
    ):
        return response

    admin_base = str(ADMIN_PANEL_BASE or "").rstrip("/")
    if admin_base and (
        request.path == admin_base
        or request.path.startswith(admin_base + "/")
    ):
        return response

    page_html = response.get_data()
    has_footer = re.search(
        rb"<footer\b[^>]*\bsivdhi-site-footer\b",
        page_html,
        flags=re.IGNORECASE,
    ) is not None

    # Keep the correct Company links for the active website role.
    path = request.path or "/"
    requested_role = (request.args.get("role") or "").strip().lower()
    if path.startswith("/worker/"):
        footer_role = "worker"
    elif path.startswith("/partner/") or path == "/partner-opportunity":
        footer_role = "partner"
    elif requested_role in {"worker", "partner"}:
        footer_role = requested_role
    else:
        footer_role = "customer"

    if not has_footer:
        try:
            footer_html = current_app.jinja_env.get_template(
                "_sivdhi_footer.html"
            ).render(
                sivdhi_footer_role=footer_role,
                sivdhi_show_customer_support=(
                    footer_role == "customer"
                    and request.endpoint == "customer.home"
                ),
            ).encode("utf-8")
            page_html, inserted_footer = re.subn(
                rb"</body\s*>",
                footer_html + b"\n</body>",
                page_html,
                count=1,
                flags=re.IGNORECASE,
            )
            if not inserted_footer:
                return response
        except Exception:
            current_app.logger.exception(
                "Could not add the shared Sivdhi footer to %s",
                request.path,
            )
            return response

    footer_layout_style = b"""<style id="sivdhi-global-footer-layout">
html { min-height: 100%; }
body {
    min-height: 100vh !important;
    min-height: 100dvh !important;
    display: flex !important;
    flex-direction: column !important;
}
.sivdhi-site-footer {
    flex: 0 0 auto;
    margin-top: auto !important;
}
body:not(:has(.bottom-nav)):not(:has(nav.bottom)):not(:has(div.bottom a[href="/partner/home"])) .sivdhi-site-footer {
    margin-bottom: 20px !important;
}
</style>"""
    if b'id="sivdhi-global-footer-layout"' not in page_html:
        page_html, inserted_style = re.subn(
            rb"(<head\b[^>]*>)",
            lambda match: match.group(1) + b"\n" + footer_layout_style,
            page_html,
            count=1,
            flags=re.IGNORECASE,
        )
        if not inserted_style:
            current_app.logger.warning(
                "Could not add global footer layout styles to %s",
                request.path,
            )

    response.set_data(page_html)
    return response


@app.after_request
def add_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    public_customer_endpoints = {
        "customer.home",
        "customer.reviews",
        "customer.about",
        "customer.faq",
        "customer.help",
        "customer.category",
        "customer.service_areas",
    }
    public_worker_partner_paths = {
        "/worker/about",
        "/partner/about",
    }
    is_private_worker_partner_page = (
        request.path.startswith(("/worker/", "/partner/"))
        and request.path not in {"/worker/login", "/partner/login"}
        and request.path not in public_worker_partner_paths
    )
    is_private_customer_page = (
        request.blueprint == "customer"
        and request.endpoint not in public_customer_endpoints
    )
    is_role_signup = request.endpoint == "auth.signup"
    is_sensitive_auth_flow = request.endpoint in {
        "auth.forgot_password",
        "auth.verify_otp",
        "auth.new_password",
    }
    if (
        request.blueprint == "admin"
        or request.path == "/customer/login"
        or is_sensitive_auth_flow
        or request.path == "/login"
        or is_role_signup
        or is_private_worker_partner_page
        or is_private_customer_page
    ):
        response.headers.setdefault("X-Robots-Tag", "noindex, nofollow, noarchive")
    if request.blueprint == "admin":
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
        "frame-src 'self' https://sdk.cashfree.com https://api.cashfree.com "
        "https://sandbox.cashfree.com https://payments.cashfree.com "
        "https://payments-test.cashfree.com; "
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

    # Apply the same Sivdhi browser favicon to every HTML page.
    # Keep apple-touch-icon and manifest links unchanged.
    if response.mimetype == "text/html" and not response.is_streamed:
        import re

        html = response.get_data()

        def remove_existing_favicon(match):
            tag = match.group(0)
            rel_match = re.search(
                rb"""\brel\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""",
                tag,
                re.IGNORECASE,
            )
            if rel_match:
                rel_value = next(
                    (value for value in rel_match.groups() if value is not None),
                    b"",
                )
                if b"icon" in rel_value.lower().split():
                    return b""
            return tag

        # Remove old tab icons so different pages cannot override Sivdhi.
        html = re.sub(
            rb"<link\b[^>]*>",
            remove_existing_favicon,
            html,
            flags=re.IGNORECASE,
        )

        favicon_link = (
            b'<link rel="icon" type="image/png" sizes="512x512" '
            b'href="/favicon.ico?v=20261121-2">'
        )
        html, inserted = re.subn(
            rb"(<head\b[^>]*>)",
            lambda match: match.group(1) + b"\n    " + favicon_link,
            html,
            count=1,
            flags=re.IGNORECASE,
        )

        if inserted:
            response.set_data(html)


    # GA4 is restricted to explicitly approved public, anonymous pages.
    # Never add authenticated account, booking, worker, partner or admin pages.
    ga4_measurement_id = current_app.config.get("GA4_MEASUREMENT_ID", "")
    analytics_public_path = (
        request.path in {
            "/",
            "/customer/",
            "/customer/home",
            "/customer/signup",
            "/customer/reviews",
            "/customer/about",
            "/customer/faq",
            "/customer/help",
            "/partner-opportunity",
        }
        or request.endpoint in {
            "customer.category",
            "customer.service_areas",
            "worker.about",
            "partner.about",
        }
        or (
            request.path in {"/login", "/signup"}
            and (request.args.get("role") or "").strip().lower()
            in {"worker", "partner"}
        )
    )
    private_session_keys = {
        "user_id", "customer_id", "worker_id", "partner_id", "admin_id",
        "customer_logged_in", "worker_logged_in", "partner_logged_in",
        "admin_logged_in", "saved_user_id", "customer_code", "customer_name",
        "customer_email", "customer_mobile", "customer_latitude",
        "customer_longitude", "customer_location_name", "last_booking_id",
        "selected_service_id", "selected_service_name", "selected_category",
        "selected_category_id", "selected_problem_ids",
        "selected_problems_snapshot", "booking_date", "booking_time",
        "booking_description", "customer_password_reset_verified",
    }
    has_private_session = (
        any(
            session.get(key) not in (None, "", False, [], {})
            for key in private_session_keys
        )
        or any(key.startswith("pending_") for key in session)
    )

    if (
        ga4_measurement_id
        and analytics_public_path
        and request.method == "GET"
        and response.status_code < 400
        and response.mimetype == "text/html"
        and not response.is_streamed
        and not has_private_session
    ):
        # The ID is validated in Config. URLs are reduced to origin/path and
        # referrers to origin only, so query strings are never sent by this tag.
        analytics_tag = (
            '<script async src="https://www.googletagmanager.com/gtag/js?id='
            + ga4_measurement_id
            + '"></script>'
            + '<script>(function(){'
            + 'var pageLocation=window.location.origin+window.location.pathname;'
            + 'var pageReferrer="";'
            + 'try{if(document.referrer){pageReferrer=new URL(document.referrer).origin;}}catch(e){}'
            + 'window.dataLayer=window.dataLayer||[];'
            + 'function gtag(){window.dataLayer.push(arguments);}'
            + "gtag('js',new Date());"
            + "gtag('config','" + ga4_measurement_id + "',{"
            + 'page_location:pageLocation,'
            + 'page_path:window.location.pathname,'
            + 'page_title:document.title,'
            + 'page_referrer:pageReferrer,'
            + 'allow_google_signals:false,'
            + 'allow_ad_personalization_signals:false'
            + '});'
            + '})();</script>'
        ).encode("utf-8")
        page_html = response.get_data()
        page_html, analytics_inserted = re.subn(
            rb"(<head\b[^>]*>)",
            lambda match: match.group(1) + b"\n    " + analytics_tag,
            page_html,
            count=1,
            flags=re.IGNORECASE,
        )
        if analytics_inserted:
            response.set_data(page_html)

    return _inject_sivdhi_site_footer(response)



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
app.register_blueprint(cashfree_webhook_bp)
# Only the dedicated provider webhook endpoint is exempt from browser CSRF tokens.
csrf.exempt(cashfree_webhook_bp)
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
