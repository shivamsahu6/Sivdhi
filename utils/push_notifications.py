"""FCM device registration and transactional notification delivery for Sivdhi.

The notifications table remains the source of truth. An SQLite trigger copies
new notification rows to an outbox in the same transaction. The assignment
monitor drains that outbox and sends generic, privacy-safe FCM data messages.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any

from database import get_db

LOGGER = logging.getLogger(__name__)
_FIREBASE_APP = None
_FIREBASE_UNAVAILABLE_LOGGED = False
_FIREBASE_INIT_LOCK = __import__("threading").Lock()


def ensure_push_schema(conn) -> None:
    """Create token/outbox tables and the notification-to-outbox trigger."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS push_device_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_type TEXT NOT NULL,
            user_id TEXT NOT NULL,
            token TEXT NOT NULL UNIQUE,
            app_package TEXT NOT NULL,
            platform TEXT NOT NULL DEFAULT 'android',
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_push_tokens_owner
        ON push_device_tokens(user_type, user_id, is_active)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS push_notification_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            notification_id INTEGER NOT NULL UNIQUE,
            user_type TEXT NOT NULL,
            user_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            locked_at TIMESTAMP,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            sent_at TIMESTAMP,
            last_error TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_push_outbox_ready
        ON push_notification_outbox(status, next_attempt_at, id)
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_notifications_push_outbox
        AFTER INSERT ON notifications
        BEGIN
            INSERT OR IGNORE INTO push_notification_outbox
                (notification_id, user_type, user_id, status, attempts,
                 next_attempt_at, created_at)
            VALUES
                (NEW.id, LOWER(COALESCE(NEW.user_type, '')),
                 CAST(COALESCE(NEW.user_id, '') AS TEXT),
                 'pending', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);
        END
        """
    )




def ensure_customer_push_columns(conn) -> None:
    """Add only the customer fields needed for safe push registration/delivery."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(customers)").fetchall()}
    if not columns:
        raise RuntimeError("Customer table is not available for push registration")
    for name, definition in (
        ("status", "TEXT NOT NULL DEFAULT 'active'"),
        ("is_deleted", "INTEGER NOT NULL DEFAULT 0"),
        ("notifications_enabled", "INTEGER DEFAULT 1"),
    ):
        if name not in columns:
            conn.execute(f'ALTER TABLE customers ADD COLUMN "{name}" {definition}')

def _firebase_app():
    """Initialize Firebase Admin lazily from a server-only credential source."""
    global _FIREBASE_APP, _FIREBASE_UNAVAILABLE_LOGGED
    if _FIREBASE_APP is not None:
        return _FIREBASE_APP

    with _FIREBASE_INIT_LOCK:
        if _FIREBASE_APP is not None:
            return _FIREBASE_APP

        try:
            import firebase_admin
            from firebase_admin import credentials
        except ImportError:
            if not _FIREBASE_UNAVAILABLE_LOGGED:
                LOGGER.error("firebase-admin is not installed; FCM delivery is disabled")
                _FIREBASE_UNAVAILABLE_LOGGED = True
            return None

        try:
            try:
                _FIREBASE_APP = firebase_admin.get_app()
                return _FIREBASE_APP
            except ValueError:
                pass

            raw_json = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON", "").strip()
            credential_path = (
                os.getenv("FIREBASE_SERVICE_ACCOUNT_FILE", "").strip()
                or os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
            )
            if raw_json:
                credential_data = json.loads(raw_json)
                credential = credentials.Certificate(credential_data)
            elif credential_path:
                path = Path(credential_path).expanduser()
                if not path.is_file():
                    if not _FIREBASE_UNAVAILABLE_LOGGED:
                        LOGGER.error("Firebase service-account file is not present")
                        _FIREBASE_UNAVAILABLE_LOGGED = True
                    return None
                credential = credentials.Certificate(str(path))
            else:
                if not _FIREBASE_UNAVAILABLE_LOGGED:
                    LOGGER.warning(
                        "FCM is waiting for FIREBASE_SERVICE_ACCOUNT_FILE or "
                        "FIREBASE_SERVICE_ACCOUNT_JSON"
                    )
                    _FIREBASE_UNAVAILABLE_LOGGED = True
                return None

            options = {}
            project_id = os.getenv("FIREBASE_PROJECT_ID", "").strip()
            if project_id:
                options["projectId"] = project_id
            _FIREBASE_APP = firebase_admin.initialize_app(credential, options or None)
            LOGGER.info("Firebase Admin initialized for Sivdhi push delivery")
            return _FIREBASE_APP
        except Exception:
            if not _FIREBASE_UNAVAILABLE_LOGGED:
                LOGGER.exception("Firebase Admin initialization failed")
                _FIREBASE_UNAVAILABLE_LOGGED = True
            return None


