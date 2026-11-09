"""Website-only Admin MFA with encrypted TOTP secrets and one-time recovery codes.

The encryption key is independent from Flask SECRET_KEY. Production must set
ADMIN_MFA_ENCRYPTION_KEY to a valid Fernet key. Development creates a private,
ignored .admin_mfa.key file so restarts do not invalidate enrollment.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import struct
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from cryptography.fernet import Fernet, InvalidToken

ADMIN_MFA_PENDING_SECONDS = 5 * 60
TOTP_PERIOD_SECONDS = 30
TOTP_DIGITS = 6
RECOVERY_CODE_COUNT = 10
_FERNET_ENV = "ADMIN_MFA_ENCRYPTION_KEY"
_fernet_instance: Fernet | None = None
_fernet_key_material: bytes | None = None


def configure_admin_mfa_encryption() -> None:
    """Validate/create the persistent independent encryption key at startup."""
    global _fernet_instance, _fernet_key_material
    configured = os.getenv(_FERNET_ENV, "").strip()
    production = os.getenv("APP_ENV", os.getenv("FLASK_ENV", "development")).strip().lower() == "production"

    if configured and configured.lower().startswith(("replace-", "change-me")):
        configured = ""
    if configured:
        try:
            _fernet_key_material = configured.encode("ascii")
            _fernet_instance = Fernet(_fernet_key_material)
        except (ValueError, UnicodeEncodeError) as exc:
            raise RuntimeError("ADMIN_MFA_ENCRYPTION_KEY must be a valid Fernet key.") from exc
        return

    if production:
        raise RuntimeError("Production requires ADMIN_MFA_ENCRYPTION_KEY. Generate and store a private Fernet key in the server environment.")

    key_path = Path(__file__).resolve().parent.parent / ".admin_mfa.key"
    key_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(str(key_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raw_key = key_path.read_bytes().strip()
    else:
        raw_key = Fernet.generate_key()
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw_key + b"\n")
        try:
            os.chmod(key_path, 0o600)
        except OSError:
            pass
    try:
        _fernet_key_material = raw_key
        _fernet_instance = Fernet(raw_key)
    except (ValueError, UnboundLocalError) as exc:
        raise RuntimeError("The local .admin_mfa.key file is invalid. Restore its backup or configure ADMIN_MFA_ENCRYPTION_KEY.") from exc


def _fernet() -> Fernet:
    global _fernet_instance, _fernet_key_material
    if _fernet_instance is not None:
        return _fernet_instance
    configured = os.getenv(_FERNET_ENV, "").strip()
    if configured:
        try:
            _fernet_key_material = configured.encode("ascii")
            _fernet_instance = Fernet(_fernet_key_material)
            return _fernet_instance
        except (ValueError, UnicodeEncodeError) as exc:
            raise RuntimeError("ADMIN_MFA_ENCRYPTION_KEY is invalid.") from exc
    key_path = Path(__file__).resolve().parent.parent / ".admin_mfa.key"
    try:
        _fernet_key_material = key_path.read_bytes().strip()
        _fernet_instance = Fernet(_fernet_key_material)
        return _fernet_instance
    except (OSError, ValueError) as exc:
        raise RuntimeError("Admin MFA encryption key is not configured.") from exc


def encrypt_totp_secret(secret: str) -> str:
    return _fernet().encrypt(secret.encode("ascii")).decode("ascii")


def decrypt_totp_secret(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(str(ciphertext).encode("ascii")).decode("ascii")
    except (InvalidToken, ValueError, UnicodeDecodeError) as exc:
        raise RuntimeError("Stored Admin MFA secret cannot be decrypted. Verify the original ADMIN_MFA_ENCRYPTION_KEY backup.") from exc


def _recovery_pepper() -> bytes:
    raw = _fernet_key_material
    if raw is None:
        _fernet()
        raw = _fernet_key_material
    if raw is None:
        raise RuntimeError("Admin MFA encryption key is unavailable.")
    return hashlib.sha256(base64.urlsafe_b64decode(raw) + b"Sivdhi Admin recovery codes v1").digest()


def _recovery_digest(admin_id: str, code: str) -> str:
    normalised = re.sub(r"[^A-Fa-f0-9]", "", str(code)).upper()
    message = (str(admin_id) + ":" + normalised).encode("utf-8")
    return hmac.new(_recovery_pepper(), message, hashlib.sha256).hexdigest()


def ensure_admin_security_schema(conn) -> None:
    """Create additive Admin-only MFA tables and seed existing admins as active."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS admin_security (
            admin_id TEXT PRIMARY KEY,
            is_active INTEGER NOT NULL DEFAULT 1,
            totp_secret_enc TEXT,
            pending_secret_enc TEXT,
            totp_enabled INTEGER NOT NULL DEFAULT 0,
            last_totp_counter INTEGER NOT NULL DEFAULT -1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS admin_recovery_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id TEXT NOT NULL,
            code_digest TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            used_at TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_admin_recovery_codes_admin_used "
        "ON admin_recovery_codes(admin_id, used_at)"
    )
    conn.execute("INSERT OR IGNORE INTO admin_security (admin_id) SELECT admin_id FROM admin")


