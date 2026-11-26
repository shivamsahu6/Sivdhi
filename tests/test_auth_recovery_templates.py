"""Regression checks for recovered auth blueprint and role-specific OTP templates."""
from pathlib import Path
import ast
import sqlite3

ROOT = Path(__file__).resolve().parents[1]

def test_auth_bytecode_backup_is_present_and_python314_bound():
    artifact = ROOT / "routes" / "_auth_recovered.pyc"
    data = artifact.read_bytes()
    assert len(data) > 10_000
    assert data[:4] == bytes.fromhex("2b0e0d0a")
    tree = ast.parse((ROOT / "routes" / "auth.py").read_text(encoding="utf-8"))
    declared = False
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__auth_binary_endpoints__" for t in node.targets):
            declared = isinstance(node.value, ast.Tuple) and {e.value for e in node.value.elts if isinstance(e, ast.Constant)} >= {"login", "signup", "logout", "forgot_password", "verify_otp", "new_password"}
    assert declared
    assert "SourcelessFileLoader" in (ROOT / "routes" / "auth.py").read_text(encoding="utf-8")

def test_forgot_password_dispatches_to_each_role_page():
    template = (ROOT / "templates" / "forgot_password.html").read_text(encoding="utf-8")
    for expected in ("customer/forgot_password.html", "worker/forgot_password.html", "partner/forgot_password.html"):
        assert expected in template

def test_verify_otp_dispatches_to_each_role_page():
    template = (ROOT / "templates" / "verify_otp.html").read_text(encoding="utf-8")
    for expected in ("customer/verify_password_otp.html", "worker/verify_forgot_password_otp.html", "partner/verify_forgot_password_otp.html"):
        assert expected in template

def test_worker_and_partner_password_reset_otp_forms_post_with_csrf():
    for role in ("worker", "partner"):
        text = (ROOT / "templates" / role / "verify_forgot_password_otp.html").read_text(encoding="utf-8")
        assert "url_for('auth.verify_otp')" in text
        assert 'name="csrf_token"' in text
        assert 'name="otp"' in text
        assert 'pattern="[0-9]{6}"' in text
        assert f"role='{role}'" in text

def test_public_entrypoint_and_sitemap_advertise_customer_worker_and_partner_pages():
    import xml.etree.ElementTree as ET

    from app import app

    client = app.test_client()
    root_response = client.get("/", follow_redirects=False)
    assert root_response.status_code == 200
    assert root_response.headers.get("Location") is None
    root_page = root_response.get_data(as_text=True)
    assert "<title>Sivdhi | Home Services &amp; Online Booking</title>" in root_page
    assert '<link rel="canonical" href="https://sivdhi.duckdns.org/">' in root_page
    assert '<meta property="og:site_name" content="Sivdhi">' in root_page
    assert '"@type": "WebSite"' in root_page
    assert '"name": "Sivdhi"' in root_page
    assert '"telephone": "+91 89540 15683"' in root_page
    assert '"areaServed": "Bareilly, Uttar Pradesh, India"' in root_page

    sitemap_response = client.get("/sitemap.xml")
    assert sitemap_response.status_code == 200

    document = ET.fromstring(sitemap_response.data)
    namespace = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    locations = {
        node.text
        for node in document.findall(".//sm:loc", namespace)
    }
    expected_public_pages = {
        "https://sivdhi.duckdns.org/",
        "https://sivdhi.duckdns.org/partner-opportunity",
        "https://sivdhi.duckdns.org/customer/reviews",
        "https://sivdhi.duckdns.org/customer/about",
        "https://sivdhi.duckdns.org/customer/faq",
        "https://sivdhi.duckdns.org/customer/help",
        "https://sivdhi.duckdns.org/worker/about",
        "https://sivdhi.duckdns.org/partner/about",
    }
    assert expected_public_pages <= locations
    assert not any("/signup" in location or "/login" in location for location in locations)
    assert not any(
        "/customer/bookings" in location
        or "/customer/profile" in location
        or "/worker/settings" in location
        or "/partner/settings" in location
        for location in locations
    )

    robots_response = client.get("/robots.txt")
    assert robots_response.status_code == 200
    robots = robots_response.get_data(as_text=True)
    assert "Disallow: /admin" in robots
    # Private pages must remain crawlable so Google can see their noindex headers.
    assert "Disallow: /customer/bookings" not in robots
    assert "Disallow: /customer/profile" not in robots
    assert "Disallow: /customer/login" not in robots
    assert "Disallow: /customer/signup" not in robots


