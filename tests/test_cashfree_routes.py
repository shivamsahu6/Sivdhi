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
            (order_id, customer_id, amount, currency, status, payment_session_id, environment)
        VALUES (?, ?, 50, 'INR', ?, ?, 'sandbox')
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
    verification_calls = []

    def verify_order(order_id, amount, environment=None):
        verification_calls.append((order_id, amount, environment))
        return {
            "cf_payment_id": "987654321",
            "payment_group": "upi",
            "payment_amount": "50.00",
        }

    monkeypatch.setattr(routes, "verify_cashfree_order_payment", verify_order)

    response = client.get(
        "/customer/cashfree/return?order_id=sivdhi_1_callbacktest"
    )

    assert response.status_code == 302
    assert "cashfree_order_id=" in response.location
    assert verification_calls == [("sivdhi_1_callbacktest", 50.0, "sandbox")]

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
        routes, "verify_cashfree_order_payment", lambda *args, **kwargs: None
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


def _cashfree_payment_client(tmp_path, monkeypatch):
    import routes.customer as customer_routes
    import routes.decorators as customer_decorators
    import utils.assignment_monitor as assignment_monitor
    from routes.customer import ensure_customer_schema

    database_path = tmp_path / "cashfree-payment-flow.sqlite"
    conn = sqlite3.connect(database_path)
    conn.row_factory = sqlite3.Row

    conn.executescript("""
        CREATE TABLE customers (
            id INTEGER PRIMARY KEY,
            fullname TEXT,
            mobile TEXT,
            email TEXT,
            latitude REAL,
            longitude REAL,
            status TEXT DEFAULT 'active',
            is_deleted INTEGER DEFAULT 0
        );

        INSERT INTO customers
            (id, fullname, mobile, email, latitude, longitude)
        VALUES
            (1, 'Test Customer', '9876543210', 'test@example.test', 28.36, 79.43);

        CREATE TABLE services (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            category TEXT NOT NULL,
            description TEXT DEFAULT '',
            price REAL DEFAULT 0,
            full_day_price REAL DEFAULT 0,
            icon TEXT DEFAULT '✦',
            active INTEGER DEFAULT 1
        );

        INSERT INTO services(id, name, category, price)
        VALUES (1, 'Electrician', 'Electrician', 100);

        CREATE TABLE bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            booking_id TEXT UNIQUE NOT NULL,
            customer_id INTEGER NOT NULL,
            service_id INTEGER,
            category TEXT NOT NULL,
            description TEXT,
            address TEXT NOT NULL,
            latitude REAL,
            longitude REAL,
            preferred_date TEXT,
            preferred_time TEXT,
            service_charge REAL DEFAULT 0,
            platform_fee REAL DEFAULT 0,
            total_amount REAL DEFAULT 0,
            payment_method TEXT,
            payment_id TEXT,
            payment_status TEXT DEFAULT 'pending',
            worker_id INTEGER,
            booking_status TEXT DEFAULT 'pending',
            status TEXT DEFAULT 'pending',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_type TEXT,
            user_id TEXT,
            title TEXT,
            message TEXT,
            is_read INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE admin_settings (
            setting_key TEXT PRIMARY KEY,
            setting_value TEXT
        );
    """)

    ensure_customer_schema(conn)
    conn.commit()
    conn.close()

    def db_factory():
        db = sqlite3.connect(database_path)
        db.row_factory = sqlite3.Row
        return db

    monkeypatch.setattr(customer_routes, "get_db", db_factory)
    monkeypatch.setattr(customer_decorators, "get_db", db_factory)

    monkeypatch.setattr(
        customer_routes,
        "validate_customer_booking_slot",
        lambda *args: ("2026-11-13", "10:00", None),
    )

    candidate = {
        "worker_db_id": 1,
        "worker_id": "W-TEST-1",
        "worker_name": "Test Worker",
        "distance_km": 1.0,
    }
    monkeypatch.setattr(
        customer_routes, "find_nearest_worker", lambda *args, **kwargs: candidate
    )

    # Keep this test focused on payment/booking transaction, not scheduler behavior.
    monkeypatch.setattr(
        assignment_monitor, "_discover_live_worker_offers", lambda *args: 0
    )

    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("SIVDHI_PAYMENT_MODE", "cashfree")
    monkeypatch.setenv("CASHFREE_ENV", "sandbox")
    monkeypatch.setenv("CASHFREE_APP_ID", "isolated-test-app")
    monkeypatch.setenv("CASHFREE_SECRET_KEY", "isolated-test-secret")
    monkeypatch.setenv("SIVDHI_PUBLIC_BASE_URL", "https://example.test")

    app = Flask(__name__)
    app.secret_key = "isolated-payment-flow-test-secret"
    app.config["TESTING"] = True
    app.register_blueprint(customer_routes.customer_bp)

    client = app.test_client()
    with client.session_transaction() as session:
        session["customer_id"] = 1
        session["booking_mode"] = "normal"
        session["selected_service_id"] = 1
        session["selected_category"] = "Electrician"
        session["booking_date"] = "2026-11-13"
        session["booking_time"] = "10:00"
        session["customer_latitude"] = 28.36
        session["customer_longitude"] = 79.43

    return client, database_path, customer_routes, candidate


