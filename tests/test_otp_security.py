"""Tests for shared OTP throttling and server-side pending-change state."""

from config import Config
from database import get_db
from utils import otp_security
from utils.pending_changes import (
    issue_pending_change,
    verify_pending_change,
    refresh_pending_change,
    delete_pending_change,
)


def _use_test_db(monkeypatch, tmp_path):
    monkeypatch.setattr(Config, "DATABASE", str(tmp_path / "otp-security-test.db"))


def test_otp_reservation_enforces_cooldown_across_parallel_requests(monkeypatch, tmp_path):
    _use_test_db(monkeypatch, tmp_path)
    clock = {"now": 1_800_000_000}
    monkeypatch.setattr(otp_security.time, "time", lambda: clock["now"])

    assert otp_security.reserve_otp_request("reset", "user@example.test", "10.0.0.1") == (True, 0)
    allowed, retry = otp_security.reserve_otp_request("reset", "user@example.test", "10.0.0.1")
    assert not allowed and retry > 0

    clock["now"] += 61
    assert otp_security.reserve_otp_request("reset", "user@example.test", "10.0.0.1") == (True, 0)


def test_otp_account_limit_is_shared_across_ips(monkeypatch, tmp_path):
    _use_test_db(monkeypatch, tmp_path)
    clock = {"now": 1_800_000_000}
    monkeypatch.setattr(otp_security.time, "time", lambda: clock["now"])

    assert otp_security.reserve_otp_request("verify", "user@example.test", "10.0.0.1")[0]
    # Account-level cooldown must apply even when a sender changes IP.
    allowed, retry = otp_security.reserve_otp_request("verify", "user@example.test", "10.0.0.2")
    assert not allowed and retry > 0


def test_otp_ip_limit_is_shared_across_identifiers(monkeypatch, tmp_path):
    _use_test_db(monkeypatch, tmp_path)
    clock = {"now": 1_800_000_000}
    monkeypatch.setattr(otp_security.time, "time", lambda: clock["now"])

    assert otp_security.reserve_otp_request("reset", "one@example.test", "10.0.0.3")[0]
    clock["now"] += 4
    assert otp_security.reserve_otp_request("reset", "two@example.test", "10.0.0.3")[0]
    conn = get_db()
    try:
        total = conn.execute("SELECT SUM(request_count) AS total FROM otp_requests").fetchone()["total"]
        assert total == 6  # two reservations × combo/account/IP dimensions
    finally:
        conn.close()


def test_pending_change_keeps_payload_server_side_and_consumes_otp_once(monkeypatch, tmp_path):
    _use_test_db(monkeypatch, tmp_path)
    clock = {"now": 1_800_000_000}
    monkeypatch.setattr("utils.pending_changes.time.time", lambda: clock["now"])

    token = issue_pending_change(
        "worker", "SV12345", "bank_change",
        {"account_number": "1234567890", "bank_name": "Example"}, "246810", ttl=300,
    )
    assert token and len(token) >= 32
    conn = get_db()
    try:
        row = conn.execute("SELECT token_hash, payload_json, otp_hash FROM pending_security_changes").fetchone()
        assert row["token_hash"] != token
        assert "246810" not in row["otp_hash"]
        assert "1234567890" in row["payload_json"]
    finally:
        conn.close()

    status, _payload, attempts = verify_pending_change("worker", "SV12345", "bank_change", token, "000000")
    assert (status, attempts) == ("invalid", 1)
    status, payload, _attempts = verify_pending_change("worker", "SV12345", "bank_change", token, "246810")
    assert status == "ok" and payload["account_number"] == "1234567890"
    # The OTP/challenge is atomically consumed and cannot be replayed.
    assert verify_pending_change("worker", "SV12345", "bank_change", token, "246810")[0] == "missing"
    assert delete_pending_change("worker", "SV12345", "bank_change", token) == 0


def test_pending_change_expiration_cannot_be_bypassed(monkeypatch, tmp_path):
    _use_test_db(monkeypatch, tmp_path)
    clock = {"now": 1_800_000_000}
    monkeypatch.setattr("utils.pending_changes.time.time", lambda: clock["now"])
    token = issue_pending_change("partner", "SP12345", "password_change", {"password_hash": "hash"}, "123456", ttl=5)
    clock["now"] += 6
    status, payload, _attempts = verify_pending_change("partner", "SP12345", "password_change", token, "123456")
    assert status == "expired" and payload is None


def test_pending_change_limits_wrong_otps(monkeypatch, tmp_path):
    _use_test_db(monkeypatch, tmp_path)
    clock = {"now": 1_800_000_000}
    monkeypatch.setattr("utils.pending_changes.time.time", lambda: clock["now"])
    token = issue_pending_change("partner", "SP54321", "password_change", {"password_hash": "hash"}, "123456")
    for _ in range(4):
        status, _payload, _attempts = verify_pending_change("partner", "SP54321", "password_change", token, "000000")
        assert status == "invalid"
    status, _payload, attempts = verify_pending_change("partner", "SP54321", "password_change", token, "000000")
    assert status == "locked" and attempts == 5
