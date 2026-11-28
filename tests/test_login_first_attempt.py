from datetime import datetime, timedelta, timezone

from flask import Flask, Response, g, session

from utils import app_persistent_sessions as app_sessions


class _FetchOne:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class _FakeDatabase:
    def __init__(self, session_record=None):
        self.session_record = session_record
        self.queries = []
        self.committed = False
        self.closed = False

    def execute(self, sql, params=()):
        self.queries.append((sql, params))
        if "FROM app_login_sessions" in sql and "SELECT" in sql.upper():
            return _FetchOne(self.session_record)
        return self

    def commit(self):
        self.committed = True

    def rollback(self):
        pass

    def close(self):
        self.closed = True


def test_login_post_from_a_valid_existing_role_session_can_issue_new_identity_token(monkeypatch):
    """Switching accounts/roles in one browser must work on the first submission."""
    cases = (
        (
            "/login?role=worker",
            {"role": "partner", "user_id": "SP10001"},
            {"role": "worker", "user_id": "W10001"},
        ),
        (
            "/login?role=partner",
            {"role": "worker", "user_id": "W10001"},
            {"role": "partner", "user_id": "SP10002"},
        ),
        (
            "/customer/login",
            {"role": "partner", "user_id": "SP10001"},
            {"customer_id": 42},
        ),
    )

    for path, old_identity, new_identity in cases:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        old_token = "old-session-token-" + path.replace("/", "-")
        session_record = {
            "role": old_identity["role"],
            "user_key": old_identity["user_id"],
            "credential_fingerprint": "valid-account-fingerprint",
            "last_seen_at": now.isoformat(),
            "expires_at": (now + timedelta(hours=7)).isoformat(),
            "revoked_at": None,
        }
        validation_db = _FakeDatabase(session_record)
        issue_db = _FakeDatabase()
        db_queue = iter((validation_db, issue_db))

        monkeypatch.setattr(app_sessions, "get_db", lambda: next(db_queue))
        monkeypatch.setattr(
            app_sessions,
            "_load_account_fingerprint",
            lambda _role, _user_key: "valid-account-fingerprint",
        )

        app = Flask(__name__)
        app.secret_key = "test-only-secret-key-that-is-long-enough"

        with app.test_request_context(path, method="POST"):
            session.update(old_identity)
            session["sivdhi_app_session_id"] = old_token

            # The old login is valid when this new login POST begins.
            assert app_sessions.before_app_request() is None

            # Model the successful login handler replacing the old identity.
            session.clear()
            session.update(new_identity)

            response = app_sessions.after_app_request(Response("login successful"))

            assert response.status_code == 200
            assert g.sivdhi_app_authentication_request is True
            new_token = session.get("sivdhi_app_session_id")
            assert new_token
            assert new_token != old_token
            assert issue_db.committed is True
            assert any(
                "INSERT INTO app_login_sessions" in sql
                and params[1] == (
                    "customer" if "customer_id" in new_identity
                    else new_identity["role"]
                )
                for sql, params in issue_db.queries
            )
            assert validation_db.closed is True
            assert issue_db.closed is True
