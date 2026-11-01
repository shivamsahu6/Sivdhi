"""Focused tests for FCM token binding and notification outbox creation."""
import sqlite3

from flask import Flask

import routes.push as push_routes
from utils.push_notifications import ensure_push_schema


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