def _notification_route(user_type: str) -> str:
    return {
        "customer": "/customer/notifications",
        "worker": "/worker/notifications",
        "partner": "/partner/notifications",
    }.get((user_type or "").lower(), "/")


def _canonical_owner_id(conn, user_type: str, user_id: str) -> str | None:
    """Resolve legacy notification IDs to the same account keys used at login."""
    owner = str(user_id or "")
    lookups = {
        "customer": (
            "SELECT CAST(id AS TEXT) FROM customers "
            "WHERE CAST(id AS TEXT)=? OR CAST(customer_id AS TEXT)=? "
            "ORDER BY CASE WHEN CAST(id AS TEXT)=? THEN 0 ELSE 1 END LIMIT 1",
        ),
        "worker": (
            "SELECT CAST(worker_id AS TEXT) FROM workers "
            "WHERE CAST(worker_id AS TEXT)=? OR CAST(id AS TEXT)=? "
            "ORDER BY CASE WHEN CAST(worker_id AS TEXT)=? THEN 0 ELSE 1 END LIMIT 1",
        ),
        "partner": (
            "SELECT CAST(partner_id AS TEXT) FROM partners "
            "WHERE CAST(partner_id AS TEXT)=? OR CAST(id AS TEXT)=? "
            "ORDER BY CASE WHEN CAST(partner_id AS TEXT)=? THEN 0 ELSE 1 END LIMIT 1",
        ),
    }
    query = lookups.get(user_type)
    if not query:
        return None
    try:
        row = conn.execute(query[0], (owner, owner, owner)).fetchone()
        return str(row[0]) if row and row[0] is not None else None
    except sqlite3.Error:
        LOGGER.exception("Could not resolve push recipient %s", user_type)
        return None


def _app_notifications_enabled(conn, user_type: str, user_id: str) -> bool:
    if user_type == "customer":
        ensure_customer_push_columns(conn)
        table, key, preference = "customers", "id", "notifications_enabled"
    elif user_type == "worker":
        table, key, preference = "workers", "worker_id", "app_notification"
    elif user_type == "partner":
        table, key, preference = "partners", "partner_id", "app_notification"
    else:
        return False
    try:
        row = conn.execute(
            f'SELECT "{preference}" FROM "{table}" '
            f'WHERE CAST("{key}" AS TEXT)=? LIMIT 1',
            (str(user_id),),
        ).fetchone()
        return bool(row and int(row[0] if row[0] is not None else 1))
    except (sqlite3.Error, TypeError, ValueError):
        LOGGER.exception("Could not read app-notification preference for %s", user_type)
        return False


def _set_outbox_state(conn, row_id: int, status: str, error: str | None = None,
                      retry_seconds: int | None = None) -> None:
    if retry_seconds is None:
        conn.execute(
            """UPDATE push_notification_outbox
               SET status=?, sent_at=CASE WHEN ?='sent' THEN CURRENT_TIMESTAMP ELSE sent_at END,
                   locked_at=NULL, last_error=? WHERE id=?""",
            (status, status, error, row_id),
        )
    else:
        conn.execute(
            """UPDATE push_notification_outbox
               SET status='pending', locked_at=NULL,
                   next_attempt_at=datetime('now', ?), last_error=? WHERE id=?""",
            (f"+{int(retry_seconds)} seconds", (error or "")[:500], row_id),
        )
    conn.commit()


