"""Concurrent-safe login throttling shared by every user role.

Three independent limits are enforced:
- identifier + client IP: short limit for repeated retries from one client;
- identifier across IPs: blocks distributed guesses against one account;
- client IP across identifiers: slows credential stuffing from one client.

For public deployment behind a reverse proxy, only configure trusted proxy
handling at the server edge; do not trust arbitrary X-Forwarded-For headers.
"""

import hashlib
import time

from database import get_db


MAX_FAILURES = 5               # identifier + IP
MAX_ACCOUNT_FAILURES = 12      # same identifier across IPs
MAX_IP_FAILURES = 30           # same IP across identifiers
WINDOW_SECONDS = 15 * 60
LOCK_SECONDS = 15 * 60
_ACCOUNT_MARKER = "__all_clients__"
_IP_MARKER = "__all_identifiers__"


def _normalise(value, default="unknown"):
    if value is None:
        return default
    value = str(value).strip().casefold()
    return value[:320] if value else default


def _key(scope, identifier, ip_address):
    raw = "|".join(
        (_normalise(scope), _normalise(identifier), _normalise(ip_address))
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _attempt_keys(scope, identifier, ip_address):
    """Return (key, max failures) tuples, retaining the legacy combo key."""
    return (
        (_key(scope, identifier, ip_address), MAX_FAILURES),
        (_key(scope, identifier, _ACCOUNT_MARKER), MAX_ACCOUNT_FAILURES),
        (_key(scope, _IP_MARKER, ip_address), MAX_IP_FAILURES),
    )


def _ensure_table(conn):
    # The application provisions this schema at startup. Avoid running DDL and
    # taking a write lock on every login request; the fallback keeps the helper
    # usable in isolated tests and one-off scripts.
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='login_attempts'"
    ).fetchone():
        return
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS login_attempts (
            attempt_key TEXT PRIMARY KEY,
            attempts INTEGER NOT NULL DEFAULT 0,
            window_started INTEGER NOT NULL,
            locked_until INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_login_attempts_locked_until "
        "ON login_attempts(locked_until)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_login_attempts_window_started "
        "ON login_attempts(window_started)"
    )
    conn.commit()

def is_login_allowed(scope, identifier, ip_address):
    """Return False if any combo/account/IP key is currently locked."""
    now = int(time.time())
    conn = get_db()
    try:
        _ensure_table(conn)
        keys = _attempt_keys(scope, identifier, ip_address)
        rows = conn.execute(
            "SELECT attempt_key, window_started, locked_until "
            "FROM login_attempts WHERE attempt_key IN (?, ?, ?)",
            tuple(key for key, _limit in keys),
        ).fetchall()
        for row in rows:
            if int(row["locked_until"] or 0) > now:
                return False
            started = int(row["window_started"] or now)
            if now - started >= WINDOW_SECONDS:
                conn.execute(
                    "DELETE FROM login_attempts WHERE attempt_key = ?",
                    (row["attempt_key"],),
                )
        conn.commit()
        return True
    finally:
        conn.close()


def record_login_failure(scope, identifier, ip_address):
    """Record a failure at combo/account/IP levels and return combo attempts."""
    now = int(time.time())
    conn = get_db()
    combo_attempts = 0
    try:
        _ensure_table(conn)
        conn.execute("BEGIN IMMEDIATE")
        for index, (key, threshold) in enumerate(_attempt_keys(scope, identifier, ip_address)):
            row = conn.execute(
                "SELECT attempts, window_started FROM login_attempts "
                "WHERE attempt_key = ? LIMIT 1",
                (key,),
            ).fetchone()
            if row is None or now - int(row["window_started"] or now) >= WINDOW_SECONDS:
                attempts = 1
                window_started = now
            else:
                attempts = int(row["attempts"] or 0) + 1
                window_started = int(row["window_started"])
            locked_until = now + LOCK_SECONDS if attempts >= threshold else 0
            conn.execute(
                """
                INSERT INTO login_attempts
                    (attempt_key, attempts, window_started, locked_until)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(attempt_key) DO UPDATE SET
                    attempts = excluded.attempts,
                    window_started = excluded.window_started,
                    locked_until = excluded.locked_until
                """,
                (key, attempts, window_started, locked_until),
            )
            if index == 0:
                combo_attempts = attempts
        conn.commit()
        return combo_attempts
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def clear_login_failures(scope, identifier, ip_address):
    """Clear combo and account failures after login; retain the IP-wide counter."""
    conn = get_db()
    try:
        _ensure_table(conn)
        keys = _attempt_keys(scope, identifier, ip_address)
        conn.executemany(
            "DELETE FROM login_attempts WHERE attempt_key = ?",
            [(keys[0][0],), (keys[1][0],)],
        )
        conn.commit()
    finally:
        conn.close()
