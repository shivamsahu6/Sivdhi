"""Static regression checks for template integrity and common web risks."""

import ast
import re
from pathlib import Path

from jinja2 import Environment

ROOT = Path(__file__).resolve().parents[1]


def _template_files():
    return sorted((ROOT / "templates").rglob("*.html"))


def test_all_html_templates_parse_as_jinja():
    env = Environment()
    failures = []
    for path in _template_files():
        try:
            env.parse(path.read_text(encoding="utf-8"))
        except Exception as exc:  # surfaced together for a useful report
            failures.append(f"{path.relative_to(ROOT)}: {exc}")
    assert not failures, "\n".join(failures)


def test_all_state_changing_html_forms_include_csrf_token():
    missing = []
    count = 0
    form_re = re.compile(r"<form\b([^>]*)>(.*?)</form\s*>", re.I | re.S)
    method_re = re.compile(r"\bmethod\s*=\s*['\"]?([a-z]+)", re.I)
    csrf_re = re.compile(r"csrf_token|csrf-token|X-CSRFToken|X-CSRF-Token", re.I)
    for path in _template_files():
        source = path.read_text(encoding="utf-8", errors="replace")
        for match in form_re.finditer(source):
            method_match = method_re.search(match.group(1))
            method = method_match.group(1).lower() if method_match else "get"
            if method in {"post", "put", "patch", "delete"}:
                count += 1
                if not csrf_re.search(match.group(2)):
                    missing.append(f"{path.relative_to(ROOT)} form near offset {match.start()}")
    assert count > 0, "No state-changing forms detected; inspect the scanner."
    assert not missing, "Forms missing CSRF protection:\n" + "\n".join(missing)


def test_customer_location_is_explicit_and_changeable():
    source = (ROOT / "templates/customer/home.html").read_text(encoding="utf-8")
    assert 'id="allowLocationButton"' in source
    assert 'id="changeLocationButton"' in source
    assert 'changeLocationButton.addEventListener("click", requestLocation)' in source
    assert "maximumAge: 0" in source
    initialization = source.split("updateModeButtons();", 1)[1].split("</script>", 1)[0]
    assert "requestLocation();" not in initialization


def test_worker_customer_phone_is_available_only_through_call_action():
    source = (ROOT / "templates/worker/job_details.html").read_text(encoding="utf-8")
    assert "href=\"tel:{{ job['customer_mobile']" in source
    assert ">Call customer</a>" in source
    assert '<span class="value">{{ job["customer_mobile"] }}</span>' not in source


def test_admin_search_uses_text_nodes_not_untrusted_inner_html():
    source = (ROOT / "templates/admin/dashboard.html").read_text(encoding="utf-8")
    assert "strong.textContent=String(item.name||'')" in source
    assert "small.textContent=String(item.type||'')" in source
    assert "innerHTML='<strong>'+item.name" not in source


def test_literal_url_for_endpoints_resolve_to_declared_routes():
    declared = {"static"}
    python_files = [ROOT / "app.py", *sorted((ROOT / "routes").glob("*.py"))]
    for path in python_files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        blueprint_names = {}
        for node in tree.body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            value = node.value
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and value.func.id == "Blueprint"
                and value.args
                and isinstance(value.args[0], ast.Constant)
            ):
                for target in targets:
                    if isinstance(target, ast.Name):
                        blueprint_names[target.id] = str(value.args[0].value)
        # Recovered auth handlers live in the supplied CPython 3.14 bytecode,
        # so the small source loader declares their route function names here.
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            if not any(isinstance(target, ast.Name) and target.id == "__auth_binary_endpoints__" for target in node.targets):
                continue
            if isinstance(node.value, (ast.Tuple, ast.List)):
                for item in node.value.elts:
                    if isinstance(item, ast.Constant) and isinstance(item.value, str):
                        declared.add("auth." + item.value)

        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
                    continue
                route_owner = decorator.func.value.id if isinstance(decorator.func.value, ast.Name) else ""
                if decorator.func.attr not in {"route", "get", "post", "put", "delete", "patch"}:
                    continue
                if route_owner in blueprint_names:
                    endpoint = node.name
                    for keyword in decorator.keywords:
                        if keyword.arg == "endpoint" and isinstance(keyword.value, ast.Constant):
                            endpoint = str(keyword.value.value)
                    declared.add(blueprint_names[route_owner] + "." + endpoint)
                elif route_owner == "app":
                    declared.add(node.name)

    refs = []
    source_files = [*python_files, *_template_files()]
    for path in source_files:
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in re.finditer(r"url_for\(\s*['\"]([^'\"]+)['\"]", text):
            refs.append((path.relative_to(ROOT), match.group(1)))
    missing = sorted({f"{path}: {endpoint}" for path, endpoint in refs if endpoint not in declared and not endpoint.startswith("static.")})
    assert not missing, "Unresolved literal url_for endpoints:\n" + "\n".join(missing)


