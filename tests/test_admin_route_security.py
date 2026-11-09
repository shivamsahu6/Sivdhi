from routes.admin import (
    ADMIN_PANEL_BASE,
    ADMIN_IDLE_TIMEOUT_SECONDS,
    ADMIN_ABSOLUTE_TIMEOUT_SECONDS,
    _admin_session_is_expired,
)


def test_admin_route_group_is_not_exposed_at_default_admin_paths():
    from app import app

    rules = {rule.rule for rule in app.url_map.iter_rules()}
    assert f"{ADMIN_PANEL_BASE}/admin/login" in rules
    assert "/admin/login" not in rules
    assert "/admin/dashboard" not in rules
    assert f"{ADMIN_PANEL_BASE}/bootstrap" in rules
    assert f"{ADMIN_PANEL_BASE}/admin/mfa/setup" in rules
    assert f"{ADMIN_PANEL_BASE}/admin/mfa/verify" in rules
    assert "/setup-admin" not in rules


def test_admin_session_expiry_has_idle_and_absolute_limits():
    now = 2_000_000_000
    assert not _admin_session_is_expired(now, now - 60, now - 60)
    assert _admin_session_is_expired(now, now - ADMIN_IDLE_TIMEOUT_SECONDS - 1, now - ADMIN_IDLE_TIMEOUT_SECONDS - 1)
    assert _admin_session_is_expired(now, now - ADMIN_ABSOLUTE_TIMEOUT_SECONDS - 1, now - 5)
    assert _admin_session_is_expired(now, None, now)