def ensure_admin_security_record(admin_id: str):
    from database import get_db

    conn = get_db()
    try:
        conn.execute("INSERT OR IGNORE INTO admin_security (admin_id) VALUES (?)", (str(admin_id),))
        conn.commit()
        return conn.execute("SELECT * FROM admin_security WHERE admin_id=? LIMIT 1", (str(admin_id),)).fetchone()
    finally:
        conn.close()


def get_admin_security_state(admin_id: str):
    from database import get_db

    conn = get_db()
    try:
        return conn.execute("SELECT * FROM admin_security WHERE admin_id=? LIMIT 1", (str(admin_id),)).fetchone()
    finally:
        conn.close()


def is_admin_authorized(admin_id: str | None) -> bool:
    if not admin_id:
        return False
    from database import get_db

    conn = get_db()
    try:
        row = conn.execute(
            """
            SELECT a.admin_id, s.is_active, s.totp_enabled
            FROM admin AS a
            JOIN admin_security AS s ON s.admin_id=a.admin_id
            WHERE a.admin_id=? LIMIT 1
            """,
            (str(admin_id),),
        ).fetchone()
        return bool(row and int(row["is_active"] or 0) == 1 and int(row["totp_enabled"] or 0) == 1)
    finally:
        conn.close()


def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _decode_totp_secret(secret: str) -> bytes:
    cleaned = str(secret).strip().replace(" ", "").upper()
    padding = "=" * ((8 - len(cleaned) % 8) % 8)
    return base64.b32decode(cleaned + padding, casefold=True)


