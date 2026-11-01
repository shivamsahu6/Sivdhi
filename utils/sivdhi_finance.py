"""Safe, additive finance helpers for Sivdhi's demo payment workflow.

The demo adapter records pretend balances only; it never charges real money.
A live gateway must verify provider callbacks before a payment status is changed
from PENDING to PAID.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any

ADVANCE_CAP = 50.00
PAID_STATES = {"PAID", "SUCCESS", "DEMO_PAID", "COLLECTED", "COMPLETED"}


def _columns(conn, table: str) -> set[str]:
    try:
        return {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
    except Exception:
        return set()


def _add_columns(conn, table: str, definitions: dict[str, str]) -> None:
    existing = _columns(conn, table)
    for name, definition in definitions.items():
        if name not in existing:
            conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {definition}')


def ensure_finance_schema(conn) -> None:
    """Migrate only missing finance fields; do not drop or replace user data."""
    if "bookings" not in {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }:
        return

    _add_columns(conn, "bookings", {
        "booking_type": "TEXT DEFAULT 'normal'",
        "booking_date": "TEXT",
        "booking_time": "TEXT",
        "assigned_at": "TEXT",
        "worker_seen_at": "TEXT",
        "worker_arrived_at": "TEXT",
        "advance_amount": "REAL NOT NULL DEFAULT 0",
        "advance_paid_amount": "REAL NOT NULL DEFAULT 0",
        "balance_due_amount": "REAL NOT NULL DEFAULT 0",
        "balance_paid_amount": "REAL NOT NULL DEFAULT 0",
        "advance_payment_status": "TEXT DEFAULT 'pending'",
        "balance_payment_status": "TEXT DEFAULT 'pending'",
        "advance_payment_id": "TEXT",
        "balance_payment_id": "TEXT",
        "partner_commission_percent": "REAL NOT NULL DEFAULT 0",
        "platform_commission_percent": "REAL NOT NULL DEFAULT 0",
        "partner_commission_amount": "REAL NOT NULL DEFAULT 0",
        "platform_commission_amount": "REAL NOT NULL DEFAULT 0",
        "worker_earning_amount": "REAL NOT NULL DEFAULT 0",
        "commission_settled_at": "TEXT",
        "refund_status": "TEXT DEFAULT 'not_required'",
        "refund_amount": "REAL NOT NULL DEFAULT 0",
        "refund_reference": "TEXT",
        "refund_processed_at": "TEXT",
    })

    conn.execute("""
        CREATE TABLE IF NOT EXISTS customer_payment_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER,
            booking_id TEXT,
            payment_id TEXT UNIQUE,
            amount REAL NOT NULL DEFAULT 0,
            method TEXT,
            status TEXT DEFAULT 'pending',
            payment_type TEXT DEFAULT 'advance',
            is_demo INTEGER NOT NULL DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    _add_columns(conn, "customer_payment_records", {
        "payment_type": "TEXT DEFAULT 'advance'",
        "is_demo": "INTEGER NOT NULL DEFAULT 1",
    })

    conn.execute("""
        CREATE TABLE IF NOT EXISTS sivdhi_finance_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            booking_id TEXT NOT NULL,
            entry_type TEXT NOT NULL,
            amount REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'recorded',
            payment_mode TEXT NOT NULL DEFAULT 'demo',
            reference_id TEXT,
            description TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # The worker-attempt table differs between old app versions. Keep both
    # column families so old and new routes can safely read/write attempts.
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "booking_worker_attempts" in tables:
        _add_columns(conn, "booking_worker_attempts", {
            "status": "TEXT DEFAULT 'assigned'",
            "reason": "TEXT",
            "attempted_at": "TEXT DEFAULT CURRENT_TIMESTAMP",
            "attempt_status": "TEXT DEFAULT 'assigned'",
            "rejection_reason": "TEXT",
            "distance_km": "REAL",
        })


def is_demo_mode() -> bool:
    """Allow simulated payments locally; fail closed in production by default.

    Production demo settlement can only be enabled with the separate,
    explicit SIVDHI_ALLOW_DEMO_IN_PRODUCTION=1 override. Do not use that
    override on a public/live deployment because demo receipts are not money.
    """
    app_env = os.getenv("APP_ENV", os.getenv("FLASK_ENV", "development")).strip().lower()
    mode = os.getenv("SIVDHI_PAYMENT_MODE", "demo").strip().lower()
    allow_demo = os.getenv("SIVDHI_ALLOW_DEMO_PAYMENTS", "1").strip().lower()
    explicit_prod_override = os.getenv("SIVDHI_ALLOW_DEMO_IN_PRODUCTION", "0").strip().lower()

    if allow_demo in {"0", "false", "no", "off"}:
        return False
    if app_env == "production" and explicit_prod_override not in {"1", "true", "yes", "on"}:
        return False
    return mode in {"demo", "test", "simulation"}


def money(value: Any) -> float:
    try:
        return round(max(0.0, float(value or 0)), 2)
    except (TypeError, ValueError):
        return 0.0


def get_commission_rates(conn) -> tuple[float, float]:
    """Return partner and platform percentages; invalid settings fail safe."""
    try:
        rows = conn.execute("""
            SELECT setting_key, setting_value
            FROM admin_settings
            WHERE setting_key IN ('partner_commission_percent', 'platform_commission_percent')
        """).fetchall()
        values = {str(row[0]): float(row[1]) for row in rows}
        partner = values.get("partner_commission_percent", 10.0)
        platform = values.get("platform_commission_percent", 10.0)
        if partner < 0 or platform < 0 or partner > 100 or platform > 100 or partner + platform > 100:
            return 10.0, 10.0
        return round(partner, 2), round(platform, 2)
    except Exception:
        return 10.0, 10.0


def find_partner_for_worker(conn, worker_db_id: int) -> str | None:
    """Find an assigned worker's partner without exposing that relationship to customers."""
    try:
        worker = conn.execute(
            "SELECT id, worker_id FROM workers WHERE id=? LIMIT 1", (worker_db_id,)
        ).fetchone()
        if not worker:
            return None
        rows = conn.execute("""
            SELECT p.partner_id
            FROM partner_workers pw
            JOIN partners p ON CAST(p.partner_id AS TEXT)=CAST(pw.partner_id AS TEXT)
            WHERE CAST(pw.worker_id AS TEXT) IN (?, ?)
              AND COALESCE(p.is_deleted,0)=0
            ORDER BY pw.id ASC LIMIT 1
        """, (str(worker["worker_id"]), str(worker["id"]))).fetchone()
        return str(rows[0]) if rows and rows[0] else None
    except Exception:
        return None


def add_finance_ledger_entry(conn, booking_id: str, entry_type: str,
                             amount: float, status: str,
                             payment_mode: str, reference_id: str | None,
                             description: str) -> None:
    conn.execute("""
        INSERT INTO sivdhi_finance_ledger
            (booking_id, entry_type, amount, status, payment_mode, reference_id, description)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (str(booking_id), entry_type, money(amount), status, payment_mode,
          reference_id, description[:500]))


def settle_completed_booking(conn, booking_db_id: int) -> tuple[bool, str]:
    """Credit worker/partner/platform once, in the same DB transaction as completion."""
    ensure_finance_schema(conn)
    booking = conn.execute("SELECT * FROM bookings WHERE id=? LIMIT 1", (booking_db_id,)).fetchone()
    if not booking:
        return False, "Booking not found."
    if booking["commission_settled_at"]:
        return True, "Commission was already settled."

    booking_status = str(booking["booking_status"] or booking["status"] or "").strip().lower()
    if booking_status not in {"completed", "complete"}:
        return False, "Booking must be completed before commission settlement."

    advance_status = str(booking["advance_payment_status"] or booking["payment_status"] or "").upper()
    balance_status = str(booking["balance_payment_status"] or "").upper()
    if advance_status not in PAID_STATES or balance_status not in PAID_STATES:
        return False, "The advance and remaining balance must both be recorded as paid."

    total = money(booking["total_amount"] if booking["total_amount"] is not None else booking["amount"])
    worker_id = booking["worker_id"] or booking["assigned_worker_id"]
    if not worker_id:
        return False, "No worker is assigned to this booking."

    worker = conn.execute("""
        SELECT id, worker_id, wallet FROM workers
        WHERE id=? OR CAST(worker_id AS TEXT)=CAST(? AS TEXT)
        ORDER BY CASE WHEN id=? THEN 0 ELSE 1 END LIMIT 1
    """, (worker_id, worker_id, worker_id)).fetchone()
    if not worker:
        return False, "Assigned worker record was not found."
    worker_db_id = int(worker["id"])
    partner_id = str(booking["assigned_partner_id"] or booking["partner_id"] or "").strip()
    if not partner_id:
        partner_id = find_partner_for_worker(conn, worker_db_id) or ""
    partner = None
    if partner_id:
        partner = conn.execute("""
            SELECT partner_id, wallet FROM partners
            WHERE CAST(partner_id AS TEXT)=CAST(? AS TEXT)
              AND COALESCE(is_deleted,0)=0 LIMIT 1
        """, (partner_id,)).fetchone()
    if not partner:
        partner_id = ""

    current_partner_rate, current_platform_rate = get_commission_rates(conn)
    # New bookings store the rates at booking time so editing settings later
    # never rewrites the earnings split for an existing job.
    try:
        snapshot_partner = float(booking["partner_commission_percent"] or 0)
        snapshot_platform = float(booking["platform_commission_percent"] or 0)
        has_snapshot = bool(booking["assigned_at"]) and (
            snapshot_partner >= 0 and snapshot_platform >= 0
            and snapshot_partner <= 100 and snapshot_platform <= 100
            and snapshot_partner + snapshot_platform <= 100
        )
    except (TypeError, ValueError, IndexError):
        has_snapshot = False
        snapshot_partner = snapshot_platform = 0.0
    partner_rate = snapshot_partner if has_snapshot else current_partner_rate
    platform_rate = snapshot_platform if has_snapshot else current_platform_rate
    if partner:
        partner_amount = money(total * partner_rate / 100)
        platform_amount = money(total * platform_rate / 100)
        worker_amount = money(max(0, total - partner_amount - platform_amount))
    else:
        partner_rate = 0.0
        partner_amount = 0.0
        platform_amount = money(total * platform_rate / 100)
        worker_amount = money(max(0, total - platform_amount))

    # Make rounding add up exactly to the booking total.
    platform_amount = money(total - worker_amount - partner_amount)
    now = datetime.now().isoformat(timespec="seconds")

    conn.execute("UPDATE workers SET wallet=COALESCE(wallet,0)+? WHERE id=?", (worker_amount, worker_db_id))
    conn.execute("""
        INSERT INTO wallet_transactions(user_type,user_id,amount,transaction_type,description)
        VALUES('worker',?,?,?,?)
    """, (str(worker["worker_id"]), worker_amount, "service_earning",
          f"Earning for booking {booking['booking_id']}"))

    if partner and partner_amount > 0:
        conn.execute("""
            UPDATE partners SET wallet=COALESCE(wallet,0)+?
            WHERE CAST(partner_id AS TEXT)=CAST(? AS TEXT)
        """, (partner_amount, partner_id))
        conn.execute("""
            INSERT INTO wallet_transactions(user_type,user_id,amount,transaction_type,description)
            VALUES('partner',?,?,?,?)
        """, (partner_id, partner_amount, "partner_commission",
              f"Commission for booking {booking['booking_id']}"))

    conn.execute("""
        INSERT INTO wallet_transactions(user_type,user_id,amount,transaction_type,description)
        VALUES('platform','sivdhi',?,?,?)
    """, (platform_amount, "platform_commission",
          f"Sivdhi share for booking {booking['booking_id']}"))
    add_finance_ledger_entry(conn, str(booking["booking_id"]), "worker_earning",
                             worker_amount, "credited", "ledger", None,
                             f"Worker earning for booking {booking['booking_id']}")
    if partner_amount:
        add_finance_ledger_entry(conn, str(booking["booking_id"]), "partner_commission",
                                 partner_amount, "credited", "ledger", None,
                                 f"Partner commission for booking {booking['booking_id']}")
    add_finance_ledger_entry(conn, str(booking["booking_id"]), "platform_commission",
                             platform_amount, "retained", "ledger", None,
                             f"Sivdhi share for booking {booking['booking_id']}")

    conn.execute("""
        UPDATE bookings
        SET partner_id=?, assigned_partner_id=?,
            partner_commission_percent=?, platform_commission_percent=?,
            partner_commission_amount=?, platform_commission_amount=?,
            worker_earning_amount=?, commission_settled_at=?
        WHERE id=?
    """, (partner_id or None, partner_id or None, partner_rate, platform_rate,
          partner_amount, platform_amount, worker_amount, now, booking_db_id))
    return True, "Commission settled successfully."
