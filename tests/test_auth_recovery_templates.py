"""Regression checks for recovered auth blueprint and role-specific OTP templates."""
from pathlib import Path
import ast

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
        "/customer/about",
        "/customer/help",
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