def test_worker_and_partner_signup_pages_require_consent_and_are_not_search_landing_pages():
    from app import app

    client = app.test_client()
    for role, heading, tagline in (
        (
            "worker",
            "Sivdhi Worker",
            "Find Service Jobs, Manage Bookings and Track Your Work.",
        ),
        (
            "partner",
            "Sivdhi Partner",
            "Manage Workers, Coordinate Service Bookings and Track Partner Activities.",
        ),
    ):
        response = client.get(f"/login?role={role}")
        assert response.status_code == 200
        assert response.headers.get("X-Robots-Tag") == "noindex, nofollow, noarchive"
        page = response.get_data(as_text=True)
        assert '<meta name="robots" content="noindex, nofollow, noarchive">' in page
        assert heading in page
        assert tagline in page
        assert "Forgot Password?" in page
        assert "Create Account" in page

        signup = client.get(f"/signup?role={role}")
        assert signup.status_code == 200
        assert signup.headers.get("X-Robots-Tag") == "noindex, nofollow, noarchive"
        signup_page = signup.get_data(as_text=True)
        assert '<meta name="robots" content="noindex, follow">' in signup_page
        assert "<h1>" in signup_page
        assert "Create Account" in signup_page
        assert 'name="terms"' in signup_page
        assert 'value="accepted"' in signup_page
        assert 'required>' in signup_page
        if role == "worker":
            assert 'href="/worker/terms"' in signup_page
            assert 'href="/worker/privacy_policy"' in signup_page
        else:
            assert 'href="/partner/terms"' in signup_page
            assert 'href="/partner/privacy"' in signup_page

        forgot = client.get(f"/forgot-password?role={role}")
        assert forgot.headers.get("X-Robots-Tag") == "noindex, nofollow, noarchive"

        verify = client.get(f"/verify-otp?role={role}")
        assert verify.headers.get("X-Robots-Tag") == "noindex, nofollow, noarchive"


