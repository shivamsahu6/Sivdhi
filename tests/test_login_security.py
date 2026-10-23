import sqlite3

from config import Config
from utils import login_security


def _use_db(monkeypatch, tmp_path):
    path = tmp_path / "login-security-tests.sqlite"
    monkeypatch.setattr(Config, "DATABASE", str(path))
    return path


def test_combination_lock_after_five_failures(monkeypatch, tmp_path):
    _use_db(monkeypatch, tmp_path)
    assert login_security.is_login_allowed("worker", "SV10001", "192.0.2.1")
    for _ in range(login_security.MAX_FAILURES):
        login_security.record_login_failure("worker", "SV10001", "192.0.2.1")
    assert not login_security.is_login_allowed("worker", "SV10001", "192.0.2.1")
    assert login_security.is_login_allowed("worker", "SV10001", "192.0.2.2")


def test_account_lock_applies_across_client_ips(monkeypatch, tmp_path):
    _use_db(monkeypatch, tmp_path)
    for i in range(login_security.MAX_ACCOUNT_FAILURES):
        login_security.record_login_failure("customer", "user@example.test", f"198.51.100.{i + 1}")
    assert not login_security.is_login_allowed("customer", "user@example.test", "203.0.113.200")


def test_ip_limit_applies_across_identifiers(monkeypatch, tmp_path):
    _use_db(monkeypatch, tmp_path)
    ip = "203.0.113.22"
    for i in range(login_security.MAX_IP_FAILURES):
        login_security.record_login_failure("admin", f"admin-{i}", ip)
    assert not login_security.is_login_allowed("admin", "a-new-id", ip)


def test_success_clears_identifier_counters_but_not_ip_counter(monkeypatch, tmp_path):
    path = _use_db(monkeypatch, tmp_path)
    scope, identifier, ip = "partner", "rp10001", "192.0.2.9"
    login_security.record_login_failure(scope, identifier, ip)
    login_security.clear_login_failures(scope, identifier, ip)
    assert login_security.is_login_allowed(scope, identifier, ip)

    # Successful login does not erase the IP-wide credential-stuffing history.
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM login_attempts").fetchone()[0] == 1
    finally:
        conn.close()


def test_identifiers_are_case_normalised(monkeypatch, tmp_path):
    _use_db(monkeypatch, tmp_path)
    login_security.record_login_failure("customer", "USER@EXAMPLE.TEST", "192.0.2.10")
    assert login_security.is_login_allowed("CUSTOMER", "user@example.test", "192.0.2.10")
    # One failure should still permit login; this confirms case variants share the same counter.
    for _ in range(login_security.MAX_FAILURES - 1):
        login_security.record_login_failure("customer", "user@example.test", "192.0.2.10")
    assert not login_security.is_login_allowed("customer", "user@example.test", "192.0.2.10")