def _totp_code(secret: str, counter: int) -> str:
    key = _decode_totp_secret(secret)
    digest = hmac.new(key, struct.pack(">Q", int(counter)), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    binary = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(binary % (10 ** TOTP_DIGITS)).zfill(TOTP_DIGITS)


def verify_totp(secret: str, code: str, now: int | None = None, window: int = 1) -> int | None:
    """Return matching time counter or None; accepts current +/- one 30s step."""
    code = str(code or "").strip()
    if not re.fullmatch(r"\d{6}", code):
        return None
    current = int(time.time() if now is None else now) // TOTP_PERIOD_SECONDS
    matched_counter = None
    for counter in range(current - window, current + window + 1):
        if counter >= 0 and hmac.compare_digest(_totp_code(secret, counter), code):
            # Prefer the newest matching time step if an extremely rare
            # six-digit collision occurs inside the accepted drift window.
            matched_counter = counter
    return matched_counter


def totp_provisioning_uri(admin_id: str, email: str, secret: str) -> str:
    label = quote(f"Sivdhi:{email or admin_id}", safe="")
    issuer = quote("Sivdhi", safe="")
    return f"otpauth://totp/{label}?secret={quote(secret)}&issuer={issuer}&algorithm=SHA1&digits=6&period=30"


def get_or_create_pending_totp_secret(admin_id: str) -> str:
    from database import get_db

    conn = get_db()
    try:
        conn.execute("INSERT OR IGNORE INTO admin_security (admin_id) VALUES (?)", (str(admin_id),))
        row = conn.execute("SELECT is_active, totp_enabled, pending_secret_enc FROM admin_security WHERE admin_id=?", (str(admin_id),)).fetchone()
        if not row or not int(row["is_active"] or 0) or int(row["totp_enabled"] or 0):
            raise PermissionError("Admin MFA enrollment is not available for this account.")
        encrypted = row["pending_secret_enc"]
        if not encrypted:
            secret = new_totp_secret()
            encrypted = encrypt_totp_secret(secret)
            conn.execute("UPDATE admin_security SET pending_secret_enc=?, updated_at=CURRENT_TIMESTAMP WHERE admin_id=?", (encrypted, str(admin_id)))
            conn.commit()
            return secret
        conn.commit()
        return decrypt_totp_secret(encrypted)
    finally:
        conn.close()


def _new_recovery_codes() -> list[str]:
    return [secrets.token_hex(16).upper() for _ in range(RECOVERY_CODE_COUNT)]


def complete_admin_mfa_enrollment(admin_id: str, code: str, now: int | None = None) -> list[str] | None:
    """Atomically verify first TOTP, enable MFA, and replace recovery codes."""
    from database import get_db

    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT is_active, totp_enabled, pending_secret_enc FROM admin_security WHERE admin_id=?", (str(admin_id),)).fetchone()
        if not row or not int(row["is_active"] or 0) or int(row["totp_enabled"] or 0) or not row["pending_secret_enc"]:
            conn.rollback()
            return None
        secret = decrypt_totp_secret(row["pending_secret_enc"])
        counter = verify_totp(secret, code, now=now)
        if counter is None:
            conn.rollback()
            return None
        codes = _new_recovery_codes()
        conn.execute("DELETE FROM admin_recovery_codes WHERE admin_id=?", (str(admin_id),))
        conn.executemany(
            "INSERT INTO admin_recovery_codes (admin_id, code_digest) VALUES (?, ?)",
            [(str(admin_id), _recovery_digest(admin_id, value)) for value in codes],
        )
        conn.execute(
            """UPDATE admin_security SET totp_secret_enc=pending_secret_enc,
               pending_secret_enc=NULL, totp_enabled=1, last_totp_counter=?,
               updated_at=CURRENT_TIMESTAMP
               WHERE admin_id=? AND is_active=1 AND totp_enabled=0""",
            (counter, str(admin_id)),
        )
        if conn.execute("SELECT changes()").fetchone()[0] != 1:
            conn.rollback()
            return None
        conn.commit()
        return codes
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def verify_and_consume_totp(admin_id: str, code: str, now: int | None = None) -> bool:
    """Verify TOTP and atomically reject a counter that was already used."""
    from database import get_db

    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT is_active, totp_enabled, totp_secret_enc, last_totp_counter FROM admin_security WHERE admin_id=?",
            (str(admin_id),),
        ).fetchone()
        if not row or not int(row["is_active"] or 0) or not int(row["totp_enabled"] or 0) or not row["totp_secret_enc"]:
            conn.rollback()
            return False
        secret = decrypt_totp_secret(row["totp_secret_enc"])
        counter = verify_totp(secret, code, now=now)
        last_counter = row["last_totp_counter"]
        if counter is None or counter <= int(last_counter if last_counter is not None else -1):
            conn.rollback()
            return False
        conn.execute(
            """UPDATE admin_security SET last_totp_counter=?, updated_at=CURRENT_TIMESTAMP
               WHERE admin_id=? AND is_active=1 AND totp_enabled=1 AND last_totp_counter < ?""",
            (counter, str(admin_id), counter),
        )
        if conn.execute("SELECT changes()").fetchone()[0] != 1:
            conn.rollback()
            return False
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def consume_admin_recovery_code(admin_id: str, code: str) -> bool:
    """Consume exactly one unused recovery code, even under concurrent requests."""
    from database import get_db

    normalised = re.sub(r"[^A-Fa-f0-9]", "", str(code or ""))
    if not re.fullmatch(r"[A-Fa-f0-9]{32}", normalised):
        return False
    digest = _recovery_digest(admin_id, normalised)
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT id FROM admin_recovery_codes WHERE admin_id=? AND code_digest=? AND used_at IS NULL LIMIT 1",
            (str(admin_id), digest),
        ).fetchone()
        if not row:
            conn.rollback()
            return False
        conn.execute("UPDATE admin_recovery_codes SET used_at=? WHERE id=? AND used_at IS NULL", (datetime.now(timezone.utc).isoformat(), row["id"]))
        if conn.execute("SELECT changes()").fetchone()[0] != 1:
            conn.rollback()
            return False
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def set_admin_active(admin_id: str, active: bool) -> bool:
    """Administrative kill switch; disabling takes effect on the next request."""
    from database import get_db

    conn = get_db()
    try:
        cur = conn.execute(
            "UPDATE admin_security SET is_active=?, updated_at=CURRENT_TIMESTAMP WHERE admin_id=?",
            (1 if active else 0, str(admin_id)),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()
