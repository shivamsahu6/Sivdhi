"""Focused tests for FCM token binding and notification outbox creation."""
import sqlite3
import sys
import types

from flask import Flask

import routes.push as push_routes
import utils.push_notifications as push_delivery
from utils.push_notifications import (
    ensure_push_schema,
    _notification_route,
    _safe_push_text,
)


def _db_factory(path):
    def connect():
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        return conn
    return connect


def test_notification_insert_creates_outbox_row(tmp_path):
    path = tmp_path / "push.sqlite"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_type TEXT, user_id TEXT, title TEXT, message TEXT,
            is_read INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    ensure_push_schema(conn)
    conn.execute(
        "INSERT INTO notifications(user_type,user_id,title,message) VALUES(?,?,?,?)",
        ("customer", "21", "Booking confirmed", "Private booking details"),
    )
    row = conn.execute("SELECT * FROM push_notification_outbox").fetchone()
    assert row["notification_id"] == 1
    assert row["user_type"] == "customer"
    assert row["user_id"] == "21"
    assert row["status"] == "pending"
    conn.close()


def test_customer_token_is_bound_to_customer_session(tmp_path, monkeypatch):
    path = tmp_path / "accounts.sqlite"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_type TEXT, user_id TEXT,
            title TEXT, message TEXT, is_read INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE customers (
            id INTEGER PRIMARY KEY, status TEXT, is_deleted INTEGER
        );
        INSERT INTO customers(id,status,is_deleted) VALUES(21,'active',0);
        CREATE TABLE workers (
            worker_id TEXT PRIMARY KEY, status TEXT, is_deleted INTEGER
        );
        CREATE TABLE partners (
            partner_id TEXT PRIMARY KEY, status TEXT, is_deleted INTEGER
        );
        """
    )
    ensure_push_schema(conn)
    conn.commit()
    conn.close()
    monkeypatch.setattr(push_routes, "get_db", _db_factory(path))

    app = Flask(__name__)
    app.secret_key = "test-only-secret"
    app.register_blueprint(push_routes.push_bp)
    client = app.test_client()
    with client.session_transaction() as session:
        session["customer_id"] = 21

    response = client.post(
        "/api/push/register",
        json={"token": "F" * 160, "app_package": "com.sivdhi.customer"},
    )
    assert response.status_code == 200
    check = sqlite3.connect(path)
    row = check.execute(
        "SELECT user_type,user_id,app_package FROM push_device_tokens"
    ).fetchone()
    assert row == ("customer", "21", "com.sivdhi.customer")
    check.close()


def test_customer_cannot_register_worker_partner_package(tmp_path, monkeypatch):
    path = tmp_path / "accounts.sqlite"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_type TEXT, user_id TEXT,
            title TEXT, message TEXT, is_read INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE customers (
            id INTEGER PRIMARY KEY, status TEXT, is_deleted INTEGER
        );
        INSERT INTO customers(id,status,is_deleted) VALUES(21,'active',0);
        """
    )
    ensure_push_schema(conn)
    conn.commit()
    conn.close()
    monkeypatch.setattr(push_routes, "get_db", _db_factory(path))

    app = Flask(__name__)
    app.secret_key = "test-only-secret"
    app.register_blueprint(push_routes.push_bp)
    client = app.test_client()
    with client.session_transaction() as session:
        session["customer_id"] = 21

    response = client.post(
        "/api/push/register",
        json={"token": "F" * 160, "app_package": "com.sivdhi.workpartner"},
    )
    assert response.status_code == 403


def test_push_notification_routes_to_correct_inbox_and_item():
    assert _notification_route("customer", 42) == (
        "/customer/notifications?open_id=42#notification-42"
    )
    assert _notification_route("worker", 42) == (
        "/worker/notification-center?open_id=42#notification-42"
    )
    assert _notification_route("partner", 42) == (
        "/partner/notification?open_id=42#notification-42"
    )


def test_push_text_shows_reason_but_hides_phone_and_amount():
    amount = chr(0x20B9) + "1,250."
    text = _safe_push_text(
        "New booking assigned. Customer phone 9876543210. Advance " + amount,
        "Sivdhi update",
    )
    assert "New booking assigned" in text
    assert "[phone hidden]" in text
    assert "[amount hidden]" in text
    assert "9876543210" not in text
    assert amount not in text