def _create_test_cashfree_order(client, routes, monkeypatch):
    monkeypatch.setattr(
        routes,
        "create_cashfree_order",
        lambda order_id, amount, customer, return_url: {
            "order_id": order_id,
            "payment_session_id": "isolated-session-test",
            "environment": "sandbox",
        },
    )

    response = client.post(
        "/customer/payment",
        data={"payment_action": "create_cashfree_order"},
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    return payload["order_id"]


def test_cashfree_order_creation_reuses_same_pending_checkout(
    tmp_path, monkeypatch
):
    client, path, routes, _candidate = _cashfree_payment_client(
        tmp_path, monkeypatch
    )

    create_calls = []

    def create_order(order_id, amount, customer, return_url):
        create_calls.append(order_id)
        return {
            "order_id": order_id,
            "payment_session_id": "isolated-session-test",
            "environment": "sandbox",
        }

    monkeypatch.setattr(routes, "create_cashfree_order", create_order)

    first = client.post(
        "/customer/payment",
        data={"payment_action": "create_cashfree_order"},
    )
    second = client.post(
        "/customer/payment",
        data={"payment_action": "create_cashfree_order"},
    )

    assert first.status_code == 200
    assert second.status_code == 200
    first_payload = first.get_json()
    second_payload = second.get_json()
    assert first_payload["order_id"] == second_payload["order_id"]
    assert first_payload["payment_session_id"] == second_payload["payment_session_id"]
    assert len(create_calls) == 1

    conn = sqlite3.connect(path)
    order_count = conn.execute(
        "SELECT COUNT(*) FROM cashfree_payment_orders"
    ).fetchone()[0]
    conn.close()
    assert order_count == 1


def test_cashfree_order_creation_persists_pending_order_only(
    tmp_path, monkeypatch
):
    client, path, routes, _candidate = _cashfree_payment_client(
        tmp_path, monkeypatch
    )

    order_id = _create_test_cashfree_order(client, routes, monkeypatch)

    conn = sqlite3.connect(path)
    order = conn.execute("""
        SELECT customer_id, amount, currency, status, payment_session_id, context_hash, environment
        FROM cashfree_payment_orders WHERE order_id=?
    """, (order_id,)).fetchone()
    booking_count = conn.execute(
        "SELECT COUNT(*) FROM bookings"
    ).fetchone()[0]
    conn.close()

    assert order is not None
    assert order[0:4] == (1, 50.0, "INR", "PENDING")
    assert order[4] == "isolated-session-test"
    assert len(order[5]) == 64
    assert order[6] == "sandbox"
    assert booking_count == 0


def test_verified_cashfree_sandbox_order_creates_booking_and_ledger(
    tmp_path, monkeypatch
):
    client, path, routes, _candidate = _cashfree_payment_client(
        tmp_path, monkeypatch
    )

    order_id = _create_test_cashfree_order(client, routes, monkeypatch)

    conn = sqlite3.connect(path)
    conn.execute("""
        UPDATE cashfree_payment_orders
        SET status='SUCCESS', cf_payment_id='cf-test-payment-1',
            payment_method='upi'
        WHERE order_id=?
    """, (order_id,))
    conn.commit()
    conn.close()

    response = client.post(
        "/customer/payment",
        data={
            "payment_action": "confirm_cashfree",
            "payment_method": "cashfree",
            "cashfree_order_id": order_id,
        },
    )

    assert response.status_code == 302

    conn = sqlite3.connect(path)
    booking = conn.execute("""
        SELECT booking_id, total_amount, advance_paid_amount,
               balance_due_amount, payment_method, payment_id,
               payment_status, advance_payment_status
        FROM bookings
    """).fetchone()

    payment = conn.execute("""
        SELECT method, status, payment_type, is_demo
        FROM customer_payment_records
    """).fetchone()

    ledger = conn.execute("""
        SELECT entry_type, amount, status, payment_mode, reference_id
        FROM sivdhi_finance_ledger
    """).fetchone()

    order = conn.execute("""
        SELECT status, booking_id FROM cashfree_payment_orders
        WHERE order_id=?
    """, (order_id,)).fetchone()

    conn.close()

    assert booking is not None
    assert booking[1:6] == (
        100.0, 50.0, 50.0, "cashfree", "cf-test-payment-1"
    )
    assert booking[6:] == ("PARTIALLY_PAID_DEMO", "DEMO_PAID")
    assert payment == ("cashfree", "DEMO_PAID", "advance", 1)
    assert ledger == (
        "customer_advance", 50.0, "demo_paid",
        "cashfree_sandbox", "cf-test-payment-1"
    )
    assert order == ("BOOKED", booking[0])


def test_paid_cashfree_order_without_worker_is_not_booked_or_charged_again(
    tmp_path, monkeypatch
):
    client, path, routes, candidate = _cashfree_payment_client(
        tmp_path, monkeypatch
    )

    order_id = _create_test_cashfree_order(client, routes, monkeypatch)

    conn = sqlite3.connect(path)
    conn.execute("""
        UPDATE cashfree_payment_orders
        SET status='SUCCESS', cf_payment_id='cf-test-payment-2'
        WHERE order_id=?
    """, (order_id,))
    conn.commit()
    conn.close()

    # Worker was eligible at checkout but became unavailable at confirmation.
    monkeypatch.setattr(
        routes, "find_nearest_worker", lambda *args, **kwargs: None
    )

    response = client.post(
        "/customer/payment",
        data={
            "payment_action": "confirm_cashfree",
            "payment_method": "cashfree",
            "cashfree_order_id": order_id,
        },
    )

    assert response.status_code == 302

    conn = sqlite3.connect(path)
    order = conn.execute("""
        SELECT status, booking_id FROM cashfree_payment_orders
        WHERE order_id=?
    """, (order_id,)).fetchone()
    booking_count = conn.execute(
        "SELECT COUNT(*) FROM bookings"
    ).fetchone()[0]
    payment_count = conn.execute(
        "SELECT COUNT(*) FROM customer_payment_records"
    ).fetchone()[0]
    ledger_count = conn.execute(
        "SELECT COUNT(*) FROM sivdhi_finance_ledger"
    ).fetchone()[0]
    conn.close()

    assert order == ("PAID_NO_WORKER", None)
    assert booking_count == 0
    assert payment_count == 0
    assert ledger_count == 0



def test_pending_checkout_from_other_cashfree_environment_is_not_reused(
    tmp_path, monkeypatch
):
    client, path, routes, _candidate = _cashfree_payment_client(
        tmp_path, monkeypatch
    )
    first_order_id = _create_test_cashfree_order(client, routes, monkeypatch)

    conn = sqlite3.connect(path)
    conn.execute(
        "UPDATE cashfree_payment_orders SET environment='production' WHERE order_id=?",
        (first_order_id,),
    )
    conn.commit()
    conn.close()

    second = client.post(
        "/customer/payment",
        data={"payment_action": "create_cashfree_order"},
    )
    assert second.status_code == 200
    second_payload = second.get_json()
    assert second_payload["order_id"] != first_order_id
    assert second_payload["environment"] == "sandbox"

    conn = sqlite3.connect(path)
    rows = conn.execute(
        "SELECT order_id, environment FROM cashfree_payment_orders ORDER BY rowid"
    ).fetchall()
    conn.close()
    assert rows == [
        (first_order_id, "production"),
        (second_payload["order_id"], "sandbox"),
    ]

def test_cashfree_return_does_not_reverify_refund_locked_orders(
    tmp_path, monkeypatch
):
    import routes.customer as customer_routes

    for status in ("REFUND_PENDING", "REFUNDED"):
        client, path, routes = _cashfree_callback_client(
            tmp_path / status.lower(), monkeypatch, order_status=status
        )
        calls = []

        def unexpected_verification(*args, **kwargs):
            calls.append((args, kwargs))
            return {
                "cf_payment_id": "must-not-be-used",
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
        assert "payment" in response.location
        assert calls == []

        conn = sqlite3.connect(path)
        stored_status = conn.execute(
            "SELECT status FROM cashfree_payment_orders"
        ).fetchone()[0]
        conn.close()
        assert stored_status == status

