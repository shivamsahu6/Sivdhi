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



def test_verified_cashfree_advance_auto_creates_booking_without_continue_button():
    template = _source("templates/customer/payment.html")
    customer_routes = _source("routes/customer.py")

    assert 'id="cashfreeAutoConfirmForm"' in template
    assert "cashfreeAutoConfirmForm.submit()" in template
    assert "payment_action" in template
    assert "Confirm booking" not in template
    assert "payment_action != \"confirm_cashfree\"" in customer_routes


def test_auto_completed_full_day_booking_can_collect_balance_and_settle_later():
    customer_routes = _source("routes/customer.py")
    balance_route = customer_routes.split("def pay_booking_balance(booking_ref):", 1)[1]
    balance_route = balance_route.split('@customer_bp.route("/api/category-services")', 1)[0]
    assert "auto_completed_full_day" in balance_route
    assert "settle_completed_booking" in balance_route

    cashfree_finalizer = customer_routes.split("def _finalize_cashfree_balance_payment(", 1)[1]
    cashfree_finalizer = cashfree_finalizer.split('@customer_bp.route("/cashfree/return"', 1)[0]
    assert "auto_completed_full_day" in cashfree_finalizer
    assert "settle_completed_booking" in cashfree_finalizer

    bookings_template = _source("templates/customer/bookings.html")
    assert "booking['auto_completed_at']" in bookings_template


def test_full_day_worker_and_partner_cannot_manually_complete_jobs():
    worker_routes = _source("routes/worker.py")
    worker_complete = worker_routes.split("def complete_job(job_id):", 1)[1]
    worker_complete = worker_complete.split("# ============================================================\n# WORKER", 1)[0]
    assert "Full-Day jobs are automatically completed at 5:00 PM." in worker_complete

    partner_routes = _source("routes/partner.py")
    partner_complete = partner_routes.split("def partner_complete_job(booking_id):", 1)[1]
    partner_complete = partner_complete.split('@partner_bp.route("/partner/job/<int:booking_id>/reject"', 1)[0]
    assert "Full-Day jobs are automatically completed at 5:00 PM." in partner_complete

    worker_template = _source("templates/worker/job_details.html")
    partner_template = _source("templates/partner/job_details.html")
    assert "booking_mode == 'full-day'" in worker_template
    assert 'job_mode == "full-day"' in partner_template

def test_checkout_payment_button_is_for_advance_not_full_amount():
    source = _source("templates/customer/payment.html")
    assert "Pay ₹{{ '%.2f'|format(advance_value|float) }} securely" in source
    assert "Record ₹{{ '%.2f'|format(advance_value|float) }} demo advance" in source
    assert "Balance at worker arrival" in source

def test_cashfree_balance_payment_button_is_only_rendered_in_cashfree_mode():
    bookings_template = _source("templates/customer/bookings.html")
    customer_routes = _source("routes/customer.py")
    assert "cashfree-balance-button" in bookings_template
    assert "create_cashfree_balance_order" in bookings_template
    assert "cashfree_mode = not demo_mode and cashfree_enabled()" in customer_routes
    assert "Cashfree Sandbox checkout only. No real money will be collected." in bookings_template

def test_worker_arrival_unlocks_one_hour_before_booking():
    source = _source("routes/worker.py")
    arrival = source.split("def worker_arrived(job_id):", 1)[1]
    arrival = arrival.split('@worker_bp.route("/worker/job/<job_id>/start"', 1)[0]
    assert "arrival_allowed_at = scheduled_at - timedelta(hours=1)" in arrival
    assert "one hour before the customer's booked time" in arrival
    assert "scheduled_at + timedelta(hours=1)" not in arrival


def test_worker_start_requires_arrival_and_paid_balance_not_booking_time():
    source = _source("routes/worker.py")
    start = source.split("def start_job(job_id):", 1)[1]
    start = start.split("# ============================================================\\n# COMPLETE JOB", 1)[0]
    assert 'if not current["worker_arrived_at"]:' in start
    assert "remaining balance" in start
    assert "scheduled_at" not in start


def test_worker_detail_and_jobs_list_follow_arrival_payment_flow():
    route_source = _source("routes/worker.py")
    detail_route = route_source.split("def job_details(", 1)[1]
    detail_route = detail_route.split("# ============================================================\\n# WORKER AVAILABILITY HEARTBEAT", 1)[0]
    assert "arrive_available_at = scheduled_at - timedelta(hours=1)" in detail_route
    assert "can_arrive=can_arrive" in detail_route

    detail_template = _source("templates/worker/job_details.html")
    assert "{% if can_arrive %}" in detail_template
    assert "runAction('arrived', this)" in detail_template
    assert "Waiting for the customer to pay the remaining" in detail_template

    jobs_template = _source("templates/worker/jobs.html")
    accepted = jobs_template.split("{% elif status == 'accepted' %}", 1)[1]
    accepted = accepted.split("{% elif status in ['started', 'in_progress'] %}", 1)[0]
    assert 'data-action="start"' not in accepted
    assert "remaining balance" in accepted


def test_partner_arrival_unlocks_one_hour_before_booking():
    source = _source("routes/partner.py")
    arrival = source.split("def partner_arrive_job(booking_id):", 1)[1]
    arrival = arrival.split('@partner_bp.route("/partner/job/<int:booking_id>/start"', 1)[0]
    assert "allowed_at = scheduled_at - timedelta(hours=1)" in arrival
    assert "one hour before the customer's booked time" in arrival
    assert "scheduled_at + timedelta(hours=1)" not in arrival
    assert "arrive_available_at = scheduled_at - timedelta(hours=1)" in source


def test_partner_start_requires_arrival_and_paid_balance_not_booking_time():
    source = _source("routes/partner.py")
    start = source.split("def partner_start_job(booking_id):", 1)[1]
    start = start.split('@partner_bp.route("/partner/job/<int:booking_id>/complete"', 1)[0]
    assert 'if not row["worker_arrived_at"]:' in start
    assert "_partner_booking_balance_due(row)" in start
    assert "balance_payment_status" in start
    assert "scheduled_at" not in start