def test_customer_private_pages_and_thin_signup_are_noindex_but_home_is_indexable():
    from app import app

    client = app.test_client()
    expected_robots = "noindex, nofollow, noarchive"

    # The public customer homepage must remain indexable.
    for path in ("/", "/customer/", "/customer/home"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers.get("X-Robots-Tag") is None
        assert '<meta name="robots" content="index, follow, max-image-preview:large">' in response.get_data(as_text=True)

    # The main catalogue remains focused; recruitment and service-area links
    # are available in the footer rather than as extra homepage call-to-action tiles.
    home_page = client.get("/customer/").get_data(as_text=True)
    for label in (
        "Create Account",
        "Find Worker",
        "Join as Worker",
        "Become a Partner",
    ):
        assert label not in home_page
    assert 'href="/partner-opportunity"' in home_page
    assert 'href="/customer/service-areas"' in home_page

    # Registration is available to visitors but thin form-only pages stay noindex.
    signup_response = client.get("/customer/signup")
    assert signup_response.status_code == 200
    assert signup_response.headers.get("X-Robots-Tag") == expected_robots
    assert '<meta name="robots" content="noindex, follow">' in signup_response.get_data(as_text=True)

    # The public privacy policy can be read before registration, but is not a search landing page.
    privacy_response = client.get("/customer/privacy")
    assert privacy_response.status_code == 200
    assert privacy_response.headers.get("X-Robots-Tag") == expected_robots

    # Protected customer pages remain noindex even on login redirects.
    for path in (
        "/customer/login",
        "/customer/bookings",
        "/customer/profile",
        "/customer/edit-profile",
    ):
        response = client.get(path, follow_redirects=False)
        assert response.headers.get("X-Robots-Tag") == expected_robots


def test_partner_opportunity_page_is_public_indexable_and_explains_commission_terms():
    from app import app

    client = app.test_client()
    response = client.get("/partner-opportunity")
    assert response.status_code == 200
    assert response.headers.get("X-Robots-Tag") is None
    page = response.get_data(as_text=True)
    assert '<meta name="robots" content="index, follow, max-image-preview:large">' in page
    assert '<link rel="canonical" href="https://sivdhi.duckdns.org/partner-opportunity">' in page
    assert "Become a Sivdhi Partner" in page
    assert "mobile phone or laptop" in page
    assert "commission-based" in page
    assert "not a fixed-salary job" in page
    assert "guaranteed monthly income" in page


def test_ga4_tracks_only_allowlisted_anonymous_public_pages(monkeypatch):
    from app import app

    client = app.test_client()
    monkeypatch.setitem(app.config, "GA4_MEASUREMENT_ID", "G-R0RQ9GKPGE")

    # Track public homepage and anonymous login/signup page views.
    allowed_pages = (
        "/customer/",
        "/customer/home?mode=full-day&email=private@example.com",
        "/customer/signup",
        "/customer/reviews",
        "/customer/about",
        "/customer/faq",
        "/customer/help",
        "/worker/about",
        "/partner/about",
        "/partner-opportunity",
        "/login?role=worker",
        "/login?role=partner",
        "/signup?role=worker",
        "/signup?role=partner",
    )
    for path in allowed_pages:
        response = client.get(path)
        assert response.status_code == 200
        page = response.get_data(as_text=True)
        assert "https://www.googletagmanager.com/gtag/js?id=G-R0RQ9GKPGE" in page
        assert "page_location:pageLocation" in page
        assert "window.location.origin+window.location.pathname" in page
        assert "document.referrer).origin" in page
        assert "email=private@example.com" not in page

    # Do not track non-public and protected routes.
    private_or_non_public = (
        "/customer/bookings",
        "/customer/profile",
        "/customer/privacy",
        "/customer/terms",
        "/login",
        "/signup",
        "/worker/login",
        "/partner/login",
    )
    for path in private_or_non_public:
        response = client.get(path, follow_redirects=False)
        assert "googletagmanager.com/gtag/js" not in response.get_data(as_text=True)

    # Authenticated customer session must remain excluded even on the homepage.
    with client.session_transaction() as session:
        session["customer_id"] = 999999
    response = client.get("/customer/")
    assert response.status_code in {200, 302}
    assert "googletagmanager.com/gtag/js" not in response.get_data(as_text=True)

    # Other account-related session fields are also a privacy exclusion.
    with client.session_transaction() as session:
        session.clear()
        session["user_id"] = 123
    response = client.get("/login?role=worker")
    assert "googletagmanager.com/gtag/js" not in response.get_data(as_text=True)

    # Invalid/empty configuration must leave Analytics completely disabled.
    with client.session_transaction() as session:
        session.clear()
    monkeypatch.setitem(app.config, "GA4_MEASUREMENT_ID", "")
    response = client.get("/customer/")
    assert response.status_code == 200
    assert "googletagmanager.com/gtag/js" not in response.get_data(as_text=True)




def test_customer_homepage_has_focused_search_metadata():
    from app import app

    client = app.test_client()
    for path in ("/", "/customer/", "/customer/home"):
        response = client.get(path)
        assert response.status_code == 200
        page = response.get_data(as_text=True)
        assert "<title>Sivdhi | Home Services &amp; Online Booking</title>" in page
        assert (
            'name="description" content="Find local home services with Sivdhi. Browse listed categories, '
            'compare displayed prices and check availability for your location before requesting a booking."'
        ) in page
        assert '<link rel="canonical" href="https://sivdhi.duckdns.org/">' in page
        assert '<meta property="og:site_name" content="Sivdhi">' in page
        assert "Sivdhi Sivdhi" not in page



def test_public_customer_and_role_information_pages_are_indexable_with_metadata():
    from app import app

    client = app.test_client()
    for path, canonical in (
        ("/customer/reviews", "https://sivdhi.duckdns.org/customer/reviews"),
        ("/customer/about", "https://sivdhi.duckdns.org/customer/about"),
        ("/customer/faq", "https://sivdhi.duckdns.org/customer/faq"),
        ("/customer/help", "https://sivdhi.duckdns.org/customer/help"),
        ("/worker/about", "https://sivdhi.duckdns.org/worker/about"),
        ("/partner/about", "https://sivdhi.duckdns.org/partner/about"),
    ):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers.get("X-Robots-Tag") is None
        page = response.get_data(as_text=True)
        assert '<meta name="description"' in page
        assert canonical in page


def test_settings_links_are_removed_from_worker_and_partner_profile_menus():
    worker = (ROOT / "templates" / "worker" / "profile.html").read_text(encoding="utf-8")
    partner = (ROOT / "templates" / "partner" / "profile.html").read_text(encoding="utf-8")
    assert "worker.worker_settings" not in worker
    assert 'href="/partner/settings"' not in partner
    # Existing settings routes/templates stay intact; only their profile-menu links are removed.
    assert (ROOT / "templates" / "worker" / "settings.html").is_file()
    assert (ROOT / "templates" / "partner" / "settings.html").is_file()


def test_worker_partner_signup_post_is_blocked_without_explicit_consent(monkeypatch):
    from app import app

    client = app.test_client()
    monkeypatch.setitem(app.config, "WTF_CSRF_ENABLED", False)
    original = app.view_functions["auth.signup"]

    def should_not_reach_signup_handler():
        raise AssertionError("Signup handler must not run without required consent.")

    app.view_functions["auth.signup"] = should_not_reach_signup_handler
    try:
        for role in ("worker", "partner"):
            response = client.post(
                f"/signup?role={role}",
                data={
                    "fullname": "Test User",
                    "email": f"{role}@example.test",
                    "mobile": "9999999999",
                    "password": "TestPassword123!",
                    "confirm_password": "TestPassword123!",
                },
                follow_redirects=False,
            )
            assert response.status_code == 302
            assert f"/signup?role={role}" in response.headers.get("Location", "")
    finally:
        app.view_functions["auth.signup"] = original



def test_public_city_service_pages_only_come_from_active_worker_skill_matches():
    from routes.customer import customer_service_areas

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    try:
        db.executescript(
            """
            CREATE TABLE service_categories (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                normal_enabled INTEGER NOT NULL DEFAULT 1,
                is_active INTEGER NOT NULL DEFAULT 1,
                icon TEXT,
                full_day_price REAL,
                included_work TEXT,
                excluded_work TEXT
            );
            CREATE TABLE services (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                category TEXT NOT NULL,
                price REAL NOT NULL,
                active INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE workers (
                id INTEGER PRIMARY KEY,
                city TEXT,
                state TEXT,
                skills TEXT,
                status TEXT,
                is_deleted INTEGER DEFAULT 0
            );
            INSERT INTO service_categories
                (id, name, normal_enabled, is_active)
                VALUES (1, 'Plumbing', 1, 1);
            INSERT INTO services
                (id, name, category, price, active)
                VALUES (1, 'Tap Repair', 'Plumbing', 499, 1);
            INSERT INTO workers
                (id, city, state, skills, status, is_deleted)
                VALUES
                (1, 'Bareilly', 'Uttar Pradesh', 'Plumber', 'Active', 0),
                (2, 'Rampur', 'Uttar Pradesh', 'Plumber', 'Suspended', 0);
            """
        )

        areas = customer_service_areas(db)
        assert len(areas) == 1
        assert areas[0]["city"] == "Bareilly"
        assert areas[0]["state"] == "Uttar Pradesh"
        assert areas[0]["slug"] == "bareilly-uttar-pradesh"
        assert areas[0]["categories"][0]["slug"] == "plumbing-1"
        assert areas[0]["categories"][0]["subcategories"][0]["name"] == "Tap Repair"
    finally:
        db.close()


def test_role_signup_consent_is_saved_without_plain_contact_details(monkeypatch, tmp_path):
    from app import app, _record_role_signup_consent
    from config import Config

    database_path = tmp_path / "role-consent.sqlite"
    monkeypatch.setattr(Config, "DATABASE", str(database_path))

    with app.app_context():
        _record_role_signup_consent(
            "worker",
            "worker@example.test",
            "9876543210",
        )

    connection = sqlite3.connect(database_path)
    try:
        columns = [
            row[1]
            for row in connection.execute("PRAGMA table_info(role_signup_consents)")
        ]
        assert "identity_hash" in columns
        assert "email" not in columns
        assert "mobile" not in columns
        row = connection.execute(
            "SELECT role, identity_hash, terms_version, privacy_policy_version, accepted_at "
            "FROM role_signup_consents"
        ).fetchone()
        assert row is not None
        assert row[0] == "worker"
        assert len(row[1]) == 64
        assert row[2] == "2026-11-26"
        assert row[3] == "2026-11-26"
        assert "worker@example.test" not in repr(row)
        assert "9876543210" not in repr(row)
    finally:
        connection.close()

def test_public_privacy_notice_describes_ga4_and_no_longer_calls_it_a_draft():
    from pathlib import Path

    policy = (
        Path(__file__).resolve().parents[1]
        / "templates"
        / "customer"
        / "privacy.html"
    ).read_text(encoding="utf-8")

    assert "This is a draft template" not in policy
    assert "This template is not a substitute" not in policy
    assert 'id="analytics"' in policy
    assert "Google Analytics 4" in policy
    assert "not active on Sivdhi Production" in policy
    assert "best-effort safeguards" in policy
    assert "does not currently publish one fixed retention" in policy
    assert "shivamsahu.co@gmail.com" in policy


def test_worker_partner_signup_footer_is_below_form_and_has_reviews_contact_links():
    from app import app

    signup_template = (ROOT / "templates" / "signup.html").read_text(encoding="utf-8")
    body_css = signup_template.split("body{", 1)[1].split("}", 1)[0]
    assert "min-height:100vh" in body_css
    assert "min-height:100dvh" in body_css
    assert "height:auto" in body_css
    assert "flex-direction:column" in body_css
    assert ".signup-box { flex: 0 0 auto; margin-bottom: auto; }" in signup_template
    assert ".sivdhi-site-footer { flex: 0 0 auto; margin: 30px auto 20px; }" in signup_template
    assert signup_template.index("{% include '_sivdhi_footer.html' %}") > signup_template.index("</div>\n<script>")

    client = app.test_client()
    for role, heading in (("worker", "For Worker"), ("partner", "For Partner")):
        response = client.get(f"/signup?role={role}")
        assert response.status_code == 200
        page = response.get_data(as_text=True)
        footer_start = page.index('<footer class="sivdhi-site-footer"')
        footer_end = page.index("</footer>", footer_start)
        footer = page[footer_start:footer_end]
        role_start = footer.index('aria-label="Role links"')
        role_end = footer.index("</section>", role_start)
        role_column = footer[role_start:role_end]
        assert heading in role_column
        assert 'href="/customer/reviews"' in role_column
        assert 'href="/customer/support"' in role_column


def test_worker_and_partner_forgot_password_forms_and_partner_login_are_role_wired():
    from app import app

    client = app.test_client()
    for role in ("worker", "partner"):
        login = client.get(f"/login?role={role}")
        assert login.status_code == 200
        login_html = login.get_data(as_text=True)
        assert 'name="user_id"' in login_html
        assert 'name="password"' in login_html
        assert f"/forgot-password?role={role}" in login_html
        assert f"/signup?role={role}" in login_html

        forgot = client.get(f"/forgot-password?role={role}")
        assert forgot.status_code == 200
        forgot_html = forgot.get_data(as_text=True)
        assert f"Forgot Password | Sivdhi {role.title()}" in forgot_html
        assert f'name="role" value="{role}"' in forgot_html
        assert 'name="csrf_token"' in forgot_html
        assert 'name="email"' in forgot_html
        assert "auth.forgot_password" in forgot_html

        verify = client.get(f"/verify-otp?role={role}")
        assert verify.status_code == 200
        verify_html = verify.get_data(as_text=True)
        assert "auth.verify_otp" in verify_html
        assert 'name="csrf_token"' in verify_html
        assert 'name="otp"' in verify_html
        assert 'pattern="[0-9]{6}"' in verify_html
        assert f"role='{role}'" in verify_html


def test_customer_home_requires_location_then_only_renders_area_categories(monkeypatch):
    from flask import session
    from app import app
    from routes import customer as customer_routes

    class FakeDB:
        def close(self):
            pass

    monkeypatch.setattr(customer_routes, "get_db", lambda: FakeDB())
    monkeypatch.setattr(customer_routes, "ensure_customer_schema", lambda _db: None)
    monkeypatch.setattr(customer_routes, "get_customer", lambda _db: None)
    monkeypatch.setattr(customer_routes, "customer_saved_coordinates", lambda _db: (None, None))
    monkeypatch.setattr(
        customer_routes,
        "list_customer_catalogue",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Signed-in customers must not receive the public catalogue.")
        ),
    )
    monkeypatch.setattr(
        customer_routes,
        "list_customer_categories",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Categories must wait until a location is selected.")
        ),
    )

    with app.test_request_context("/customer/"):
        session["customer_id"] = 42
        page = customer_routes.home()

    assert 'data-location-required="true"' in page
    assert 'id="locationState"' in page
    assert "Set your location first" in page
    assert "Share your location to see categories in your area" in page
    assert 'id="categoryGrid"' in page