def test_push_text_redacts_formatted_indian_phone_numbers():
    numbers = ("98765 43210", "+91 98765-43210", "+91-98765-43210")

    for number in numbers:
        result = _safe_push_text(
            "Booking update. Customer contact: " + number,
            "Sivdhi update",
        )
        assert "[phone hidden]" in result
        assert number not in result


def test_push_text_redacts_email_and_labelled_address():
    text = _safe_push_text(
        "Update for test@example.com. Customer address: 22 Park Street.",
        "Sivdhi update",
    )
    assert "[email hidden]" in text
    assert "[address hidden]" in text
    assert "test@example.com" not in text
    assert "22 Park Street" not in text


def test_outbox_delivery_sends_notification_content_and_safe_item_route(
    tmp_path, monkeypatch
):
    path = tmp_path / "delivery.sqlite"

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_type TEXT,
            user_id TEXT,
            title TEXT,
            message TEXT,
            is_read INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE customers (
            id INTEGER PRIMARY KEY,
            customer_id TEXT,
            status TEXT,
            is_deleted INTEGER,
            notifications_enabled INTEGER DEFAULT 1
        );

        INSERT INTO customers
            (id, customer_id, status, is_deleted, notifications_enabled)
        VALUES (21, 'C21', 'active', 0, 1);
        """
    )

    ensure_push_schema(conn)

    conn.execute(
        """
        INSERT INTO notifications(user_type, user_id, title, message)
        VALUES (?, ?, ?, ?)
        """,
        (
            "customer",
            "21",
            "Booking Confirmed",
            "Booking 123 confirmed. Phone 98765 43210. "
            "Email test@example.com. Amount INR 1250. "
            "Customer address: 22 Park Street, Aligarh, Uttar Pradesh.",
        ),
    )

    conn.execute(
        """
        INSERT INTO push_device_tokens
            (user_type, user_id, token, app_package, platform, is_active)
        VALUES (?, ?, ?, ?, 'android', 1)
        """,
        ("customer", "21", "F" * 160, "com.sivdhi.customer"),
    )

    conn.commit()
    conn.close()

    monkeypatch.setattr(push_delivery, "get_db", _db_factory(path))

    fake_app = object()
    monkeypatch.setattr(push_delivery, "_firebase_app", lambda: fake_app)

    sent_messages = []

    fake_messaging = types.ModuleType("firebase_admin.messaging")
    fake_messaging.Message = lambda **kwargs: kwargs
    fake_messaging.AndroidConfig = lambda **kwargs: kwargs
    fake_messaging.UnregisteredError = type("UnregisteredError", (Exception,), {})
    fake_messaging.send = (
        lambda message, app=None: sent_messages.append((message, app)) or "mock-id"
    )

    fake_firebase_admin = types.ModuleType("firebase_admin")
    fake_firebase_admin.messaging = fake_messaging

    monkeypatch.setitem(sys.modules, "firebase_admin", fake_firebase_admin)

    report = push_delivery.process_push_outbox_once()

    assert report["configured"] is True
    assert report["claimed"] == 1
    assert report["sent"] == 1
    assert report["errors"] == 0
    assert len(sent_messages) == 1

    message, used_app = sent_messages[0]
    assert used_app is fake_app

    data = message["data"]
    assert data["title"] == "Booking Confirmed"
    assert data["route"] == (
        "/customer/notifications?open_id=1#notification-1"
    )

    body = data["body"]
    assert "Booking 123 confirmed" in body
    assert "[phone hidden]" in body
    assert "[email hidden]" in body
    assert "[amount hidden]" in body
    assert "[address hidden]" in body

    for private_value in (
        "98765 43210",
        "test@example.com",
        "INR 1250",
        "22 Park Street",
        "Aligarh, Uttar Pradesh",
    ):
        assert private_value not in body

    check = sqlite3.connect(path)
    status = check.execute(
        "SELECT status FROM push_notification_outbox WHERE notification_id=1"
    ).fetchone()[0]
    check.close()

    assert status == "sent"
