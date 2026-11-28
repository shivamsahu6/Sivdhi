from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(relative_path):
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_customer_booking_shows_only_a_compact_worker_id_card_button():
    template = _read("templates/customer/bookings.html")
    start = template.index("{% if booking['worker_id_card'] %}")
    end = template.index("{% endif %}", start) + len("{% endif %}")
    block = template[start:end]

    assert 'class="worker-id-view"' in block
    assert "View Worker ID Card" in block
    assert "worker-id-photo" not in block
    assert "worker-id-name" not in block
    assert "worker-id-skills" not in block
    assert "I Checked the ID Card" not in block
    assert "worker_id_card_url" in block


def test_worker_profile_places_downloadable_id_card_below_personal_details():
    template = _read("templates/worker/profile.html")

    details_end = template.index("</main>", template.index('<main class="card">'))
    id_card_start = template.index('<section class="worker-id-section"')
    nav_start = template.index('<nav class="bottom"')

    assert details_end < id_card_start < nav_start
    assert "Download Worker ID Card" in template
    assert "Show this ID card to the customer whenever you arrive" in template
    assert "Customer must see and verify" not in template


def test_worker_partner_login_footer_is_pushed_to_the_bottom():
    template = _read("templates/login.html")

    assert "min-height: 100dvh;" in template
    assert "flex-direction: column;" in template
    assert "justify-content: flex-start;" in template
    assert ".login-box {" in template
    assert "margin: clamp(20px, 5vh, 44px) auto 0;" in template
    assert '.sivdhi-site-footer[data-role="worker"]' in template
    assert '.sivdhi-site-footer[data-role="partner"]' in template
    assert "margin: auto auto 0;" in template


def test_worker_partner_signup_footer_is_pushed_to_the_bottom():
    template = _read("templates/signup.html")

    assert "min-height: 100dvh;" in template
    assert "display: flex;" in template
    assert "flex-direction: column;" in template
    assert "justify-content: flex-start;" in template
    assert ".signup-box {" in template
    assert "margin: clamp(20px, 5vh, 44px) auto 0;" in template
    assert '.sivdhi-site-footer[data-role="worker"]' in template
    assert '.sivdhi-site-footer[data-role="partner"]' in template
    assert "margin: auto auto 0;" in template