def test_customer_home_uses_area_scoped_categories_after_location(monkeypatch):
    from flask import session
    from app import app
    from routes import customer as customer_routes

    class FakeDB:
        def close(self):
            pass

    area_categories = [{
        "id": 91,
        "name": "Area-only category",
        "display_name": "Area-only category",
        "description": "Available locally",
        "display_icon": "✦",
        "available_in_area": True,
    }]

    monkeypatch.setattr(customer_routes, "get_db", lambda: FakeDB())
    monkeypatch.setattr(customer_routes, "ensure_customer_schema", lambda _db: None)
    monkeypatch.setattr(customer_routes, "get_customer", lambda _db: None)
    monkeypatch.setattr(
        customer_routes,
        "customer_saved_coordinates",
        lambda _db: (28.367, 79.430),
    )
    monkeypatch.setattr(
        customer_routes,
        "list_customer_categories",
        lambda *_args, **_kwargs: area_categories,
    )
    monkeypatch.setattr(
        customer_routes,
        "list_customer_catalogue",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Signed-in customer should receive area-scoped categories.")
        ),
    )

    with app.test_request_context("/customer/"):
        session["customer_id"] = 42
        page = customer_routes.home()

    assert 'data-location-required="false"' in page
    assert "Area-only category" in page
    assert "Browse all active categories" not in page