def test_legacy_brand_name_is_absent_from_text_files():
    legacy_name = re.compile("work" + r"\s*" + "mitra", re.I)
    ignored_dirs = {"__pycache__", ".pytest_cache", ".git", ".venv", "venv", "node_modules"}
    matches = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or any(part in ignored_dirs for part in path.parts):
            continue
        if path.name in {".secret_key", ".env"} or path.suffix.lower() in {".db", ".sqlite", ".sqlite3", ".pyc"}:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for line_number, line in enumerate(content.splitlines(), 1):
            if legacy_name.search(line):
                matches.append(f"{path.relative_to(ROOT)}:{line_number}")
    assert not matches, "Old product branding remains:\n" + "\n".join(matches)



def test_shared_footer_is_added_to_pages_that_do_not_include_it():
    from app import app, _inject_sivdhi_site_footer

    with app.test_request_context("/customer/service-areas"):
        response = app.response_class(
            "<html><head></head><body><main>Service areas</main></body></html>",
            mimetype="text/html",
        )
        updated = _inject_sivdhi_site_footer(response)
        html = updated.get_data(as_text=True)

    assert html.count('class="sivdhi-site-footer"') == 1
    assert 'href="/customer/faq"' in html
    assert 'href="/customer/service-areas"' not in html
    assert 'href="/partner-opportunity"' not in html
    assert "Service Areas" not in html
    assert "Partner Opportunity" not in html
    assert 'id="sivdhi-global-footer-layout"' in html
    assert "margin-top: auto !important" in html


def test_shared_footer_is_not_duplicated_and_worker_role_is_preserved():
    from app import app, _inject_sivdhi_site_footer

    existing = (
        '<html><head></head><body><main>Worker</main>'
        '<footer class="sivdhi-site-footer">Existing footer</footer></body></html>'
    )
    with app.test_request_context("/worker/home"):
        response = app.response_class(existing, mimetype="text/html")
        updated = _inject_sivdhi_site_footer(response)
        html = updated.get_data(as_text=True)

    assert html.count('class="sivdhi-site-footer"') == 1
    assert html.count("Existing footer") == 1
    assert 'id="sivdhi-global-footer-layout"' in html

    with app.test_request_context("/worker/forgot-password"):
        response = app.response_class(
            "<html><head></head><body><main>Forgot password</main></body></html>",
            mimetype="text/html",
        )
        updated = _inject_sivdhi_site_footer(response)
        worker_html = updated.get_data(as_text=True)

    assert 'href="/worker/faq"' in worker_html
    assert worker_html.count('class="sivdhi-site-footer"') == 1


def test_shared_footer_is_not_injected_into_admin_or_non_html_responses():
    from app import app, _inject_sivdhi_site_footer
    from routes.admin import ADMIN_PANEL_BASE

    with app.test_request_context(ADMIN_PANEL_BASE + "/dashboard"):
        response = app.response_class(
            "<html><head></head><body>Admin</body></html>",
            mimetype="text/html",
        )
        updated = _inject_sivdhi_site_footer(response)
        assert "sivdhi-site-footer" not in updated.get_data(as_text=True)

    with app.test_request_context("/customer/api/example"):
        response = app.response_class('{"ok":true}', mimetype="application/json")
        updated = _inject_sivdhi_site_footer(response)
        assert updated.get_data(as_text=True) == '{"ok":true}'


def test_flask_minimum_version_excludes_known_affected_release():
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert re.search(r"(?m)^Flask>=3\.1\.3,<4\.0$", requirements)


def test_admin_search_query_is_bounded():
    source = (ROOT / "routes/admin.py").read_text(encoding="utf-8")
    assert 'request.args.get("q", "") or "").strip()[:100]' in source

def test_site_favicon_serves_existing_brand_jpeg_with_correct_mime_type():
    from app import app

    favicon_asset = ROOT / "static" / "pwa" / "customer" / "icon-512.jpg"
    assert favicon_asset.is_file()

    with app.test_client() as client:
        response = client.get("/favicon.ico")

    assert response.status_code == 200
    assert response.mimetype == "image/jpeg"
    assert response.data.startswith(b"\xff\xd8\xff")
    assert response.data == favicon_asset.read_bytes()
    assert response.headers.get("Cache-Control") == "public, max-age=86400"


def test_site_favicon_injection_uses_jpeg_mime_and_new_cache_buster():
    from app import add_security_headers, app

    source = (
        '<html><head><link rel="icon" type="image/png" href="/old-favicon.png">'
        '</head><body>Page</body></html>'
    )
    with app.test_request_context("/customer/home"):
        response = app.response_class(source, mimetype="text/html")
        updated = add_security_headers(response)
        html = updated.get_data(as_text=True)

    assert html.count('rel="icon"') == 1
    assert 'type="image/jpeg"' in html
    assert 'sizes="512x512"' in html
    assert 'href="/favicon.ico?v=20261128-1"' in html
    assert "/old-favicon.png" not in html

