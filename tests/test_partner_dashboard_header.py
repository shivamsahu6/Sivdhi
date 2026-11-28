from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_partner_dashboard_welcome_identity_is_left_aligned_beside_logo():
    template = (ROOT / "templates" / "partner" / "home.html").read_text(
        encoding="utf-8"
    )

    assert '<img class="dashboard-logo"' in template
    assert '<div class="header-copy">' in template
    assert "<h2>Welcome Partner 👋</h2>" in template
    assert "<p>ID : {{ session.user_id }}</p>" in template
    assert "flex: 1 1 auto;" in template
    assert "text-align: left;" in template
    assert "align-items: center;" in template
    assert 'class="notification-bell"' in template