def test_customer_notification_bell_has_read_all_and_endpoint_returns_json(monkeypatch):
    from flask import session
    from app import app
    from routes import customer as customer_routes

    home_template = (ROOT / "templates" / "customer" / "home.html").read_text(encoding="utf-8")
    assert 'id="customerNotificationReadAll"' in home_template
    assert '"/customer/notifications/mark-all-read"' in home_template
    assert 'X-CSRFToken' in home_template

    class FakeDB:
        def __init__(self):
            self.executed = []
        def execute(self, query, params=()):
            self.executed.append((query, params))
        def commit(self):
            pass
        def close(self):
            pass

    db = FakeDB()
    monkeypatch.setattr(customer_routes, "get_db", lambda: db)
    monkeypatch.setattr(
        customer_routes,
        "get_customer",
        lambda _db: {"id": 42, "customer_id": "CUS-42"},
    )
    monkeypatch.setattr(customer_routes, "table_exists", lambda _db, name: name == "notifications")

    with app.test_request_context(
        "/customer/notifications/mark-all-read",
        method="POST",
        headers={"Accept": "application/json"},
    ):
        session["customer_id"] = 42
        response = customer_routes.mark_all_notifications_read()

    assert response.status_code == 200
    assert response.get_json()["success"] is True
    assert response.get_json()["unread_count"] == 0
    assert db.executed
    assert "user_type" in db.executed[0][0]
    assert db.executed[0][1] == ["42", "CUS-42"]


