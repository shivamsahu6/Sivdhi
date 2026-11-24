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

def test_public_entrypoint_and_sitemap_advertise_only_customer_home():
    import xml.etree.ElementTree as ET

    from app import app

    client = app.test_client()
    root_response = client.get("/")
    assert root_response.status_code == 302
    assert root_response.headers.get("Location") == "/customer/"

    sitemap_response = client.get("/sitemap.xml")
    assert sitemap_response.status_code == 200

    document = ET.fromstring(sitemap_response.data)
    namespace = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    locations = [
        node.text
        for node in document.findall(".//sm:loc", namespace)
    ]
    assert locations == ["https://sivdhi.duckdns.org/customer/"]


def test_worker_and_partner_login_pages_are_noindex_but_accessible():
    from app import app

    client = app.test_client()
    expected_robots = "noindex, nofollow, noarchive"

    for role in ("worker", "partner"):
        response = client.get(f"/login?role={role}")
        assert response.status_code == 200
        assert response.headers.get("X-Robots-Tag") == expected_robots

        page = response.get_data(as_text=True)
        assert f'<meta name="robots" content="{expected_robots}">' in page

    for role in ("worker", "partner"):
        response = client.get(f"/signup?role={role}")
        assert response.status_code == 200
        assert response.headers.get("X-Robots-Tag") == expected_robots

def test_customer_private_pages_are_noindex_but_public_home_remains_indexable():
    from app import app

    client = app.test_client()
    expected_robots = "noindex, nofollow, noarchive"

    # The public customer homepage must remain indexable.
    for path in ("/customer/", "/customer/home"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers.get("X-Robots-Tag") is None
        assert '<meta name="robots" content="index, follow">' in response.get_data(as_text=True)

    # Protected customer pages must carry noindex even on login redirects.
    for path in (
        "/customer/bookings",
        "/customer/profile",
        "/customer/edit-profile",
    ):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 302
        assert response.headers.get("X-Robots-Tag") == expected_robots

    # The customer signup page remains accessible but not indexable.
    signup_response = client.get("/customer/signup")
    assert signup_response.status_code == 200
    assert signup_response.headers.get("X-Robots-Tag") == expected_robots


def test_ga4_tracks_only_allowlisted_anonymous_public_pages(monkeypatch):
    from app import app

    client = app.test_client()
    monkeypatch.setitem(app.config, "GA4_MEASUREMENT_ID", "G-R0RQ9GKPGE")

    # Track public homepage and anonymous login/signup page views.
    allowed_pages = (
        "/customer/",
        "/customer/home?mode=full-day&email=private@example.com",
        "/customer/signup",
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
