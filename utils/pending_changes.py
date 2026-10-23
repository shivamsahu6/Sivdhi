"""Server-side storage for sensitive OTP-confirmed account changes.

Flask's default session cookie is signed but not encrypted. Bank account details
and pending password hashes therefore belong in the database, not in session.
Only a random opaque token is placed in the browser cookie by these helpers.
"""

import hashlib
import json
import secrets
import time

from database import get_db
from utils.otp_security import hash_otp, verify_otp

PENDING_TTL_SECONDS = 5 * 60
MAX_VERIFY_ATTEMPTS = 5


def _token_hash(token):
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def _ensure_table(conn):
    # Provisioned by database.create_tables() at app startup. Avoid acquiring a
    # schema write lock in the normal verification path; fallback for unit tests.
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pending_security_changes'"
    ).fetchone():
        return
    conn.execute(
        """CREATE TABLE IF NOT EXISTS pending_security_changes (
            token_hash TEXT PRIMARY KEY,
            user_role TEXT NOT NULL,
            user_id TEXT NOT NULL,
            purpose TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            otp_hash TEXT NOT NULL,
            expires_at INTEGER NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL,
            last_sent INTEGER NOT NULL,
            UNIQUE(user_role, user_id, purpose)
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pending_security_expires ON pending_security_changes(expires_at)"
    )
    conn.commit()

def issue_pending_change(user_role, user_id, purpose, payload, otp, ttl=PENDING_TTL_SECONDS):
    """Create a pending change and return its unguessable session token."""
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    conn = get_db()
    try:
        _ensure_table(conn)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "DELETE FROM pending_security_changes WHERE user_role=? AND user_id=? AND purpose=?",
            (str(user_role), str(user_id), str(purpose)),
        )
        conn.execute(
            """INSERT INTO pending_security_changes
               (token_hash, user_role, user_id, purpose, payload_json, otp_hash,
                expires_at, attempts, created_at, last_sent)
               VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)""",
            (
                _token_hash(token), str(user_role), str(user_id), str(purpose),
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                hash_otp(otp, purpose), now + int(ttl), now, now,
            ),
        )
        conn.commit()
        return token
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def refresh_pending_change(user_role, user_id, purpose, token, otp, ttl=PENDING_TTL_SECONDS):
    """Replace a pending OTP during an allowed resend; returns status string."""
    now = int(time.time())
    conn = get_db()
    try:
        _ensure_table(conn)
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """SELECT expires_at FROM pending_security_changes
               WHERE token_hash=? AND user_role=? AND user_id=? AND purpose=?""",
            (_token_hash(token), str(user_role), str(user_id), str(purpose)),
        ).fetchone()
        if row is None:
            conn.rollback()
            return "missing"
        if int(row["expires_at"] or 0) <= now:
            conn.execute("DELETE FROM pending_security_changes WHERE token_hash=?", (_token_hash(token),))
            conn.commit()
            return "expired"
        conn.execute(
            """UPDATE pending_security_changes
               SET otp_hash=?, expires_at=?, attempts=0, last_sent=?
               WHERE token_hash=? AND user_role=? AND user_id=? AND purpose=?""",
            (hash_otp(otp, purpose), now + int(ttl), now, _token_hash(token),
             str(user_role), str(user_id), str(purpose)),
        )
        conn.commit()
        return "ok"
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def verify_pending_change(user_role, user_id, purpose, token, otp):
    """Return (status, payload, attempts). Wrong attempts are recorded atomically."""
    now = int(time.time())
    conn = get_db()
    try:
        _ensure_table(conn)
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """SELECT payload_json, otp_hash, expires_at, attempts
               FROM pending_security_changes
               WHERE token_hash=? AND user_role=? AND user_id=? AND purpose=?""",
            (_token_hash(token), str(user_role), str(user_id), str(purpose)),
        ).fetchone()
        if row is None:
            conn.rollback()
            return "missing", None, 0
        if int(row["expires_at"] or 0) <= now:
            conn.execute("DELETE FROM pending_security_changes WHERE token_hash=?", (_token_hash(token),))
            conn.commit()
            return "expired", None, 0
        attempts = max(0, int(row["attempts"] or 0))
        if attempts >= MAX_VERIFY_ATTEMPTS:
            conn.execute("DELETE FROM pending_security_changes WHERE token_hash=?", (_token_hash(token),))
            conn.commit()
            return "locked", None, attempts
        if not verify_otp(str(otp or "").strip(), row["otp_hash"], purpose):
            attempts += 1
            if attempts >= MAX_VERIFY_ATTEMPTS:
                conn.execute("DELETE FROM pending_security_changes WHERE token_hash=?", (_token_hash(token),))
                conn.commit()
                return "locked", None, attempts
            conn.execute(
                "UPDATE pending_security_changes SET attempts=? WHERE token_hash=?",
                (attempts, _token_hash(token)),
            )
            conn.commit()
            return "invalid", None, attempts
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError):
            conn.execute("DELETE FROM pending_security_changes WHERE token_hash=?", (_token_hash(token),))
            conn.commit()
            return "invalid_state", None, 0

        # Consume the challenge before releasing the write lock. This makes OTP
        # verification single-use even if two POST requests arrive concurrently.
        conn.execute(
            "DELETE FROM pending_security_changes WHERE token_hash=? AND user_role=? AND user_id=? AND purpose=?",
            (_token_hash(token), str(user_role), str(user_id), str(purpose)),
        )
        conn.commit()
        return "ok", payload, attempts
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def delete_pending_change(user_role, user_id, purpose, token=None):
    """Delete one pending change; token can constrain cleanup to this session."""
    conn = get_db()
    try:
        _ensure_table(conn)
        if token:
            cur = conn.execute(
                "DELETE FROM pending_security_changes WHERE token_hash=? AND user_role=? AND user_id=? AND purpose=?",
                (_token_hash(token), str(user_role), str(user_id), str(purpose)),
            )
        else:
            cur = conn.execute(
                "DELETE FROM pending_security_changes WHERE user_role=? AND user_id=? AND purpose=?",
                (str(user_role), str(user_id), str(purpose)),
            )
        conn.commit()
        return max(0, int(cur.rowcount or 0))
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def cleanup_expired_pending_changes():
    conn = get_db()
    try:
        _ensure_table(conn)
        cur = conn.execute("DELETE FROM pending_security_changes WHERE expires_at <= ?", (int(time.time()),))
        conn.commit()
        return max(0, int(cur.rowcount or 0))
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
