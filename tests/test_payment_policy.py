import os

from utils.sivdhi_finance import is_demo_mode


def test_demo_mode_allowed_in_development(monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("SIVDHI_PAYMENT_MODE", "demo")
    monkeypatch.setenv("SIVDHI_ALLOW_DEMO_PAYMENTS", "1")
    monkeypatch.delenv("SIVDHI_ALLOW_DEMO_IN_PRODUCTION", raising=False)
    assert is_demo_mode() is True


def test_demo_mode_disabled_by_default_in_production(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SIVDHI_PAYMENT_MODE", "demo")
    monkeypatch.setenv("SIVDHI_ALLOW_DEMO_PAYMENTS", "1")
    monkeypatch.delenv("SIVDHI_ALLOW_DEMO_IN_PRODUCTION", raising=False)
    assert is_demo_mode() is False


def test_demo_mode_production_override_is_explicit(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SIVDHI_PAYMENT_MODE", "demo")
    monkeypatch.setenv("SIVDHI_ALLOW_DEMO_PAYMENTS", "1")
    monkeypatch.setenv("SIVDHI_ALLOW_DEMO_IN_PRODUCTION", "1")
    assert is_demo_mode() is True


def test_live_mode_never_counts_as_demo(monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("SIVDHI_PAYMENT_MODE", "live")
    monkeypatch.setenv("SIVDHI_ALLOW_DEMO_PAYMENTS", "1")
    assert is_demo_mode() is False


# Regression checks for the advance-now / balance-after-arrival policy.
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _source(relative_path):
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_checkout_records_only_advance_and_leaves_balance_due():
    source = _source("routes/customer.py")
    checkout = source.split("# JSON ENDPOINTS FOR THE CUSTOMER FLOW", 1)[0]
    assert "advance_amount = money(min(ADVANCE_CAP, total))" in checkout
    assert '"advance_paid_amount": advance_amount' in checkout
    assert '"balance_paid_amount": 0' in checkout
    assert '"balance_payment_status": "DUE" if balance_due > 0 else advance_status' in checkout
    assert 'db, booking_id, "customer_advance", advance_amount' in checkout
    assert "Baaki amount worker ke pahunchne par due hoga" in checkout


def test_balance_payment_endpoint_is_post_only_and_customer_authenticated():
    source = _source("routes/customer.py")
    marker = '@customer_bp.route("/booking/<booking_ref>/balance-payment", methods=["POST"])'
    assert marker in source
    segment = source.split(marker, 1)[1].split("# JSON ENDPOINTS", 1)[0]
    assert "@customer_login_required" in segment[:180]
    assert "def pay_booking_balance(booking_ref):" in segment[:300]
    assert "WHERE customer_id = ?" in segment


def test_balance_payment_is_blocked_until_worker_arrives():
    source = _source("routes/customer.py")
    segment = source.split("def pay_booking_balance(booking_ref):", 1)[1]
    segment = segment.split("@customer_bp.route", 1)[0]
    assert 'if not booking["worker_arrived_at"]:' in segment
    assert "worker_arrived_at IS NOT NULL" in segment
    assert "if not demo_mode:" in segment
    assert "Live balance payment is not enabled yet. No payment was taken." in segment


def test_worker_arrival_only_records_arrival_not_balance_payment():
    source = _source("routes/worker.py")
    segment = source.split("def worker_arrived(job_id):", 1)[1]
    segment = segment.split('@worker_bp.route("/worker/job/<job_id>/start"', 1)[0]
    assert "SET worker_arrived_at=COALESCE(worker_arrived_at, CURRENT_TIMESTAMP)" in segment
    assert "balance_due" in segment
    assert "balance_payment_status = 'DEMO_PAID'" not in segment
    assert "No balance payment was recorded by the arrival action." in segment


def test_worker_cannot_start_service_before_remaining_balance_is_recorded():
    source = _source("routes/worker.py")
    segment = source.split("def start_job(job_id):", 1)[1]
    segment = segment.split("# ============================================================\n# COMPLETE JOB", 1)[0]
    assert "if not current[\"worker_arrived_at\"]:" in segment
    assert "customer must pay/record the remaining balance" in segment


def test_customer_ui_reveals_balance_action_only_after_arrival():
    source = _source("templates/customer/bookings.html")
    assert "if booking['worker_arrived_at'] and balance_due > 0" in source
    assert "elif not booking['worker_arrived_at'] and balance_due > 0" in source
    assert "Record ₹{{ '%.2f'|format(balance_due) }} demo balance" in source


def test_worker_ui_has_arrival_action_and_balance_wait_state():
    source = _source("templates/worker/job_details.html")
    assert "arrived: `/worker/job/${encodeURIComponent(jobId)}/arrived`" in source
    assert "runAction('arrived', this)" in source
    assert "Waiting for the customer to pay the remaining" in source


def test_checkout_payment_button_is_for_advance_not_full_amount():
    source = _source("templates/customer/payment.html")
    assert "Pay a ₹50 booking advance now" in source
    assert "Record ₹{{ '%.2f'|format(advance_value|float) }} demo advance" in source
    assert "Balance at worker arrival" in source
