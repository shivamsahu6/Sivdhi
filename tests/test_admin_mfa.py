import sqlite3

from cryptography.fernet import Fernet

from config import Config
from database import get_db
from utils import admin_mfa


def _setup_admin_db(monkeypatch, tmp_path):
    db_path = tmp_path / "admin-mfa-tests.sqlite"
    monkeypatch.setattr(Config, "DATABASE", str(db_path))
    monkeypatch.setenv("ADMIN_MFA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    admin_mfa._fernet_instance = None
    admin_mfa._fernet_key_material = None

    conn = get_db()
    try:
        conn.execute(
            """CREATE TABLE admin (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id TEXT UNIQUE NOT NULL,
                fullname TEXT NOT NULL,
                email TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        conn.execute(
            "INSERT INTO admin (admin_id, fullname, email, password) VALUES (?, ?, ?, ?)",
            ("ADMIN_TEST", "Test Admin", "admin@example.test", "test-password-hash"),
        )
        admin_mfa.ensure_admin_security_schema(conn)
        conn.commit()
    finally:
        conn.close()
    return db_path


def test_totp_generates_six_digit_codes_and_checks_time_window():
    secret = admin_mfa.new_totp_secret()
    counter = 60_000
    code = admin_mfa._totp_code(secret, counter)
    assert len(code) == 6
    assert code.isdigit()
    assert admin_mfa.verify_totp(secret, code, now=counter * 30, window=0) == counter
    assert admin_mfa.verify_totp(secret, "not-a-code", now=counter * 30) is None




def test_totp_matches_rfc6238_sha1_six_digit_vector():
    # RFC 6238 Appendix B SHA-1 test secret, truncated to six digits.
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    assert admin_mfa._totp_code(secret, 1) == "287082"
    assert admin_mfa.verify_totp(secret, "287082", now=59, window=0) == 1

def test_admin_mfa_enrollment_encrypts_secret_and_replay_is_rejected(monkeypatch, tmp_path):
    _setup_admin_db(monkeypatch, tmp_path)
    secret = admin_mfa.get_or_create_pending_totp_secret("ADMIN_TEST")

    conn = get_db()
    try:
        stored = conn.execute(
            "SELECT pending_secret_enc FROM admin_security WHERE admin_id='ADMIN_TEST'"
        ).fetchone()[0]
        assert stored != secret
        assert admin_mfa.decrypt_totp_secret(stored) == secret
    finally:
        conn.close()

    enrollment_counter = 60_100
    enrollment_code = admin_mfa._totp_code(secret, enrollment_counter)
    recovery_codes = admin_mfa.complete_admin_mfa_enrollment(
        "ADMIN_TEST", enrollment_code, now=enrollment_counter * 30
    )
    assert recovery_codes and len(recovery_codes) == admin_mfa.RECOVERY_CODE_COUNT
    assert all(len(value) == 32 for value in recovery_codes)
    assert admin_mfa.is_admin_authorized("ADMIN_TEST")

    used_counter = enrollment_counter + 1
    used_code = admin_mfa._totp_code(secret, used_counter)
    assert admin_mfa.verify_and_consume_totp("ADMIN_TEST", used_code, now=used_counter * 30)
    assert not admin_mfa.verify_and_consume_totp("ADMIN_TEST", used_code, now=used_counter * 30)


def test_recovery_codes_are_hashed_and_one_time(monkeypatch, tmp_path):
    db_path = _setup_admin_db(monkeypatch, tmp_path)
    secret = admin_mfa.get_or_create_pending_totp_secret("ADMIN_TEST")
    counter = 60_200
    codes = admin_mfa.complete_admin_mfa_enrollment(
        "ADMIN_TEST", admin_mfa._totp_code(secret, counter), now=counter * 30
    )
    assert codes

    conn = sqlite3.connect(db_path)
    try:
        stored_digests = [row[0] for row in conn.execute(
            "SELECT code_digest FROM admin_recovery_codes WHERE admin_id='ADMIN_TEST'"
        )]
        assert codes[0] not in stored_digests
    finally:
        conn.close()

    assert admin_mfa.consume_admin_recovery_code("ADMIN_TEST", codes[0])
    assert not admin_mfa.consume_admin_recovery_code("ADMIN_TEST", codes[0])


def test_disabled_admin_is_rejected_on_next_authorization_check(monkeypatch, tmp_path):
    _setup_admin_db(monkeypatch, tmp_path)
    secret = admin_mfa.get_or_create_pending_totp_secret("ADMIN_TEST")
    counter = 60_300
    codes = admin_mfa.complete_admin_mfa_enrollment(
        "ADMIN_TEST", admin_mfa._totp_code(secret, counter), now=counter * 30
    )
    assert codes
    assert admin_mfa.is_admin_authorized("ADMIN_TEST")
    assert admin_mfa.set_admin_active("ADMIN_TEST", False)
    assert not admin_mfa.is_admin_authorized("ADMIN_TEST")
    assert admin_mfa.set_admin_active("ADMIN_TEST", True)
    assert admin_mfa.is_admin_authorized("ADMIN_TEST")


def test_admin_security_schema_is_additive_and_idempotent(monkeypatch, tmp_path):
    _setup_admin_db(monkeypatch, tmp_path)
    conn = get_db()
    try:
        admin_mfa.ensure_admin_security_schema(conn)
        conn.commit()
        columns = {row[1] for row in conn.execute("PRAGMA table_info(admin)").fetchall()}
        assert columns == {"id", "admin_id", "fullname", "email", "password", "created_at"}
        assert conn.execute("SELECT COUNT(*) FROM admin_security").fetchone()[0] == 1
    finally:
        conn.close()
