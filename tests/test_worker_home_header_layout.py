from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_worker_home_groups_welcome_text_with_sivdhi_logo():
    template = (ROOT / "templates" / "worker" / "home.html").read_text(encoding="utf-8")

    header_start = template.index('<div class="header-top">')
    brand_start = template.index('<div class="header-brand">', header_start)
    logo_start = template.index('<img class="dashboard-logo"', brand_start)
    copy_start = template.index('<div class="header-copy">', logo_start)
    welcome_start = template.index("<h2>Welcome Worker", copy_start)
    brand_end = template.index("</div>\n\n            <a", welcome_start)
    notification_start = template.index('class="notification-bell"', brand_end)

    assert header_start < brand_start < logo_start < copy_start < welcome_start
    assert brand_end < notification_start
    assert "display: flex;" in template[template.index(".header-brand {"):template.index(".header-copy {")]
    assert "gap: 14px;" in template[template.index(".header-brand {"):template.index(".header-copy {")]
