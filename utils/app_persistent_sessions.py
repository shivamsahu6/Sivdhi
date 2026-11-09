"""Long-lived, revocable customer, worker and partner login sessions.

Sivdhi Android App:
    Users stay signed in for up to 365 days. Valid activity refreshes expiry.

Website:
    Uses Flask's normal permanent-session lifetime (8 hours in this project).

Sessions are revocable. Logging out, changing a password, or administrator
revocation invalidates the server-side token. Admin sessions are excluded
because they use their own MFA policy.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from flask import current_app, g, redirect, request, session, url_for
from flask.sessions import SecureCookieSessionInterface
from itsdangerous import BadSignature

from database import get_db


APP_USER_AGENT_MARKER = "SivdhiAndroidApp/1.0"

# Android app persistent-session duration.
PERSISTENT_SESSION_DAYS = 365

# Check/update last activity at most once every six hours.
APP_SESSION_TOUCH_INTERVAL = timedelta(hours=6)

_ROLE_TABLES = {
    "customer": ("customers", "id"),
    "worker": ("workers", "worker_id"),
    "partner": ("partners", "partner_id"),
}


def is_sivdhi_android_request(req=None) -> bool:
    """Return True when the request comes from the Sivdhi Android app."""
    req = req or request
    user_agent = req.headers.get("User-Agent") or ""
    return APP_USER_AGENT_MARKER in user_agent


def _session_lifetime(req=None) -> timedelta:
    """Return the session lifetime for the current client."""
    if is_sivdhi_android_request(req):
        return timedelta(days=PERSISTENT_SESSION_DAYS)

    # Website uses Flask's configured permanent-session lifetime.
    return current_app.permanent_session_lifetime


def _identity_from_session(data=None):
    """Return (role, user_id) for supported non-admin sessions."""
    data = session if data is None else data

    customer_id = data.get("customer_id")
    if customer_id not in (None, ""):
        return "customer", str(customer_id)

    role = str(data.get("role") or "").strip().lower()
    user_id = data.get("user_id")

    if role in ("worker", "partner") and user_id not in (None, ""):
        return role, str(user_id)

    return None


def _utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def _as_utc(value):
    if not value:
        return None

    try:
        parsed = datetime.fromisoformat(str(value))

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)

        return parsed.astimezone(timezone.utc)

    except (TypeError, ValueError):
        return None


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _credential_fingerprint(password_hash) -> str:
    return hashlib.sha256(
        str(password_hash or "").encode("utf-8")
    ).hexdigest()


def ensure_app_session_schema(conn=None):
    """Create the app-session ledger additively; never alters user tables."""
    owns_connection = conn is None
    db = conn or get_db()

    try:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS app_login_sessions (
                token_hash TEXT PRIMARY KEY,
                role TEXT NOT NULL,
                user_key TEXT NOT NULL,
                credential_fingerprint TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                revoked_at TEXT
            )
            """
        )

        db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_app_login_sessions_user
            ON app_login_sessions(role, user_key, revoked_at)
            """
        )

        # Gradually remove expired and old revoked records.
        now = _utc_now()
        cutoff = (now - timedelta(days=90)).isoformat()

        db.execute(
            """
            DELETE FROM app_login_sessions
            WHERE expires_at < ?
               OR (revoked_at IS NOT NULL AND revoked_at < ?)
            """,
            (now.isoformat(), cutoff),
        )

        if owns_connection:
            db.commit()

    finally:
        if owns_connection:
            db.close()


def _load_account_fingerprint(role: str, user_key: str):
    """Read the account password hash for session invalidation checks."""
    target = _ROLE_TABLES.get(role)

    if not target:
        return None

    table, id_column = target
    db = get_db()

    try:
        row = db.execute(
            f"""
            SELECT password
            FROM {table}
            WHERE {id_column}=?
            LIMIT 1
            """,
            (user_key,),
        ).fetchone()

        if not row or not row["password"]:
            return None

        return _credential_fingerprint(row["password"])

    finally:
        db.close()


def _revoke_token_hash(token_hash: str):
    """Revoke one persistent session token."""
    if not token_hash:
        return

    db = get_db()

    try:
        db.execute(
            """
            UPDATE app_login_sessions
            SET revoked_at=?
            WHERE token_hash=?
              AND revoked_at IS NULL
            """,
            (_utc_now().isoformat(), token_hash),
        )

        db.commit()

    finally:
        db.close()


def _login_redirect(role: str):
    """Redirect an expired session to the correct role login page."""
    if role == "customer":
        return redirect(url_for("customer.login"))

    return redirect(url_for("auth.login", role=role))


def _is_app_identity_payload(data):
    """Check that a signed cookie contains a supported user identity."""
    if not isinstance(data, dict):
        return False

    return _identity_from_session(data) is not None


class AppAwareSecureCookieSessionInterface(SecureCookieSessionInterface):
    """Use long-lived signed cookies only for Sivdhi Android app sessions."""

    def _app_max_age(self) -> int:
        return int(
            timedelta(days=PERSISTENT_SESSION_DAYS).total_seconds()
        )

    def open_session(self, app, req):
        serializer = self.get_signing_serializer(app)

        if serializer is None:
            return None

        cookie_value = req.cookies.get(self.get_cookie_name(app))

        if not cookie_value:
            return self.session_class()

        default_max_age = int(
            app.permanent_session_lifetime.total_seconds()
        )

        # Only the Sivdhi Android app may use the 365-day cookie age.
        # A valid server-issued token is also required.
        if is_sivdhi_android_request(req):
            try:
                data = serializer.loads(
                    cookie_value,
                    max_age=self._app_max_age(),
                )

                if (
                    _is_app_identity_payload(data)
                    and isinstance(
                        data.get("sivdhi_app_session_id"),
                        str,
                    )
                    and bool(data.get("sivdhi_app_session_id"))
                ):
                    return self.session_class(data)

            except BadSignature:
                pass

        # Website and all other clients use Flask's normal cookie lifetime.
        try:
            data = serializer.loads(
                cookie_value,
                max_age=default_max_age,
            )

        except BadSignature:
            return self.session_class()

        return self.session_class(data)

    def get_expiration_time(self, app, session_obj):
        """Set cookie expiry according to the current client."""
        has_persistent_identity = bool(
            _identity_from_session(session_obj)
            and session_obj.get("sivdhi_app_session_id")
        )

        if (
            has_persistent_identity
            and is_sivdhi_android_request()
        ):
            return _utc_now() + timedelta(
                days=PERSISTENT_SESSION_DAYS
            )

        # Website keeps Flask's normal configured expiry.
        return super().get_expiration_time(app, session_obj)


def before_app_request():
    """Validate persistent non-admin sessions before protected routes run."""
    identity = _identity_from_session()

    if not identity:
        # A successful login/signup can create a token in after_app_request.
        g.sivdhi_app_authentication_request = True
        return None

    role, user_key = identity

    session.permanent = True

    raw_token = session.get("sivdhi_app_session_id")

    if not raw_token:
        # Do not silently promote an old session into a persistent session.
        session.clear()
        return _login_redirect(role)

    hashed = _token_hash(str(raw_token))

    db = get_db()

    try:
        record = db.execute(
            """
            SELECT
                role,
                user_key,
                credential_fingerprint,
                last_seen_at,
                expires_at,
                revoked_at
            FROM app_login_sessions
            WHERE token_hash=?
            LIMIT 1
            """,
            (hashed,),
        ).fetchone()

    except Exception:
        current_app.logger.exception(
            "Could not validate Sivdhi persistent session"
        )

        session.clear()
        return _login_redirect(role)

    finally:
        db.close()

    now = _utc_now()
    expires_at = (
        _as_utc(record["expires_at"])
        if record
        else None
    )

    try:
        account_fingerprint = _load_account_fingerprint(
            role,
            user_key,
        )

    except Exception:
        current_app.logger.exception(
            "Could not verify Sivdhi persistent-session credentials"
        )

        session.clear()
        return _login_redirect(role)

    valid = bool(
        record
        and not record["revoked_at"]
        and record["role"] == role
        and str(record["user_key"]) == user_key
        and expires_at
        and expires_at > now
        and account_fingerprint
        and account_fingerprint == record["credential_fingerprint"]
    )

    if not valid:
        try:
            _revoke_token_hash(hashed)

        except Exception:
            current_app.logger.exception(
                "Could not revoke invalid Sivdhi persistent session"
            )

        session.clear()
        return _login_redirect(role)

    g.sivdhi_app_session_hash = hashed

    last_seen = _as_utc(record["last_seen_at"])

    if (
        not last_seen
        or now - last_seen >= APP_SESSION_TOUCH_INTERVAL
    ):
        # Android: refresh by 365 days.
        # Website: refresh by its normal Flask session lifetime.
        new_expiry = (
            now + _session_lifetime()
        ).isoformat()

        db = get_db()

        try:
            db.execute(
                """
                UPDATE app_login_sessions
                SET last_seen_at=?,
                    expires_at=?
                WHERE token_hash=?
                  AND revoked_at IS NULL
                """,
                (
                    now.isoformat(),
                    new_expiry,
                    hashed,
                ),
            )

            db.commit()

        except Exception:
            # Keep the already validated session available if only the
            # sliding-expiry refresh fails. Existing expiry remains enforced.
            current_app.logger.exception(
                "Could not refresh Sivdhi persistent-session activity"
            )

        finally:
            db.close()

    return None


def after_app_request(response):
    """Issue or revoke persistent user sessions after login/logout."""
    identity = _identity_from_session()
    existing_hash = getattr(
        g,
        "sivdhi_app_session_hash",
        None,
    )

    if not identity:
        if existing_hash:
            try:
                _revoke_token_hash(existing_hash)

            except Exception:
                current_app.logger.exception(
                    "Could not revoke logged-out Sivdhi persistent session"
                )

        return response

    role, user_key = identity

    session.permanent = True

    raw_token = session.get("sivdhi_app_session_id")

    if raw_token:
        return response

    # Issue a token only when this request began unauthenticated and
    # successfully established a new authenticated identity.
    if not getattr(
        g,
        "sivdhi_app_authentication_request",
        False,
    ):
        session.clear()
        return _login_redirect(role)

    fingerprint = _load_account_fingerprint(
        role,
        user_key,
    )

    if not fingerprint:
        session.clear()
        return _login_redirect(role)

    raw_token = secrets.token_urlsafe(32)
    token_hash = _token_hash(raw_token)

    now = _utc_now()

    # Android gets 365 days; website gets its configured Flask lifetime.
    expiry = now + _session_lifetime()

    db = get_db()

    try:
        db.execute(
            """
            INSERT INTO app_login_sessions
            (
                token_hash,
                role,
                user_key,
                credential_fingerprint,
                created_at,
                last_seen_at,
                expires_at,
                revoked_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                token_hash,
                role,
                user_key,
                fingerprint,
                now.isoformat(),
                now.isoformat(),
                expiry.isoformat(),
            ),
        )

        db.commit()

    except Exception:
        db.rollback()

        current_app.logger.exception(
            "Could not create Sivdhi persistent session"
        )

        session.clear()
        return _login_redirect(role)

    finally:
        db.close()

    # The raw random token is stored only in Flask's signed, HttpOnly cookie.
    # SQLite stores its SHA-256 digest, never the bearer token itself.
    session["sivdhi_app_session_id"] = raw_token

    return response