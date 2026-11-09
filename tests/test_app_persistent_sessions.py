import sqlite3
from datetime import datetime, timedelta, timezone

from flask import Flask

from config import Config
from utils import app_persistent_sessions as app_sessions


def test_android_session_lifetime_is_long_but_website_policy_is_unchanged():
    app = Flask(__name__)
    app.secret_key = "test-only-secret-key-that-is-long-enough"
    app.config.from_object(Config)
    interface = app_sessions.AppAwareSecureCookieSessionInterface()

    app_data = interface.session_class(
        {
            "customer_id": 7,
            "_permanent": True,
            "sivdhi_app_session_id": "test-only-app-session-token",
        }
    )
    with app.test_request_context(
        "/",
        headers={"User-Agent": "Mozilla/5.0 SivdhiAndroidApp/1.0"},
    ):
        app_expiry = interface.get_expiration_time(app, app_data)
        remaining = app_expiry - datetime.now(timezone.utc)
        assert timedelta(days=364) < remaining <= timedelta(days=365)

    web_data = interface.session_class({"customer_id": 7, "_permanent": True})
    with app.test_request_context("/", headers={"User-Agent": "Mozilla/5.0 Chrome/120"}):
        web_expiry = interface.get_expiration_time(app, web_data)
        web_remaining = web_expiry - datetime.now(timezone.utc)
        assert timedelta(hours=7, minutes=59) < web_remaining <= timedelta(hours=8)


def test_app_session_schema_is_additive_and_uses_hashed_token_ids(tmp_path):
    path = tmp_path / "app-session-test.sqlite"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(
            "CREATE TABLE customers (id INTEGER PRIMARY KEY, password TEXT NOT NULL)"
        )
        conn.execute("INSERT INTO customers (id, password) VALUES (1, 'password-hash-v1')")
        app_sessions.ensure_app_session_schema(conn)
        conn.commit()

        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(app_login_sessions)").fetchall()
        }
        assert {
            "token_hash",
            "role",
            "user_key",
            "credential_fingerprint",
            "created_at",
            "last_seen_at",
            "expires_at",
            "revoked_at",
        }.issubset(columns)

        fingerprint = app_sessions._credential_fingerprint("password-hash-v1")
        assert fingerprint != app_sessions._credential_fingerprint("password-hash-v2")
        assert len(app_sessions._token_hash("sample-random-token")) == 64
    finally:
        conn.close()


def test_only_customer_worker_and_partner_sessions_are_marked_as_app_sessions():
    assert app_sessions._identity_from_session({"customer_id": 12}) == ("customer", "12")
    assert app_sessions._identity_from_session({"role": "worker", "user_id": "W-12"}) == (
        "worker",
        "W-12",
    )
    assert app_sessions._identity_from_session({"role": "partner", "user_id": "P-12"}) == (
        "partner",
        "P-12",
    )
    assert app_sessions._identity_from_session({"role": "admin", "admin_id": "A-1"}) is None


# SIVDHI_ANDROID_PERSISTENT_LOGIN_REGRESSION_TESTS

def test_expired_website_cookie_is_not_accepted_as_long_lived_android_session(monkeypatch):
    from flask import Flask
    from itsdangerous import BadSignature
    from types import SimpleNamespace

    app = Flask(__name__)
    app.secret_key = "test-only-secret-key-that-is-long-enough"
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=8)
    interface = app_sessions.AppAwareSecureCookieSessionInterface()
    calls = []

    class FakeSerializer:
        def loads(self, value, max_age):
            calls.append(max_age)
            if max_age == interface._app_max_age():
                # Simulate an otherwise valid old website cookie with no app token.
                return {"customer_id": 7, "_permanent": True}
            raise BadSignature("website cookie has exceeded its normal lifetime")

    monkeypatch.setattr(interface, "get_signing_serializer", lambda _app: FakeSerializer())

    cookie_name = interface.get_cookie_name(app)
    req = SimpleNamespace(
        cookies={cookie_name: "old-signed-website-cookie"},
        headers={"User-Agent": "Mozilla/5.0 SivdhiAndroidApp/1.0"},
    )

    loaded = interface.open_session(app, req)

    assert loaded is not None
    assert loaded.get("customer_id") is None
    assert calls == [
        interface._app_max_age(),
        int(app.permanent_session_lifetime.total_seconds()),
    ]


def test_existing_website_session_without_app_token_requires_fresh_login(monkeypatch):
    from flask import Flask, session

    app = Flask(__name__)
    app.secret_key = "test-only-secret-key-that-is-long-enough"
    app.add_url_rule("/customer/login", endpoint="customer.login", view_func=lambda: "login")

    with app.test_request_context(
        "/customer/",
        headers={"User-Agent": "Mozilla/5.0 SivdhiAndroidApp/1.0"},
    ):
        session["customer_id"] = 7

        response = app_sessions.before_app_request()

        assert response is not None
        assert response.status_code == 302
        assert response.location == "/customer/login"
        assert not session


def test_app_token_is_not_issued_without_new_authentication_in_current_request(monkeypatch):
    from flask import Flask, Response, g, session

    app = Flask(__name__)
    app.secret_key = "test-only-secret-key-that-is-long-enough"
    app.add_url_rule("/customer/login", endpoint="customer.login", view_func=lambda: "login")

    def unexpected_database_access(*args, **kwargs):
        raise AssertionError("A token must not be issued from an existing unverified session.")

    monkeypatch.setattr(app_sessions, "_load_account_fingerprint", unexpected_database_access)

    with app.test_request_context(
        "/customer/",
        headers={"User-Agent": "Mozilla/5.0 SivdhiAndroidApp/1.0"},
    ):
        g.sivdhi_android_request = True
        session["customer_id"] = 7

        response = app_sessions.after_app_request(Response("not authenticated in this request"))

        assert response.status_code == 302
        assert response.location == "/customer/login"
        assert not session


def test_fresh_android_authentication_issues_a_server_side_persistent_token(monkeypatch, tmp_path):
    import sqlite3
    from flask import Flask, g, session, Response

    db_path = tmp_path / "fresh-app-login.sqlite"

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(
            "CREATE TABLE customers (id INTEGER PRIMARY KEY, password TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO customers (id, password) VALUES (?, ?)",
            (7, "test-password-hash"),
        )
        app_sessions.ensure_app_session_schema(conn)
        conn.commit()
    finally:
        conn.close()

    def test_get_db():
        db = sqlite3.connect(db_path)
        db.row_factory = sqlite3.Row
        return db

    monkeypatch.setattr(app_sessions, "get_db", test_get_db)

    app = Flask(__name__)
    app.secret_key = "test-only-secret-key-that-is-long-enough"

    with app.test_request_context(
        "/customer/login",
        headers={"User-Agent": "Mozilla/5.0 SivdhiAndroidApp/1.0"},
    ):
        # The request starts unauthenticated, as a fresh login request should.
        assert app_sessions.before_app_request() is None
        session["customer_id"] = 7

        response = app_sessions.after_app_request(Response("login successful"))

        raw_token = session.get("sivdhi_app_session_id")
        assert response.status_code == 200
        assert raw_token

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            record = conn.execute(
                "SELECT * FROM app_login_sessions WHERE token_hash=?",
                (app_sessions._token_hash(raw_token),),
            ).fetchone()
            assert record is not None
            assert record["role"] == "customer"
            assert record["user_key"] == "7"
            assert record["revoked_at"] is None
        finally:
            conn.close()