def test_customer_terms_and_faq_have_no_bottom_tabs_and_footer_company_faq_links():
    faq = (ROOT / "templates" / "customer" / "faq.html").read_text(encoding="utf-8")
    terms = (ROOT / "templates" / "customer" / "terms.html").read_text(encoding="utf-8")
    footer = (ROOT / "templates" / "_sivdhi_footer.html").read_text(encoding="utf-8")

    for page in (faq, terms):
        assert 'class="bottom-nav"' not in page
        assert ".bottom-nav" not in page
        assert "nav-item" not in page
        assert "{% include '_sivdhi_footer.html' %}" in page

    assert "url_for('customer.faq')" in footer
    assert "url_for('worker.faq')" in footer
    assert "url_for('partner.faq')" in footer
    assert "min-height: 200px" in footer


def test_shared_footer_is_removed_from_service_areas_and_partner_opportunity():
    service_areas = (ROOT / "templates" / "customer" / "service_areas.html").read_text(encoding="utf-8")
    opportunity = (ROOT / "templates" / "partner_opportunity.html").read_text(encoding="utf-8")

    assert "_sivdhi_footer.html" not in service_areas
    assert "_sivdhi_footer.html" not in opportunity


def test_worker_help_uses_customer_help_layout_with_worker_content():
    customer_help = (ROOT / "templates" / "customer" / "help.html").read_text(encoding="utf-8")
    worker_help = (ROOT / "templates" / "worker" / "help.html").read_text(encoding="utf-8")

    for marker in (".help-item", ".owner-contact", "--bg: #09070f", 'class="help-list"'):
        assert marker in customer_help
        assert marker in worker_help

    assert "Sivdhi Worker Help" in worker_help
    assert "Browse Worker FAQs" in worker_help
    assert "worker.faq" in worker_help
    assert "customer.faq" not in worker_help
    assert '<meta name="robots" content="noindex, follow">' in worker_help
