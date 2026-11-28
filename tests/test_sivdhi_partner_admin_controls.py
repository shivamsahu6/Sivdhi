from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def source(path):
    return (ROOT / path).read_text(encoding="utf-8")

def test_partner_controls_worker_availability_without_worker_heartbeat():
    partner = source("routes/partner.py")
    worker = source("routes/worker.py")
    customer = source("routes/customer.py")
    assert '"/partner/worker/<worker_id>/availability"' in partner
    assert "partner_availability_override" in partner
    assert "partner_availability_override" in worker
    assert "3 * 24 * 60 * 60" in customer
    assert 'if not allow_offline and "last_seen_at" in columns and partner_override != 1' in customer

def test_partner_can_control_all_four_job_actions():
    partner = source("routes/partner.py")
    template = source("templates/partner/job_details.html")
    for action in ("accept", "arrive", "start", "complete"):
        assert f"/partner/job/<int:booking_id>/{action}" in partner
        assert f"/partner/job/{{{{ job['id'] }}}}/{action}" in template

def test_partner_job_actions_follow_arrival_payment_and_settlement_guards():
    partner = source("routes/partner.py")
    assert "allowed_at = scheduled_at - timedelta(hours=1)" in partner
    assert "Arrive will be available one hour before the customer's booked time." in partner

    start = partner.split("def partner_start_job(booking_id):", 1)[1]
    start = start.split('@partner_bp.route("/partner/job/<int:booking_id>/complete"', 1)[0]
    assert 'if not row["worker_arrived_at"]' in start
    assert "_partner_booking_balance_due(row)" in start
    assert "booking_is_fully_paid(row)" in start
    assert "scheduled_at" not in start

    assert "arrive_available_at = scheduled_at - timedelta(hours=1)" in partner
    assert "settle_completed_booking" in partner

def test_admin_manual_refund_requires_cancelled_booking_and_records_reference():
    admin = source("routes/admin.py")
    template = source("templates/admin/booking_details.html")
    assert '"/admin/booking/<int:booking_id>/refund"' in admin
    assert 'booking_status not in {"cancelled", "canceled"}' in admin
    assert "refund_reference" in admin
    assert "manual_bank_transfer" in admin
    assert "Refund Successful" in admin
    assert "Click the button only after the bank transfer is complete" in template

def test_worker_range_is_capped_at_thirty_km():
    worker = source("routes/worker.py")
    template = source("templates/worker/edit_profile.html")
    assert "service_radius_km > 30" in worker
    assert "Up to 30 km" in template
    assert "Up to 50 km" not in template

def test_customer_notifications_do_not_include_internal_assignment_reminders():
    monitor = source("utils/assignment_monitor.py")
    worker = source("routes/worker.py")
    customer = source("routes/customer.py")
    assert '"Booking Confirmation Pending"' not in monitor
    assert '"Worker Arrived"' not in worker or "user_type,user_id,title,message,is_read,created_at) VALUES('partner'" in worker
    assert '"Remaining Balance Recorded (Demo)"' not in customer


def test_unassigned_booking_refund_is_queued_or_left_for_admin_review():
    monitor = source("utils/assignment_monitor.py")
    segment = monitor.split("def _finalize_unassigned_booking", 1)[1].split("def _worker_attempt_status", 1)[0]
    assert "pending_admin" in segment
    assert "queue_cashfree_refunds_for_booking" in segment
    assert "Admin review" in segment
    assert "add_finance_ledger_entry(" not in segment
    assert '"Booking Cancelled"' in segment



def test_sivdhi_direct_is_protected_internal_partner_without_withdrawal():
    database = source("database.py")
    decorators = source("routes/decorators.py")
    partner = source("routes/partner.py")
    finance = source("utils/sivdhi_finance.py")
    admin = source("routes/admin.py")
    assert 'SIVDHI_DIRECT_PARTNER_ID = "SP00000"' in database
    assert 'SIVDHI_DIRECT_REFERRAL_CODE = "SV00000"' in database
    assert 'SIVDHI_DIRECT_EMAIL = "shivamsahu.co@gmail.com"' in database
    assert 'SIVDHI_DIRECT_MOBILE = "8954015683"' in database
    assert 'session["is_internal_partner"] = bool(partner["is_internal"] or 0)' in decorators
    assert "Withdrawals are disabled for the Sivdhi Direct company account." in partner
    assert "Bank details cannot be added to the Sivdhi Direct company account." in partner
    assert 'if partner_id == "SP00000":' in finance
    assert "protected internal account and cannot be suspended" in admin
    assert "protected internal account and cannot be deleted" in admin


def test_sivdhi_direct_internal_wallet_controls_are_not_rendered():
    home = source("templates/partner/home.html")
    wallet = source("templates/partner/wallet.html")
    profile = source("templates/partner/profile.html")
    assert "is_internal_partner" in home
    assert "Partner commission is retained by Sivdhi" in home
    assert "is_internal_partner" in wallet
    assert "Partner commission from its workers is retained by Sivdhi" in wallet
    assert "{% if not is_internal_partner %}" in profile
