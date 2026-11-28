from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_worker_partner_auth_footers_stay_at_bottom_with_a_clear_form_gap():
    for path, box_class in (
        ("templates/login.html", ".login-box"),
        ("templates/signup.html", ".signup-box"),
    ):
        template = (ROOT / path).read_text(encoding="utf-8")
        assert "row-gap: clamp(32px, 6vh, 56px);" in template
        assert "padding: 20px 20px 0;" in template
        assert "padding-bottom: 0;" in template
        assert "margin: auto auto 0;" in template
        assert box_class in template

    from app import app

    client = app.test_client()
    for role in ("worker", "partner"):
        for route, box_class in (
            (f"/login?role={role}", "login-box"),
            (f"/signup?role={role}", "signup-box"),
        ):
            response = client.get(route)
            assert response.status_code == 200
            page = response.get_data(as_text=True)
            assert f'data-role="{role}"' in page
            assert page.index(f'class="{box_class}"') < page.index(
                f'<footer class="sivdhi-site-footer" data-role="{role}"'
            )
