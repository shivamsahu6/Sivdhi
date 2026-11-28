import sqlite3
from pathlib import Path
from unittest.mock import patch

from utils import worker_identity


def test_worker_id_verification_table_is_additive_idempotent_and_scoped():
    conn = sqlite3.connect(":memory:")
    try:
        worker_identity.ensure_worker_id_verification_schema(conn)
        worker_identity.ensure_worker_id_verification_schema(conn)
        columns = {
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(booking_worker_id_verifications)"
            ).fetchall()
        }
        assert {"booking_id", "customer_id", "worker_id", "verified_at"}.issubset(columns)

        conn.execute(
            """
            INSERT INTO booking_worker_id_verifications
                (booking_id, customer_id, worker_id)
            VALUES (91, 12, 7)
            """
        )
        assert worker_identity.is_worker_id_verified(conn, 91, 12, 7) is True
        assert worker_identity.is_worker_id_verified(conn, 91, 12, 8) is False
        assert worker_identity.is_worker_id_verified(conn, 91, 13, 7) is False
        assert worker_identity.is_worker_id_verified(conn, 92, 12, 7) is False
    finally:
        conn.close()


def test_worker_id_card_svg_contains_worker_identity_and_escapes_untrusted_fields():
    card = worker_identity.build_worker_id_card_svg({
        "fullname": "Ravi <script>alert(1)</script>",
        "worker_id": "SW10021",
        "skills": "AC Repair, Electrician",
        "profile_photo": "",
    })

    assert "Ravi &lt;script&gt;alert(1)&lt;/script&gt;" in card
    assert "SW10021" in card
    assert "AC Repair" in card
    assert "Electrician" in card
    assert "Show this card to the customer at every service visit." in card
    assert "<script>" not in card
    assert "data:image/" not in card


def test_worker_id_card_embeds_only_managed_profile_image_and_uses_safe_filename():
    with patch.object(worker_identity, "WORKER_UPLOAD_DIR", Path("/tmp/sivdhi-worker-id-test")):
        root = Path("/tmp/sivdhi-worker-id-test")
        root.mkdir(parents=True, exist_ok=True)
        image_path = root / "worker-card-test.jpg"
        image_path.write_bytes(b"\xff\xd8\xfftest")
        try:
            with patch.object(worker_identity, "WORKER_UPLOAD_DIR", root):
                card = worker_identity.build_worker_id_card_svg({
                    "fullname": "Ravi Kumar",
                    "worker_id": "SW10021",
                    "skills": "Plumbing",
                    "profile_photo": "worker-card-test.jpg",
                })
            assert "data:image/jpeg;base64," in card
            assert worker_identity.worker_id_card_filename({
                "worker_id": "SW/10021 ?"
            }) == "Sivdhi-Worker-ID-SW10021.svg"
        finally:
            image_path.unlink(missing_ok=True)
            root.rmdir()


def test_worker_profile_has_card_preview_and_download_action():
    root = Path(__file__).resolve().parents[1]
    template = (root / "templates" / "worker" / "profile.html").read_text(encoding="utf-8")
    route = (root / "routes" / "worker.py").read_text(encoding="utf-8")

    assert "worker.worker_id_card_preview" in template
    assert "worker.download_worker_id_card" in template
    assert "Mandatory: Customer must see and verify your Sivdhi ID card" in template
    assert '"/worker/id-card.svg"' in route
    assert '"/worker/id-card/download"' in route
    assert 'disposition = "attachment" if download else "inline"' in route


def test_customer_can_see_assigned_worker_card_and_verify_after_arrival():
    root = Path(__file__).resolve().parents[1]
    template = (root / "templates" / "customer" / "bookings.html").read_text(encoding="utf-8")
    route = (root / "routes" / "customer.py").read_text(encoding="utf-8")

    assert "Your Assigned Sivdhi Worker" in template
    assert "Worker ID:" in template
    assert "I Checked the ID Card - Verify Worker" in template
    assert "booking['worker_arrived_at']" in template
    assert "booking['worker_id_verified']" in template
    assert '"/booking/<int:booking_db_id>/verify-worker-id"' in route
    assert "booking_worker_id_verifications" in route


def test_worker_and_partner_cannot_start_without_the_matching_customer_id_verification():
    root = Path(__file__).resolve().parents[1]
    worker = (root / "routes" / "worker.py").read_text(encoding="utf-8")
    partner = (root / "routes" / "partner.py").read_text(encoding="utf-8")
    worker_template = (root / "templates" / "worker" / "job_details.html").read_text(encoding="utf-8")
    partner_template = (root / "templates" / "partner" / "job_details.html").read_text(encoding="utf-8")

    worker_start = worker.split("def start_job(job_id):", 1)[1].split(
        '@worker_bp.route("/worker/job/<job_id>/complete"', 1
    )[0]
    partner_start = partner.split("def partner_start_job(booking_id):", 1)[1].split(
        '@partner_bp.route("/partner/job/<int:booking_id>/complete"', 1
    )[0]

    assert "is_worker_id_verified(" in worker_start
    assert "is_worker_id_verified(" in partner_start
    assert "worker_id_verified" in worker_template
    assert 'job["worker_id_verified_at"]' in partner_template


def test_worker_partner_customer_are_all_informed_about_required_id_presentation():
    root = Path(__file__).resolve().parents[1]
    worker_profile = (root / "templates" / "worker" / "profile.html").read_text(encoding="utf-8")
    worker_job = (root / "templates" / "worker" / "job_details.html").read_text(encoding="utf-8")
    partner_home = (root / "templates" / "partner" / "home.html").read_text(encoding="utf-8")
    partner_job = (root / "templates" / "partner" / "job_details.html").read_text(encoding="utf-8")
    customer_bookings = (root / "templates" / "customer" / "bookings.html").read_text(encoding="utf-8")
    worker_route = (root / "routes" / "worker.py").read_text(encoding="utf-8")
    partner_route = (root / "routes" / "partner.py").read_text(encoding="utf-8")

    assert "show it to every customer" in worker_profile
    assert "Show your Sivdhi Worker ID card to the customer" in worker_job
    assert "Worker ID Card Rule" in partner_home
    assert "Partner responsibility" in partner_job
    assert "Worker identity safety" in customer_bookings
    assert "Verify Your Worker ID Card" in worker_route
    assert "Verify Your Worker ID Card" in partner_route


def test_full_day_auto_completion_only_processes_jobs_that_have_started():
    root = Path(__file__).resolve().parents[1]
    monitor = (root / "utils" / "assignment_monitor.py").read_text(encoding="utf-8")
    start = monitor.split("def _auto_complete_full_day_jobs", 1)[1].split(
        "def _process_offer_pool", 1
    )[0]
    assert "IN ('started','in_progress')" in start
    assert "AND worker_arrived_at IS NOT NULL" in start
