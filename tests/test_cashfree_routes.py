import sqlite3

from flask import Flask


def _cashfree_callback_client(
    tmp_path, monkeypatch, *, order_owner=1, order_status="PENDING"
):
    import routes.customer as customer_routes
    import routes.decorators as customer_decorators
    from utils.sivdhi_finance import ensure_finance_schema

    database_path = tmp_path / "cashfree-callback.sqlite"

    conn = sqlite3.connect(database_path)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE customers (
            id INTEGER PRIMARY KEY,
            fullname TEXT,
            status TEXT DEFAULT 'active',
            is_deleted INTEGER DEFAULT 0
        );

        INSERT INTO customers(id, fullname, status, is_deleted)
        VALUES (1, 'Test Customer', 'active', 0);

        CREATE TABLE bookings (
            id INTEGER PRIMARY KEY,
            booking_id TEXT
        );
    """)

    ensure_finance_schema(conn)

    conn.execute("""
        INSERT INTO cashfree_payment_orders
            (order_id, customer_id, amount, currency, status, payment_session_id)
        VALUES (?, ?, 50, 'INR', ?, ?)
    """, (
        "sivdhi_1_callbacktest",
        order_owner,
        order_status,
        "session_test_only",
    ))
    conn.commit()
    conn.close()

    def db_factory():
        db = sqlite3.connect(database_path)
        db.row_factory = sqlite3.Row
        return db

    # Both the route and its authentication decorator use get_db.
    monkeypatch.setattr(customer_routes, "get_db", db_factory)
    monkeypatch.setattr(customer_decorators, "get_db", db_factory)

    app = Flask(__name__)
    app.secret_key = "isolated-cashfree-route-test-secret"
    app.config["TESTING"] = True
    app.register_blueprint(customer_routes.customer_bp)

    client = app.test_client()
    with client.session_transaction() as session:
        session["customer_id"] = 1

    return client, database_path, customer_routes


def test_cashfree_return_marks_own_verified_order_success(
    tmp_path, monkeypatch
):
    client, path, routes = _cashfree_callback_client(tmp_path, monkeypatch)

    monkeypatch.setattr(
        routes,
        "verify_cashfree_order_payment",
        lambda order_id, amount: {
            "cf_payment_id": "987654321",
            "payment_group": "upi",
            "payment_amount": "50.00",
        },
    )

    response = client.get(
        "/customer/cashfree/return?order_id=sivdhi_1_callbacktest"
    )

    assert response.status_code == 302
    assert "cashfree_order_id=" in response.location

    conn = sqlite3.connect(path)
    row = conn.execute("""
        SELECT status, cf_payment_id, payment_method
        FROM cashfree_payment_orders
        WHERE order_id=?
    """, ("sivdhi_1_callbacktest",)).fetchone()
    conn.close()

    assert row == ("SUCCESS", "987654321", "upi")


def test_cashfree_return_keeps_order_pending_when_payment_is_not_verified(
    tmp_path, monkeypatch
):
    client, path, routes = _cashfree_callback_client(tmp_path, monkeypatch)

    monkeypatch.setattr(
        routes, "verify_cashfree_order_payment", lambda *args: None
    )

    response = client.get(
        "/customer/cashfree/return?order_id=sivdhi_1_callbacktest"
    )

    assert response.status_code == 302

    conn = sqlite3.connect(path)
    status = conn.execute(
        "SELECT status FROM cashfree_payment_orders"
    ).fetchone()[0]
    conn.close()

    assert status == "PENDING"


def test_cashfree_return_rejects_another_customers_order(
    tmp_path, monkeypatch
):
    client, path, routes = _cashfree_callback_client(
        tmp_path, monkeypatch, order_owner=2
    )

    calls = []

    def unexpected_verification(*args):
        calls.append(args)
        return {
            "cf_payment_id": "wrong-owner",
            "payment_group": "upi",
            "payment_amount": "50.00",
        }

    monkeypatch.setattr(
        routes, "verify_cashfree_order_payment", unexpected_verification
    )

    response = client.get(
        "/customer/cashfree/return?order_id=sivdhi_1_callbacktest"
    )

    assert response.status_code == 302
    assert calls == []

    conn = sqlite3.connect(path)
    status = conn.execute(
        "SELECT status FROM cashfree_payment_orders"
    ).fetchone()[0]
    conn.close()

    assert status == "PENDING"


def test_cashfree_return_does_not_reverify_booked_order(
    tmp_path, monkeypatch
):
    client, _path, routes = _cashfree_callback_client(
        tmp_path, monkeypatch, order_status="BOOKED"
    )

    calls = []

    def unexpected_verification(*args):
        calls.append(args)
        return None

    monkeypatch.setattr(
        routes, "verify_cashfree_order_payment", unexpected_verification
    )

    response = client.get(
        "/customer/cashfree/return?order_id=sivdhi_1_callbacktest"
    )

    assert response.status_code == 302
    assert "bookings" in response.location
    assert calls == []
