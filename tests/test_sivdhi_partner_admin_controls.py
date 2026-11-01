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
    assert 'if "last_seen_at" in columns and partner_override != 1' in customer

def test_partner_can_control_all_four_job_actions():
    partner = source("routes/partner.py")
    template = source("templates/partner/job_details.html")
    for action in ("accept", "arrive", "start", "complete"):
        assert f"/partner/job/<int:booking_id>/{action}" in partner
        assert f"/partner/job/{{{{ job['id'] }}}}/{action}" in template

def test_partner_job_actions_follow_arrival_payment_and_settlement_guards():
    partner = source("routes/partner.py")
    assert "scheduled_at + timedelta(hours=1)" in partner
    assert 'if not row["worker_arrived_at"]' in partner
    assert "balance_payment_status" in partner
    assert "settle_completed_booking" in partner

def test_admin_manual_refund_requires_cancelled_booking_and_records_reference():
    admin = source("routes/admin.py")
    template = source("templates/admin/booking_details.html")
    assert '"/admin/booking/<int:booking_id>/refund"' in admin
    assert 'booking_status not in {"cancelled", "canceled"}' in admin
    assert "refund_reference" in admin
    assert "manual_bank_transfer" in admin
    assert "Refund Processed" in admin
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


def test_unassigned_booking_refund_waits_for_admin_bank_transfer():
    monitor = source("utils/assignment_monitor.py")
    segment = monitor.split("def _finalize_unassigned_booking", 1)[1].split("def _worker_attempt_status", 1)[0]
    assert "pending_admin" in segment
    assert "Admin bank transfer" in segment
    assert "add_finance_ledger_entry(" not in segment
    assert '"Booking Cancelled"' in segment