def process_push_outbox_once(batch_size: int = 30) -> dict[str, Any]:
    """Claim and deliver one batch. Safe for multiple monitor processes to poll."""
    app = _firebase_app()
    if app is None:
        return {"configured": False, "claimed": 0, "sent": 0, "errors": 0}

    try:
        from firebase_admin import messaging
    except ImportError:
        return {"configured": False, "claimed": 0, "sent": 0, "errors": 0}

    conn = get_db()
    claimed = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        claimed = conn.execute(
            """
            SELECT id, user_type, user_id, attempts
            FROM push_notification_outbox
            WHERE (
                status='pending' AND next_attempt_at <= CURRENT_TIMESTAMP
            ) OR (
                status='processing' AND locked_at < datetime('now', '-5 minutes')
            )
            ORDER BY id ASC
            LIMIT ?
            """,
            (max(1, min(int(batch_size), 100)),),
        ).fetchall()
        for row in claimed:
            conn.execute(
                """UPDATE push_notification_outbox
                   SET status='processing', attempts=attempts+1,
                       locked_at=CURRENT_TIMESTAMP WHERE id=?""",
                (row["id"],),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        LOGGER.exception("Could not claim Sivdhi push outbox rows")
        return {"configured": True, "claimed": 0, "sent": 0, "errors": 1}
    finally:
        conn.close()

    report = {"configured": True, "claimed": len(claimed), "sent": 0,
              "no_device": 0, "errors": 0}

    for item in claimed:
        row_id = int(item["id"])
        user_type = str(item["user_type"] or "").lower()
        user_id = str(item["user_id"] or "")
        conn = get_db()
        try:
            owner_id = _canonical_owner_id(conn, user_type, user_id)
            if not owner_id or not _app_notifications_enabled(conn, user_type, owner_id):
                _set_outbox_state(conn, row_id, "skipped", "No recipient or app notifications disabled")
                continue

            tokens = conn.execute(
                """SELECT id, token FROM push_device_tokens
                   WHERE user_type=? AND user_id=? AND is_active=1""",
                (user_type, owner_id),
            ).fetchall()
            if not tokens:
                _set_outbox_state(conn, row_id, "no_device", "No registered Android device")
                report["no_device"] += 1
                continue

            sent_count = 0
            transient_error = None
            for token_row in tokens:
                try:
                    message = messaging.Message(
                        data={
                            # Deliberately generic: no customer details, phone numbers,
                            # booking amounts, or job contents are exposed on lock screens.
                            "title": {
                                "customer": "Booking update",
                                "worker": "Job update",
                                "partner": "Partner update",
                            }.get(user_type, "Sivdhi update"),
                            "body": "Open Sivdhi to view your latest notification.",
                            "route": _notification_route(user_type),
                        },
                        token=str(token_row["token"]),
                        android=messaging.AndroidConfig(
                            priority="high",
                            ttl=timedelta(hours=1),
                        ),
                    )
                    messaging.send(message, app=app)
                    sent_count += 1
                except messaging.UnregisteredError:
                    conn.execute(
                        "DELETE FROM push_device_tokens WHERE id=?",
                        (token_row["id"],),
                    )
                except Exception as exc:
                    transient_error = type(exc).__name__
                    LOGGER.warning("FCM delivery failed for outbox %s: %s", row_id, transient_error)

            if transient_error and sent_count == 0:
                attempts = int(item["attempts"] or 0) + 1
                retry = min(900, 2 ** min(attempts, 9))
                _set_outbox_state(conn, row_id, "pending", transient_error, retry)
                report["errors"] += 1
            else:
                _set_outbox_state(conn, row_id, "sent" if sent_count else "no_device",
                                  None if sent_count else "No valid registered device")
                if sent_count:
                    report["sent"] += 1
                else:
                    report["no_device"] += 1
        except Exception:
            conn.rollback()
            LOGGER.exception("Sivdhi push outbox delivery failed for row %s", row_id)
            try:
                attempts = int(item["attempts"] or 0) + 1
                _set_outbox_state(conn, row_id, "pending", "Internal delivery error",
                                  min(900, 2 ** min(attempts, 9)))
            except Exception:
                LOGGER.exception("Could not reschedule push outbox row %s", row_id)
            report["errors"] += 1
        finally:
            conn.close()

    return report
