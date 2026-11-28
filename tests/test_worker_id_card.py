from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from utils import worker_identity


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
    with TemporaryDirectory(prefix="sivdhi-worker-id-") as temp_dir:
        root = Path(temp_dir)
        image_path = root / "worker-card-test.jpg"
        image_path.write_bytes(b"\xff\xd8\xfftest")

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

        # A path traversal attempt must never embed a file outside the upload directory.
        with patch.object(worker_identity, "WORKER_UPLOAD_DIR", root):
            unsafe = worker_identity.build_worker_id_card_svg({
                "fullname": "Ravi Kumar",
                "worker_id": "SW10021",
                "profile_photo": "../secret.jpg",
            })
        assert "data:image/" not in unsafe


def test_worker_profile_has_card_preview_and_download_action():
    root = Path(__file__).resolve().parents[1]
    template = (root / "templates" / "worker" / "profile.html").read_text(encoding="utf-8")
    route = (root / "routes" / "worker.py").read_text(encoding="utf-8")

    assert "worker.worker_id_card_preview" in template
    assert "worker.download_worker_id_card" in template
    assert "Download Worker ID Card" in template
    assert "show it to every customer when you arrive" in template
    assert '"/worker/id-card.svg"' in route
    assert '"/worker/id-card/download"' in route
    assert 'disposition = "attachment" if download else "inline"' in route


def test_customer_can_view_assigned_worker_id_card_without_confirming_it():
    root = Path(__file__).resolve().parents[1]
    template = (root / "templates" / "customer" / "bookings.html").read_text(encoding="utf-8")
    route = (root / "routes" / "customer.py").read_text(encoding="utf-8")

    assert "Your Assigned Sivdhi Worker" in template
    assert "Worker ID:" in template
    assert "View Official Worker ID Card" in template
    assert "booking['worker_id_card_url']" in template
    assert '"/booking/<int:booking_db_id>/worker-id-card.svg"' in route
    assert "WHERE id=? AND customer_id=?" in route
    assert "build_worker_id_card_svg(worker)" in route
    assert "verify_booking_worker_id" not in route
    assert "I Checked the ID Card" not in template
    assert "worker_id_verified" not in template


def test_worker_and_partner_start_routes_keep_only_existing_arrival_and_payment_gates():
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

    assert 'if not current["worker_arrived_at"]' in worker_start
    assert "booking_is_fully_paid(current)" in worker_start
    assert "is_worker_id_verified" not in worker_start
    assert "if not row[\"worker_arrived_at\"]" in partner_start
    assert "booking_is_fully_paid(row)" in partner_start
    assert "is_worker_id_verified" not in partner_start
    assert "worker_id_verified" not in worker_template
    assert "worker_id_verified_at" not in partner_template


def test_worker_partner_and_customer_are_informed_that_worker_should_show_id_card():
    root = Path(__file__).resolve().parents[1]
    worker_profile = (root / "templates" / "worker" / "profile.html").read_text(encoding="utf-8")
    worker_job = (root / "templates" / "worker" / "job_details.html").read_text(encoding="utf-8")
    partner_home = (root / "templates" / "partner" / "home.html").read_text(encoding="utf-8")
    partner_job = (root / "templates" / "partner" / "job_details.html").read_text(encoding="utf-8")
    customer_bookings = (root / "templates" / "customer" / "bookings.html").read_text(encoding="utf-8")
    worker_route = (root / "routes" / "worker.py").read_text(encoding="utf-8")
    partner_route = (root / "routes" / "partner.py").read_text(encoding="utf-8")

    assert "show it to every customer when you arrive" in worker_profile
    assert "Show your official Sivdhi Worker ID Card to the customer" in worker_job
    assert "Worker ID Card Rule" in partner_home
    assert "Customer can view the same card in My Bookings" in partner_job
    assert "ask the assigned Worker to show their official Sivdhi ID card" in customer_bookings
    assert "Show Your Sivdhi ID Card" in worker_route
    assert "Show Sivdhi Worker ID" in partner_route
    assert "Verify Your Worker ID Card" not in worker_route
    assert "Verify Your Worker ID Card" not in partner_route
