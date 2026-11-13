import sqlite3
import time

from flask import Flask


def _admin_refund_client(tmp_path, monkeypatch, *, order_status="PAID_NO_WORKER", environment="production"):
    import routes.admin as admin_routes
    import routes.decorators as admin_decorators
    import utils.admin_mfa as admin_mfa
    from utils.sivdhi_finance import ensure_finance_schema

    database_path = tmp_path / "cashfree-manual-refunds.sqlite"
    conn = sqlite3.connect(database_path)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            booking_id TEXT UNIQUE,
            customer_id INTEGER,
            status TEXT DEFAULT 'pending',
            booking_status TEXT DEFAULT 'pending'
        );
        CREATE TABLE customers (
            id INTEGER PRIMARY KEY,
            fullname TEXT,
            email TEXT
        );
        INSERT INTO customers(id, fullname, email)
        VALUES (1, 'Refund Test Customer', 'refund@example.test');
        CREATE TABLE notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_type TEXT,
            user_id TEXT,
            title TEXT,
            message TEXT,
            is_read INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
    """)
    ensure_finance_schema(conn)
    conn.execute("""
        INSERT INTO cashfree_payment_orders
            (order_id, customer_id, amount, currency, status,
             payment_session_id, environment, cf_payment_id)
        VALUES ('sivdhi_1_refundtest', 1, 50, 'INR', ?, 'session_test', ?, 'cf-payment-123')
    """, (order_status, environment))
    conn.commit()
    conn.close()

    def db_factory():
        db = sqlite3.connect(database_path)
        db.row_factory = sqlite3.Row
        return db

    monkeypatch.setattr(admin_routes, "get_db", db_factory)
    monkeypatch.setattr(admin_routes, "is_admin_authorized", lambda _admin_id: True)
    monkeypatch.setattr(admin_mfa, "is_admin_authorized", lambda _admin_id: True)
    monkeypatch.setattr(admin_decorators, "get_db", db_factory)

    app = Flask(__name__)
    app.secret_key = "isolated-admin-refund-test-secret"
    app.config["TESTING"] = True
    app.register_blueprint(admin_routes.admin_bp)
    client = app.test_client()
    now = int(time.time())
    with client.session_transaction() as session:
        session["role"] = "admin"
        session["admin_id"] = "ADMIN_TEST"
        session["admin_login_at"] = now
        session["admin_last_seen_at"] = now
    return client, database_path, admin_routes


def test_cashfree_manual_refund_is_recorded_then_reconciled_without_api_call(
    tmp_path, monkeypatch
):
    client, path, routes = _admin_refund_client(tmp_path, monkeypatch)
    base = routes.ADMIN_PANEL_BASE + "/admin/cashfree-refunds"

    response = client.post(
        base + "/sivdhi_1_refundtest/record",
        data={
            "refund_id": "refund-test-123",
            "environment": "production",
            "reason": "Worker unavailable",
        },
    )
    assert response.status_code == 302

    conn = sqlite3.connect(path)
    attempt = conn.execute("""
        SELECT order_id, refund_id, amount, status, admin_id
        FROM cashfree_manual_refunds
    """).fetchone()
    conn.close()
    assert attempt == (
        "sivdhi_1_refundtest", "refund-test-123", 50.0,
        "INITIATED", "ADMIN_TEST",
    )

    duplicate = client.post(
        base + "/sivdhi_1_refundtest/record",
        data={
            "refund_id": "refund-test-duplicate",
            "environment": "production",
        },
    )
    assert duplicate.status_code == 302

    conn = sqlite3.connect(path)
    count = conn.execute(
        "SELECT COUNT(*) FROM cashfree_manual_refunds"
    ).fetchone()[0]
    conn.close()
    assert count == 1

    success = client.post(
        base + "/1/status",
        data={"status": "SUCCESS"},
    )
    assert success.status_code == 302

    conn = sqlite3.connect(path)
    status = conn.execute(
        "SELECT status FROM cashfree_manual_refunds WHERE id=1"
    ).fetchone()[0]
    notification = conn.execute("""
        SELECT user_type, user_id, title, message
        FROM notifications
    """).fetchone()
    order_status = conn.execute(
        "SELECT status FROM cashfree_payment_orders"
    ).fetchone()[0]
    conn.close()

    assert status == "SUCCESS"
    assert notification[0:3] == (
        "customer", "1", "Cashfree Refund Successful"
    )
    assert "refund-test-123" in notification[3]
    assert order_status == "PAID_NO_WORKER"


def test_cashfree_manual_refund_requires_environment_for_legacy_unknown_order(
    tmp_path, monkeypatch
):
    client, path, routes = _admin_refund_client(
        tmp_path, monkeypatch, environment="unknown"
    )
    base = routes.ADMIN_PANEL_BASE + "/admin/cashfree-refunds"

    missing = client.post(
        base + "/sivdhi_1_refundtest/record",
        data={"refund_id": "refund-unknown-1"},
    )
    assert missing.status_code == 302

    conn = sqlite3.connect(path)
    count = conn.execute(
        "SELECT COUNT(*) FROM cashfree_manual_refunds"
    ).fetchone()[0]
    environment = conn.execute(
        "SELECT environment FROM cashfree_payment_orders"
    ).fetchone()[0]
    conn.close()
    assert count == 0
    assert environment == "unknown"

    confirmed = client.post(
        base + "/sivdhi_1_refundtest/record",
        data={
            "refund_id": "refund-unknown-1",
            "environment": "sandbox",
        },
    )
    assert confirmed.status_code == 302

    conn = sqlite3.connect(path)
    row = conn.execute("""
        SELECT environment FROM cashfree_payment_orders
    """).fetchone()
    conn.close()
    assert row[0] == "sandbox"


def test_cashfree_manual_refund_page_requires_admin_and_lists_paid_no_worker(
    tmp_path, monkeypatch
):
    client, _path, routes = _admin_refund_client(tmp_path, monkeypatch)
    response = client.get(routes.ADMIN_PANEL_BASE + "/admin/cashfree-refunds")
    assert response.status_code == 200
    assert b"sivdhi_1_refundtest" in response.data
    assert b"Cashfree Payment ID" in response.data
    assert b"does not transfer money" in response.data
