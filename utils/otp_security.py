"""Atomic, database-backed OTP request throttling.

Three shared limits apply to each OTP issuance: per account+IP, per account
across IPs, and per IP across accounts. This is useful against simple abuse,
but a shared Redis/store and edge rate limits are still recommended at scale.
"""

import hashlib
import hmac
import time

from config import Config

from database import get_db

OTP_REQUEST_WINDOW_SECONDS = 60 * 60
COMBO_MAX_REQUESTS = 5
ACCOUNT_MAX_REQUESTS = 10
IP_MAX_REQUESTS = 30
COMBO_COOLDOWN_SECONDS = 60
ACCOUNT_COOLDOWN_SECONDS = 60
IP_COOLDOWN_SECONDS = 3
_ACCOUNT_MARKER = "__all_client_ips__"
_IP_MARKER = "__all_accounts__"


def _clean(value, max_length=320):
    if value is None:
        return "unknown"
    cleaned = str(value).strip().casefold()
    return (cleaned[:max_length] or "unknown")


def _key(scope, identifier, ip_address):
    raw = "|".join((_clean(scope), _clean(identifier), _clean(ip_address))).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _request_keys(scope, identifier, ip_address):
    scope = _clean(scope)
    identifier = _clean(identifier)
    ip_address = _clean(ip_address)
    return (
        (_key("otp:combo:" + scope, identifier, ip_address), COMBO_MAX_REQUESTS, COMBO_COOLDOWN_SECONDS),
        (_key("otp:account", identifier, _ACCOUNT_MARKER), ACCOUNT_MAX_REQUESTS, ACCOUNT_COOLDOWN_SECONDS),
        (_key("otp:ip", _IP_MARKER, ip_address), IP_MAX_REQUESTS, IP_COOLDOWN_SECONDS),
    )


def _ensure_table(conn):
    # The application provisions this table during startup. Do not perform DDL
    # on every OTP attempt; the fallback supports standalone tests/scripts.
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='otp_requests'"
    ).fetchone():
        return
    conn.execute(
        """CREATE TABLE IF NOT EXISTS otp_requests (
            request_key TEXT PRIMARY KEY,
            request_count INTEGER NOT NULL DEFAULT 0,
            window_started INTEGER NOT NULL,
            last_sent INTEGER NOT NULL
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_otp_requests_window ON otp_requests(window_started)"
    )
    conn.commit()

def hash_otp(otp, purpose):
    """Hash an OTP with the server-only key before storing it in session/database state."""
    secret = Config.SECRET_KEY
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    message = ("sivdhi-otp|" + _clean(purpose) + "|" + str(otp)).encode("utf-8")
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def verify_otp(otp, expected_hash, purpose):
    """Constant-time validation of an OTP hash created by :func:`hash_otp`."""
    if not expected_hash or not str(otp).isdigit() or len(str(otp)) != 6:
        return False
    return hmac.compare_digest(hash_otp(otp, purpose), str(expected_hash))

def reserve_otp_request(scope, identifier, ip_address):
    """Atomically check limits and reserve one OTP-send attempt.

    Returns ``(True, 0)`` if reserved, otherwise ``(False, retry_after_seconds)``.
    Reservations are recorded before contacting the email provider so provider
    failures cannot be abused to generate unbounded outbound traffic.
    """
    now = int(time.time())
    keys = _request_keys(scope, identifier, ip_address)
    conn = get_db()
    try:
        _ensure_table(conn)
        conn.execute("BEGIN IMMEDIATE")
        rows = {}
        for key, _limit, _cooldown in keys:
            rows[key] = conn.execute(
                "SELECT request_count, window_started, last_sent FROM otp_requests WHERE request_key=?",
                (key,),
            ).fetchone()

        # Check every dimension before mutating any counter.
        for key, limit, cooldown in keys:
            row = rows[key]
            if row is None:
                continue
            started = int(row["window_started"] or now)
            count = max(0, int(row["request_count"] or 0))
            last_sent = int(row["last_sent"] or 0)
            if now - started < OTP_REQUEST_WINDOW_SECONDS:
                elapsed = max(0, now - last_sent)
                if elapsed < cooldown:
                    conn.rollback()
                    return False, max(1, cooldown - elapsed)
                if count >= limit:
                    conn.rollback()
                    return False, max(1, OTP_REQUEST_WINDOW_SECONDS - max(0, now - started))

        for key, _limit, _cooldown in keys:
            row = rows[key]
            if row is None or now - int(row["window_started"] or now) >= OTP_REQUEST_WINDOW_SECONDS:
                count = 1
                started = now
            else:
                count = int(row["request_count"] or 0) + 1
                started = int(row["window_started"])
            conn.execute(
                """INSERT INTO otp_requests(request_key, request_count, window_started, last_sent)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(request_key) DO UPDATE SET
                     request_count=excluded.request_count,
                     window_started=excluded.window_started,
                     last_sent=excluded.last_sent""",
                (key, count, started, now),
            )
        conn.commit()
        return True, 0
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def otp_request_allowed(scope, identifier, ip_address):
    """Compatibility preflight; use reserve_otp_request before sending OTPs."""
    now = int(time.time())
    conn = get_db()
    try:
        _ensure_table(conn)
        for key, limit, cooldown in _request_keys(scope, identifier, ip_address):
            row = conn.execute(
                "SELECT request_count, window_started, last_sent FROM otp_requests WHERE request_key=?",
                (key,),
            ).fetchone()
            if row is None:
                continue
            started = int(row["window_started"] or now)
            count = max(0, int(row["request_count"] or 0))
            if now - started >= OTP_REQUEST_WINDOW_SECONDS:
                continue
            elapsed = max(0, now - int(row["last_sent"] or 0))
            if elapsed < cooldown:
                return False, max(1, cooldown - elapsed)
            if count >= limit:
                return False, max(1, OTP_REQUEST_WINDOW_SECONDS - max(0, now - started))
        return True, 0
    finally:
        conn.close()


def record_otp_request(scope, identifier, ip_address):
    """Legacy wrapper kept for external callers; records a reservation atomically."""
    allowed, retry_after = reserve_otp_request(scope, identifier, ip_address)
    if not allowed:
        raise RuntimeError(f"OTP request rate limit exceeded; retry after {retry_after} seconds")


def cleanup_expired_otp_requests():
    cutoff = int(time.time()) - OTP_REQUEST_WINDOW_SECONDS
    conn = get_db()
    try:
        _ensure_table(conn)
        cur = conn.execute("DELETE FROM otp_requests WHERE window_started < ?", (cutoff,))
        deleted = max(0, int(cur.rowcount or 0))
        conn.commit()
        return deleted
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
