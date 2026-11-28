from urllib.parse import parse_qs, urlsplit

from routes.partner import build_partner_worker_referral_link


def test_partner_referral_link_targets_worker_signup_with_exact_partner_code():
    link = build_partner_worker_referral_link("  partner-abc123 ")
    parsed = urlsplit(link)

    assert parsed.scheme == "https"
    assert parsed.netloc == "sivdhi.duckdns.org"
    assert parsed.path == "/signup"
    assert parse_qs(parsed.query) == {
        "role": ["worker"],
        "referral_code": ["PARTNER-ABC123"],
    }


def test_missing_partner_referral_code_does_not_create_a_share_link():
    assert build_partner_worker_referral_link("") == ""
    assert build_partner_worker_referral_link(None) == ""


def test_worker_signup_prefills_partner_code_into_visible_and_submitted_fields():
    from app import app

    client = app.test_client()
    response = client.get(
        "/signup?role=worker&referral_code=PARTNER-ABC123",
        follow_redirects=False,
    )

    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert 'name="referral_code"' in page
    assert 'name="signup_default_referral_code"' in page
    assert page.count('value="PARTNER-ABC123"') == 2


def test_worker_signup_keeps_default_sivdhi_referral_when_no_partner_link_is_used():
    from app import app

    client = app.test_client()
    response = client.get("/signup?role=worker", follow_redirects=False)

    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert page.count('value="SV00000"') == 2


def test_partner_profile_displays_copy_and_whatsapp_referral_actions():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    template = (root / "templates" / "partner" / "profile.html").read_text(
        encoding="utf-8"
    )
    route = (root / "routes" / "partner.py").read_text(encoding="utf-8")

    assert 'id="partnerReferralLink"' in template
    assert 'id="copyPartnerReferralLink"' in template
    assert "https://wa.me/?text=" in template
    assert "build_partner_worker_referral_link" in route
    assert "referral_link=referral_link" in route
