import sqlite3

from flask import Flask


def _review_client(tmp_path, monkeypatch):
    import routes.customer as customer_routes
    import routes.decorators as customer_decorators

    database_path = tmp_path / "booking-reviews.sqlite"
    conn = sqlite3.connect(database_path)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE customers (
            id INTEGER PRIMARY KEY, fullname TEXT,
            status TEXT DEFAULT 'active', is_deleted INTEGER DEFAULT 0
        );
        INSERT INTO customers(id, fullname) VALUES (1, 'Customer One');
        INSERT INTO customers(id, fullname) VALUES (2, 'Customer Two');
        CREATE TABLE workers (id INTEGER PRIMARY KEY, worker_id TEXT, fullname TEXT);
        INSERT INTO workers VALUES (7, 'W-007', 'Private Worker Name');
        CREATE TABLE services (id INTEGER PRIMARY KEY, name TEXT);
        INSERT INTO services VALUES (3, 'AC Repair');
        CREATE TABLE bookings (
            id INTEGER PRIMARY KEY, booking_id TEXT UNIQUE, customer_id INTEGER,
            service_id INTEGER, category TEXT, worker_id INTEGER,
            booking_status TEXT, status TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO bookings VALUES
            (11,'SIV-REVIEW-11',1,3,'AC',7,'completed','completed',CURRENT_TIMESTAMP),
            (12,'SIV-REVIEW-12',1,3,'AC',7,'pending','pending',CURRENT_TIMESTAMP),
            (13,'SIV-REVIEW-13',2,3,'AC',7,'completed','completed',CURRENT_TIMESTAMP);
        CREATE TABLE reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT, booking_id INTEGER UNIQUE NOT NULL,
            customer_id INTEGER NOT NULL, worker_id INTEGER, rating INTEGER NOT NULL,
            review TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.commit()
    conn.close()

    def db_factory():
        db = sqlite3.connect(database_path)
        db.row_factory = sqlite3.Row
        return db

    monkeypatch.setattr(customer_routes, "get_db", db_factory)
    monkeypatch.setattr(customer_decorators, "get_db", db_factory)
    monkeypatch.setattr(customer_routes, "ensure_customer_schema", lambda _db: None)

    from pathlib import Path
    template_root = Path(__file__).resolve().parents[1] / "templates"
    app = Flask(__name__, template_folder=str(template_root))
    app.secret_key = "test-customer-review-secret"
    app.config["TESTING"] = True
    app.register_blueprint(customer_routes.customer_bp)
    client = app.test_client()
    with client.session_transaction() as session:
        session["customer_id"] = 1
    return client, database_path


def test_customer_can_review_own_completed_booking(tmp_path, monkeypatch):
    client, database_path = _review_client(tmp_path, monkeypatch)
    response = client.post("/customer/booking/11/review", data={
        "rating": "5", "review": "The service was completed well."
    })
    assert response.status_code == 302
    assert response.headers["Location"].endswith("#booking-11")

    conn = sqlite3.connect(database_path)
    row = conn.execute(
        "SELECT booking_id,customer_id,worker_id,rating,review FROM reviews"
    ).fetchone()
    conn.close()
    assert row == (11, 1, 7, 5, "The service was completed well.")


def test_customer_cannot_review_pending_or_another_customers_booking(tmp_path, monkeypatch):
    client, database_path = _review_client(tmp_path, monkeypatch)
    assert client.post("/customer/booking/12/review", data={"rating": "5"}).status_code == 302
    assert client.post("/customer/booking/13/review", data={"rating": "5"}).status_code == 302

    conn = sqlite3.connect(database_path)
    count = conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
    conn.close()
    assert count == 0


def test_completed_booking_review_cannot_be_overwritten(tmp_path, monkeypatch):
    client, database_path = _review_client(tmp_path, monkeypatch)
    client.post("/customer/booking/11/review", data={"rating": "4", "review": "Good work."})
    client.post("/customer/booking/11/review", data={"rating": "1", "review": "Overwrite."})

    conn = sqlite3.connect(database_path)
    rows = conn.execute(
        "SELECT rating, review FROM reviews WHERE booking_id=11"
    ).fetchall()
    conn.close()
    assert rows == [(4, "Good work.")]


def test_public_reviews_are_anonymous(tmp_path, monkeypatch):
    client, database_path = _review_client(tmp_path, monkeypatch)
    conn = sqlite3.connect(database_path)
    conn.execute(
        "INSERT INTO reviews(booking_id,customer_id,worker_id,rating,review) "
        "VALUES(11,1,7,5,'Public review text')"
    )
    conn.commit()
    conn.close()

    response = client.get("/customer/reviews")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Public review text" in body
    assert "AC Repair" in body
    assert "Customer One" not in body
    assert "Private Worker Name" not in body
    assert "SIV-REVIEW-11" not in body


def test_signup_policy_links_and_server_side_consent():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    signup = (root / "templates/customer/signup.html").read_text(encoding="utf-8")
    routes = (root / "routes/customer.py").read_text(encoding="utf-8")
    assert "url_for('customer.terms')" in signup
    assert "url_for('customer.privacy')" in signup
    assert 'id="termsDialog"' not in signup
    assert 'request.form.get("terms") != "accepted"' in routes
    assert "terms_accepted_at" in routes
    assert "privacy_policy_version" in routes


def test_shared_footer_uses_role_specific_review_and_contact_pages():
    from pathlib import Path

    footer = (
        Path(__file__).resolve().parents[1] / "templates/_sivdhi_footer.html"
    ).read_text(encoding="utf-8")

    role_links = footer.split(
        '<section class="sivdhi-footer-column" aria-label="Role links">', 1
    )[1]
    worker_section = role_links.split(
        "{% if sivdhi_footer_role|default('customer') == 'worker' %}", 1
    )[1].split(
        "{% elif sivdhi_footer_role|default('customer') == 'partner' %}", 1
    )[0]
    partner_section = role_links.split(
        "{% elif sivdhi_footer_role|default('customer') == 'partner' %}", 1
    )[1].split(
        "{% else %}", 1
    )[0]

    assert "url_for('worker.rate_app')" in worker_section
    assert "url_for('worker.help')" in worker_section
    assert "url_for('partner.rate_app')" in partner_section
    assert "url_for('partner.help')" in partner_section
    assert footer.count("url_for('customer.reviews')") == 1

def test_worker_partner_footer_uses_site_palette_and_reaches_page_bottom():
    from pathlib import Path

    footer = (
        Path(__file__).resolve().parents[1] / "templates/_sivdhi_footer.html"
    ).read_text(encoding="utf-8")

    assert 'data-role="{{ sivdhi_footer_role|default(\'customer\') }}"' in footer
    assert 'body:has(.sivdhi-site-footer[data-role="worker"])' in footer
    assert 'body:has(.sivdhi-site-footer[data-role="partner"])' in footer
    assert "min-height: 100vh;" in footer
    assert "flex-direction: column;" in footer
    assert "margin: auto auto 24px;" in footer
    assert "rgba(37,99,235,.26)" in footer
    assert "rgba(15,23,42,.99)" in footer
    assert "padding-bottom: calc(90px + env(safe-area-inset-bottom, 0px));" in footer
