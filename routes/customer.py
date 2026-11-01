"""Sivdhi customer routes.

Customer flow:
  Login/Signup -> Home/location
  NORMAL/FULL-DAY: Category -> Subcategory -> Price/Details -> Payment

Worker-route compatibility helpers are retained for existing worker modules.
"""

import math
import os
import re
import secrets
from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo
from functools import wraps
from urllib.parse import urlsplit

from flask import (
    Blueprint,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash
from utils.email_sender import send_otp
from utils.otp_security import reserve_otp_request
from utils.pending_changes import issue_pending_change, refresh_pending_change, verify_pending_change, delete_pending_change

from database import get_db
from utils.login_security import is_login_allowed, record_login_failure, clear_login_failures
from utils.booking_schedule import (
    india_now as _india_now,
    minimum_booking_date as _minimum_booking_date,
    validate_customer_booking_slot,
)
from utils.sivdhi_finance import (
    ADVANCE_CAP, ensure_finance_schema, is_demo_mode, get_commission_rates,
    find_partner_for_worker, add_finance_ledger_entry,
)
from utils.assignment_monitor import response_seconds_for_count

try:
    from routes.decorators import customer_login_required
except ImportError:
    def customer_login_required(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not session.get("customer_id"):
                flash("Please login to continue.", "warning")
                return redirect(url_for("customer.login", next=request.path))
            return view(*args, **kwargs)
        return wrapped


customer_bp = Blueprint("customer", __name__, url_prefix="/customer")
customer = customer_bp


# =========================================================
# GENERIC HELPERS
# =========================================================

def table_exists(db, table):
    row = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def table_columns(db, table):
    if not table_exists(db, table):
        return []
    return [
        row[1]
        for row in db.execute(
            f'PRAGMA table_info("{table}")'
        ).fetchall()
    ]


def get_columns(db, table):
    """Backward-compatible alias used by worker routes."""
    return table_columns(db, table)


def first_existing(columns, *names):
    return next((name for name in names if name in columns), None)


def safe_float(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_int(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def money(value):
    return round(max(0.0, safe_float(value, 0.0) or 0.0), 2)


def format_response_window(seconds):
    """Format an assignment response slice without rounding away seconds."""
    seconds = max(1, safe_int(seconds, 1))
    minutes, remainder = divmod(seconds, 60)
    if minutes and remainder:
        return f"{minutes} minute(s) {remainder} second(s)"
    if minutes:
        return f"{minutes} minute(s)"
    return f"{remainder} second(s)"


def row_dict(row):
    return {key: row[key] for key in row.keys()} if row else None


def normalize_email(value):
    return (value or "").strip().lower()


def normalize_mobile(value):
    return re.sub(r"\D", "", value or "")


def normalise_booking_mode(value):
    value = str(value or "normal").strip().lower()
    value = value.replace("_", "-")

    if value in {"full-day", "fullday", "full day"}:
        return "full-day"

    return "normal"


def customer_db_id():
    return session.get("customer_id")


def customer_name(row):
    if not row:
        return "Customer"

    keys = row.keys()

    for key in ("fullname", "name", "customer_name"):
        if key in keys and row[key]:
            return str(row[key])

    return "Customer"


def get_customer(db, customer_id=None):
    customer_id = customer_id or customer_db_id()

    if not customer_id or not table_exists(db, "customers"):
        return None

    return db.execute(
        "SELECT * FROM customers WHERE id=? LIMIT 1",
        (customer_id,),
    ).fetchone()


def booking_number():
    return (
        f"SIV-{datetime.now().strftime('%y%m%d')}-"
        f"{secrets.token_hex(3).upper()}"
    )


def generate_booking_id():
    return booking_number()


def payment_number():
    return (
        f"PAY-{datetime.now().strftime('%y%m%d%H%M')}-"
        f"{secrets.token_hex(3).upper()}"
    )


def _worker_has_schedule_conflict(conn, worker_id, booking_date, booking_time, mode, exclude_booking_id=None):
    """Conservatively avoid assigning a worker to overlapping active jobs."""
    if not booking_date:
        return False
    try:
        rows = conn.execute("""
            SELECT id, booking_date, booking_time, preferred_date, preferred_time,
                   booking_type, service_type, booking_status, status
            FROM bookings
            WHERE (worker_id=? OR assigned_worker_id=?
                   OR CAST(assigned_worker_id AS TEXT)=(
                       SELECT worker_id FROM workers WHERE id=? LIMIT 1))
              AND id != COALESCE(?, -1)
              AND LOWER(COALESCE(NULLIF(booking_status,''), status, 'pending'))
                  IN ('matching','assigned','pending','accepted','started','in_progress')
        """, (worker_id, worker_id, worker_id, exclude_booking_id)).fetchall()
    except Exception:
        # If legacy schema/query cannot be inspected, do not reject every worker.
        return False

    try:
        requested_day = date.fromisoformat(str(booking_date)[:10])
    except ValueError:
        return False
    requested_mode = normalise_booking_mode(mode)
    try:
        requested_minutes = int(str(booking_time or "09:00").split(":")[0]) * 60 + int(str(booking_time or "09:00").split(":")[1])
    except (TypeError, ValueError, IndexError):
        requested_minutes = 9 * 60

    for row in rows:
        existing_date = row["preferred_date"] or row["booking_date"]
        if not existing_date:
            # Legacy active bookings without a schedule conservatively block the day.
            return True
        try:
            existing_day = date.fromisoformat(str(existing_date)[:10])
        except ValueError:
            continue
        if existing_day != requested_day:
            continue
        existing_mode = normalise_booking_mode(row["booking_type"] or row["service_type"] or "normal")
        if requested_mode == "full-day" or existing_mode == "full-day":
            return True
        existing_time = row["preferred_time"] or row["booking_time"]
        if not existing_time:
            return True
        try:
            existing_minutes = int(str(existing_time).split(":")[0]) * 60 + int(str(existing_time).split(":")[1])
        except (ValueError, IndexError):
            return True
        # Treat a Normal job as a two-hour block to prevent double-booking.
        if abs(existing_minutes - requested_minutes) < 120:
            return True
    return False


def _add_column_if_missing(db, table, column, definition):
    columns = table_columns(db, table)

    if columns and column not in columns:
        db.execute(
            f'ALTER TABLE "{table}" ADD COLUMN "{column}" {definition}'
        )


# =========================================================
# CUSTOMER DATABASE SETUP
# =========================================================

def ensure_customer_schema(db):
    """
    Add only missing customer-flow columns and support tables.
    Existing customer, service, and worker records are preserved.
    """

    if not table_exists(db, "customers"):
        db.execute("""
            CREATE TABLE customers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id TEXT,
                fullname TEXT,
                mobile TEXT,
                email TEXT,
                password TEXT,
                latitude REAL,
                longitude REAL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

    customer_cols = set(table_columns(db, "customers"))

    if not (
        {"fullname", "name", "customer_name"} & customer_cols
    ):
        _add_column_if_missing(db, "customers", "fullname", "TEXT")

    for column, definition in (
        ("customer_id", "TEXT"),
        ("mobile", "TEXT"),
        ("email", "TEXT"),
        ("password", "TEXT"),
        ("profile_photo", "TEXT"),
        ("address", "TEXT"),
        ("city", "TEXT"),
        ("state", "TEXT"),
        ("pincode", "TEXT"),
        ("bank_name", "TEXT"),
        ("account_holder", "TEXT"),
        ("account_number", "TEXT"),
        ("ifsc", "TEXT"),
        ("bank_verified", "INTEGER DEFAULT 0"),
        ("branch_name", "TEXT"),
        ("account_type", "TEXT"),
        ("notifications_enabled", "INTEGER DEFAULT 1"),
        ("email_notification", "INTEGER DEFAULT 1"),
        ("status", "TEXT NOT NULL DEFAULT 'active'"),
        ("is_deleted", "INTEGER NOT NULL DEFAULT 0"),
        ("booking_notifications", "INTEGER DEFAULT 1"),
        ("promotional_notifications", "INTEGER DEFAULT 0"),
        ("language", "TEXT DEFAULT 'en'"),
        ("latitude", "REAL"),
        ("longitude", "REAL"),
        ("created_at", "TEXT"),
    ):
        _add_column_if_missing(
            db, "customers", column, definition
        )

    db.execute("""
        CREATE TABLE IF NOT EXISTS customer_support_tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER NOT NULL,
            category TEXT NOT NULL,
            booking_id TEXT,
            subject TEXT NOT NULL,
            message TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    if not table_exists(db, "services"):
        db.execute("""
            CREATE TABLE services (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                category TEXT NOT NULL,
                description TEXT DEFAULT '',
                price REAL DEFAULT 0,
                full_day_price REAL DEFAULT 0,
                visit_fee REAL DEFAULT 0,
                included_work TEXT DEFAULT '',
                excluded_work TEXT DEFAULT '',
                icon TEXT DEFAULT '✦',
                active INTEGER DEFAULT 1
            )
        """)

    service_cols = set(table_columns(db, "services"))

    service_aliases = (
        (("name", "service_name", "title"), "name", "TEXT"),
        (("category", "service_category", "type"), "category", "TEXT"),
        (("description", "details"), "description", "TEXT DEFAULT ''"),
        (
            ("price", "base_price", "service_charge", "amount"),
            "price",
            "REAL DEFAULT 0",
        ),
        (("icon", "icon_emoji", "symbol"), "icon", "TEXT DEFAULT '✦'"),
        (("active", "is_active", "status"), "active", "INTEGER DEFAULT 1"),
    )

    for aliases, column, definition in service_aliases:
        if not (set(aliases) & service_cols):
            _add_column_if_missing(
                db, "services", column, definition
            )
            service_cols.add(column)

    for column, definition in (
        ("full_day_price", "REAL DEFAULT 0"),
        ("partner_commission_percent", "REAL NOT NULL DEFAULT 10"),
        ("platform_commission_percent", "REAL NOT NULL DEFAULT 10"),
        ("visit_fee", "REAL DEFAULT 0"),
        ("included_work", "TEXT DEFAULT ''"),
        ("excluded_work", "TEXT DEFAULT ''"),
    ):
        _add_column_if_missing(
            db, "services", column, definition
        )

    if not table_exists(db, "service_categories"):
        db.execute("""
            CREATE TABLE service_categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                icon TEXT DEFAULT '✦',
                description TEXT DEFAULT '',
                is_active INTEGER NOT NULL DEFAULT 1,
                normal_enabled INTEGER NOT NULL DEFAULT 1,
                full_day_enabled INTEGER NOT NULL DEFAULT 0,
                full_day_price REAL DEFAULT 0,
                visit_fee REAL DEFAULT 0,
                included_work TEXT DEFAULT '',
                excluded_work TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

    for column, definition in (
        ("icon", "TEXT DEFAULT '✦'"),
        ("description", "TEXT DEFAULT ''"),
        ("is_active", "INTEGER NOT NULL DEFAULT 1"),
        ("normal_enabled", "INTEGER NOT NULL DEFAULT 1"),
        ("full_day_enabled", "INTEGER NOT NULL DEFAULT 0"),
        ("full_day_price", "REAL DEFAULT 0"),
        ("visit_fee", "REAL DEFAULT 0"),
        ("included_work", "TEXT DEFAULT ''"),
        ("excluded_work", "TEXT DEFAULT ''"),
        ("created_at", "TEXT"),
    ):
        _add_column_if_missing(
            db, "service_categories", column, definition
        )

    db.executescript("""
        CREATE TABLE IF NOT EXISTS customer_service_problems (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            service_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            description TEXT DEFAULT '',
            price REAL NOT NULL DEFAULT 0,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(service_id, name)
        );

        CREATE TABLE IF NOT EXISTS customer_booking_problem_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            booking_id TEXT NOT NULL,
            problem_id INTEGER NOT NULL,
            problem_name TEXT NOT NULL,
            price REAL NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS customer_payment_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER,
            booking_id TEXT,
            payment_id TEXT UNIQUE,
            amount REAL,
            method TEXT,
            status TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS booking_worker_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            booking_id TEXT NOT NULL,
            worker_id INTEGER NOT NULL,
            distance_km REAL,
            status TEXT DEFAULT 'assigned',
            reason TEXT,
            attempted_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(booking_id, worker_id)
        );
    """)

    # Backfill category records from existing services only when the
    # Admin has not already created that category.
    service_cols = table_columns(db, "services")
    category_col = first_existing(
        service_cols, "category", "service_category", "type"
    )
    category_table_cols = table_columns(db, "service_categories")

    if category_col and "name" in category_table_cols:
        rows = db.execute(
            f'''
            SELECT DISTINCT "{category_col}" AS category_name
            FROM services
            WHERE COALESCE("{category_col}", '') <> ''
            '''
        ).fetchall()

        for row in rows:
            category_name_value = str(
                row["category_name"] or ""
            ).strip()

            if not category_name_value:
                continue

            exists = db.execute(
                """
                SELECT id FROM service_categories
                WHERE LOWER(name)=LOWER(?) LIMIT 1
                """,
                (category_name_value,),
            ).fetchone()

            if exists:
                continue

            values = {
                "name": category_name_value,
                "icon": "✦",
                "description": "",
                "is_active": 1,
                "normal_enabled": 1,
                "full_day_enabled": 0,
            }
            values = {
                key: value
                for key, value in values.items()
                if key in category_table_cols
            }
            cols = list(values)

            db.execute(
                f'''
                INSERT OR IGNORE INTO service_categories
                ({",".join(cols)})
                VALUES ({",".join("?" for _ in cols)})
                ''',
                [values[col] for col in cols],
            )

    # Repair older catalog rows that were created before the Admin
    # parent-category dropdown was enforced. Only link an unassigned
    # service when its name is an exact match for a category or clearly
    # starts with that category name (for example, "AC Repair" -> "AC").
    # This is additive/idempotent and does not overwrite an Admin's existing
    # parent-category choice.
    _backfill_unlinked_service_categories(db)

    # Do not add shadow columns if an existing worker field already
    # provides the same information.
    if table_exists(db, "workers"):
        worker_cols = set(table_columns(db, "workers"))

        if not (
            {"work_type", "work_mode", "employment_type",
             "availability_type", "worker_type"} & worker_cols
        ):
            _add_column_if_missing(
                db, "workers", "work_type", "TEXT DEFAULT 'both'"
            )

        if not (
            {"service_radius_km", "service_radius", "travel_range_km",
             "radius_km", "travel_radius"} & worker_cols
        ):
            _add_column_if_missing(
                db, "workers", "service_radius_km", "REAL DEFAULT 10"
            )

        if not (
            {"is_available", "available", "is_active", "active"}
            & worker_cols
        ):
            _add_column_if_missing(
                db, "workers", "is_available", "INTEGER DEFAULT 1"
            )

    ensure_finance_schema(db)
    db.commit()


def _backfill_unlinked_service_categories(db):
    'Link legacy unassigned services to an obvious existing parent category.'
    if not table_exists(db, 'service_categories') or not table_exists(db, 'services'):
        return

    category_columns = table_columns(db, 'service_categories')
    service_columns = table_columns(db, 'services')
    category_name_col = first_existing(category_columns, 'name', 'category_name', 'title')
    service_name_col = first_existing(service_columns, 'name', 'service_name', 'title')
    service_category_col = first_existing(
        service_columns, 'category', 'service_category', 'type'
    )

    if not category_name_col or not service_name_col or not service_category_col:
        return

    category_rows = db.execute(
        f'''SELECT "{category_name_col}" AS category_name
            FROM service_categories
            WHERE TRIM(COALESCE("{category_name_col}", '')) <> '' '''
    ).fetchall()

    # Longest parent name wins when categories overlap, e.g. "AC" and
    # "AC Repair". Word-boundary matching prevents "AC" matching "Action".
    category_names = []
    for row in category_rows:
        name = str(row['category_name'] or '').strip()
        normalized = re.sub(r'\s+', ' ', name.lower())
        if normalized:
            category_names.append((normalized, name))
    category_names.sort(key=lambda item: len(item[0]), reverse=True)

    if not category_names:
        return

    unlinked_rows = db.execute(
        f'''SELECT id, "{service_name_col}" AS service_name
            FROM services
            WHERE TRIM(COALESCE("{service_category_col}", '')) = '' '''
    ).fetchall()

    for row in unlinked_rows:
        service_name = re.sub(
            r'\s+', ' ', str(row['service_name'] or '').strip().lower()
        )
        if not service_name:
            continue

        matched_name = None
        for normalized_category, original_category in category_names:
            if (
                service_name == normalized_category
                or service_name.startswith(normalized_category + ' ')
                or service_name.startswith(normalized_category + '-')
                or service_name.startswith(normalized_category + '_')
            ):
                matched_name = original_category
                break

        if matched_name:
            db.execute(
                f'''UPDATE services
                    SET "{service_category_col}" = ?
                    WHERE id = ?
                      AND TRIM(COALESCE("{service_category_col}", '')) = '' ''',
                (matched_name, row['id']),
            )


def create_support_tables(db):
    """Compatibility alias used by older customer route code."""
    ensure_customer_schema(db)


# =========================================================
# WORKER-CODE COMPATIBILITY HELPERS
# =========================================================

def _ensure_worker_attempt_table(db):
    db.execute("""
        CREATE TABLE IF NOT EXISTS booking_worker_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            booking_id TEXT NOT NULL,
            worker_id INTEGER NOT NULL,
            distance_km REAL,
            status TEXT DEFAULT 'assigned',
            reason TEXT,
            attempted_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(booking_id, worker_id)
        )
    """)
    existing = set(table_columns(db, "booking_worker_attempts"))
    for name, definition in (
        ("status", "TEXT DEFAULT 'assigned'"),
        ("reason", "TEXT"),
        ("attempted_at", "TEXT DEFAULT CURRENT_TIMESTAMP"),
        ("attempt_status", "TEXT DEFAULT 'assigned'"),
        ("rejection_reason", "TEXT"),
        ("updated_at", "TEXT"),
        ("created_at", "TEXT DEFAULT CURRENT_TIMESTAMP"),
    ):
        if name not in existing:
            db.execute(f'ALTER TABLE booking_worker_attempts ADD COLUMN "{name}" {definition}')
            existing.add(name)


def get_booking_attempted_worker_ids(*args, **kwargs):
    """
    Supports the common signatures:
        get_booking_attempted_worker_ids(db, booking_id)
        get_booking_attempted_worker_ids(booking_id)
    """

    db = kwargs.get("db") or kwargs.get("conn")
    booking_id = kwargs.get("booking_id") or kwargs.get("booking")

    for arg in args:
        if hasattr(arg, "execute"):
            db = db or arg
        elif isinstance(arg, (str, int)) and booking_id is None:
            booking_id = arg

    opened_here = db is None

    if db is None:
        db = get_db()

    try:
        if not booking_id:
            return set()

        _ensure_worker_attempt_table(db)

        rows = db.execute(
            """
            SELECT worker_id
            FROM booking_worker_attempts
            WHERE booking_id=?
            """,
            (str(booking_id),),
        ).fetchall()

        return {str(row[0]) for row in rows}

    except Exception:
        return set()

    finally:
        if opened_here:
            try:
                db.close()
            except Exception:
                pass


def record_worker_attempt(db, booking_id, worker_id, distance_km=None, status="assigned"):
    try:
        _ensure_worker_attempt_table(db)
        columns = set(table_columns(db, "booking_worker_attempts"))
        values = {
            "booking_id": str(booking_id),
            "worker_id": int(worker_id),
            "distance_km": distance_km,
            "status": status or "assigned",
            "attempt_status": status or "assigned",
            "attempted_at": datetime.now().isoformat(timespec="seconds"),
            "created_at": _india_now().isoformat(timespec="seconds"),
            "updated_at": _india_now().isoformat(timespec="seconds"),
        }
        cols = [key for key in values if key in columns]
        db.execute(
            f"INSERT OR IGNORE INTO booking_worker_attempts ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})",
            [values[key] for key in cols],
        )
        return True
    except Exception:
        return False


def add_booking_attempted_worker_id(
    db, booking_id, worker_id, distance_km=None, status="assigned"
):
    return record_worker_attempt(
        db, booking_id, worker_id, distance_km, status
    )


def remove_booking_attempted_worker_id(
    db, booking_id, worker_id=None
):
    try:
        _ensure_worker_attempt_table(db)

        if worker_id is None:
            db.execute(
                "DELETE FROM booking_worker_attempts WHERE booking_id=?",
                (str(booking_id),),
            )
        else:
            db.execute(
                """
                DELETE FROM booking_worker_attempts
                WHERE booking_id=? AND worker_id=?
                """,
                (str(booking_id), int(worker_id)),
            )

        return True

    except Exception:
        return False


def clear_booking_attempted_worker_ids(db, booking_id):
    return remove_booking_attempted_worker_id(db, booking_id)


def mark_worker_attempt_rejected(db, booking_id, worker_id, reason="Worker rejected job"):
    try:
        _ensure_worker_attempt_table(db)
        columns = set(table_columns(db, "booking_worker_attempts"))
        where = "booking_id=? AND worker_id=?"
        found = db.execute(f"SELECT id FROM booking_worker_attempts WHERE {where} LIMIT 1", (str(booking_id), int(worker_id))).fetchone()
        now = datetime.now().isoformat(timespec="seconds")
        if found:
            updates = {}
            if "status" in columns: updates["status"] = "rejected"
            if "attempt_status" in columns: updates["attempt_status"] = "rejected"
            if "reason" in columns: updates["reason"] = str(reason or "Worker rejected job")[:500]
            if "rejection_reason" in columns: updates["rejection_reason"] = str(reason or "Worker rejected job")[:500]
            if "updated_at" in columns: updates["updated_at"] = now
            if updates:
                db.execute("UPDATE booking_worker_attempts SET " + ",".join(f'"{k}"=?' for k in updates) + " WHERE id=?", list(updates.values()) + [found[0]])
        else:
            values = {
                "booking_id": str(booking_id), "worker_id": int(worker_id),
                "status": "rejected", "attempt_status": "rejected",
                "reason": str(reason or "Worker rejected job")[:500],
                "rejection_reason": str(reason or "Worker rejected job")[:500],
                "attempted_at": now, "created_at": now, "updated_at": now,
            }
            keys = [key for key in values if key in columns]
            db.execute(f"INSERT OR IGNORE INTO booking_worker_attempts ({','.join(keys)}) VALUES ({','.join('?' for _ in keys)})", [values[key] for key in keys])
        return True
    except Exception:
        return False


# Aliases retained for old worker files.
mark_booking_worker_attempted = add_booking_attempted_worker_id
record_booking_worker_attempt = add_booking_attempted_worker_id
add_booking_attempted_worker = add_booking_attempted_worker_id
mark_worker_attempted = add_booking_attempted_worker_id


def has_worker_been_attempted(*args, **kwargs):
    worker_id = kwargs.get("worker_id")

    for arg in reversed(args):
        if isinstance(arg, (str, int)):
            worker_id = worker_id or arg
            break

    attempted = get_booking_attempted_worker_ids(*args, **kwargs)

    return worker_id is not None and str(worker_id) in attempted


def calculate_distance(lat1, lon1, lat2, lon2):
    """Straight-line distance between two GPS coordinates in kilometres."""

    try:
        lat1, lon1, lat2, lon2 = map(
            float, (lat1, lon1, lat2, lon2)
        )
    except (TypeError, ValueError):
        return None

    radius = 6371.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)

    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(p1)
        * math.cos(p2)
        * math.sin(dlon / 2) ** 2
    )

    a = max(0.0, min(1.0, a))

    return radius * (
        2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    )


def _worker_category_text(worker, columns):
    parts = []

    for col in (
        "skills", "category", "service_category",
        "specialization", "skill", "service", "service_type",
    ):
        if col in columns and worker[col]:
            parts.append(str(worker[col]))

    return ",".join(parts).lower()


def _worker_matches_category(worker, columns, category_name):
    target = re.sub(
        r"[^a-z0-9]+",
        " ",
        str(category_name or "").lower(),
    ).strip()

    skills = _worker_category_text(worker, columns)
    normalized = re.sub(r"[^a-z0-9]+", " ", skills).strip()

    if not target or not normalized:
        return False

    if target == "ac":
        return (
            bool(re.search(r"(^| )ac( |$)", normalized))
            or "air conditioner" in normalized
        )

    return target in normalized


def _worker_matches_mode(worker, columns, mode):
    col = first_existing(
        columns,
        "work_type",
        "work_mode",
        "employment_type",
        "availability_type",
        "worker_type",
    )

    if not col or not worker[col]:
        return True

    value = (
        str(worker[col])
        .lower()
        .replace("_", "-")
        .replace(" ", "-")
    )

    if mode == "full-day":
        return value in {
            "full-day", "fullday", "both",
            "full-day-and-part-time", "both-types",
            "full-time", "fulltime",
        }

    return value in {
        "part-time", "parttime", "normal", "both",
        "part-time-and-full-day", "both-types",
        "full-time", "fulltime",
    }


def _worker_is_active(worker, columns, allow_offline=False):
    for col in ("status", "worker_status"):
        if col in columns:
            value = str(worker[col] or "active").lower()

            if value in {
                "inactive", "blocked", "suspended",
                "rejected", "deleted", "pending", "0",
            }:
                return False

    for col in ("is_deleted", "deleted"):
        if col in columns and safe_int(worker[col], 0):
            return False

    availability_columns = ["is_available", "available", "is_active", "active"]
    if not allow_offline:
        availability_columns.append("is_online")

    for col in availability_columns:
        if col in columns and worker[col] is not None:
            value = str(worker[col]).lower()
            if value not in {"1", "true", "active", "available", "yes"}:
                return False

    # Normal bookings must target a recently connected worker. Full-Day
    # bookings are scheduled ahead (including at night), so an active worker
    # can be assigned for the future and reminded when the service day starts.
    if "last_seen_at" in columns and not allow_offline:
        last_seen = worker["last_seen_at"]
        if not last_seen:
            return False
        try:
            raw = str(last_seen).strip().replace("Z", "")
            seen = datetime.fromisoformat(raw[:19])
            current = datetime.utcnow()
            if (current - seen).total_seconds() > 15 * 60:
                return False
            if seen > current + timedelta(minutes=2):
                return False
        except (TypeError, ValueError):
            return False

    return True


def find_nearest_worker(
    conn, category, latitude, longitude, excluded_workers=None,
    exclude_booking_id=None, mode="normal", preferred_date=None,
    preferred_time=None,
):
    """Pick the nearest eligible worker within 10 km first, then extended radius."""
    if not table_exists(conn, "workers") or latitude is None or longitude is None:
        return None

    # Reassignment calls may provide only the booking ID. Reuse its saved slot.
    if exclude_booking_id and (not preferred_date or not preferred_time):
        try:
            slot_row = conn.execute(
                "SELECT preferred_date, preferred_time, booking_date, booking_time, booking_type, service_type FROM bookings WHERE id=? LIMIT 1",
                (exclude_booking_id,),
            ).fetchone()
            if slot_row:
                preferred_date = preferred_date or slot_row["preferred_date"] or slot_row["booking_date"]
                preferred_time = preferred_time or slot_row["preferred_time"] or slot_row["booking_time"]
                mode = slot_row["booking_type"] or slot_row["service_type"] or mode
        except Exception:
            pass

    columns = table_columns(conn, "workers")
    lat_col = first_existing(columns, "latitude", "lat")
    lon_col = first_existing(columns, "longitude", "lng", "lon")
    if not lat_col or not lon_col:
        return None

    excluded = {str(value) for value in (excluded_workers or set())}
    name_col = first_existing(columns, "fullname", "name")
    radius_col = first_existing(columns, "service_radius_km", "service_radius", "travel_range_km", "radius_km", "travel_radius")
    candidates = []

    try:
        rows = conn.execute("SELECT * FROM workers ORDER BY id DESC LIMIT 2000").fetchall()
    except Exception:
        return None

    for worker in rows:
        worker_db_id = worker["id"]
        public_id = worker["worker_id"] if "worker_id" in worker.keys() else worker_db_id
        if str(worker_db_id) in excluded or str(public_id) in excluded:
            continue
        if not _worker_is_active(
            worker, columns, allow_offline=(normalise_booking_mode(mode) == "full-day")
        ):
            continue
        if not _worker_matches_category(worker, columns, category):
            continue
        if not _worker_matches_mode(worker, columns, mode):
            continue

        worker_lat = safe_float(worker[lat_col])
        worker_lon = safe_float(worker[lon_col])
        if worker_lat is None or worker_lon is None:
            continue
        distance = calculate_distance(latitude, longitude, worker_lat, worker_lon)
        if distance is None:
            continue
        radius = safe_float(worker[radius_col], 10.0) if radius_col else 10.0
        if radius is None:
            radius = 10.0
        if distance > max(0.0, radius):
            continue
        if _worker_has_schedule_conflict(conn, worker_db_id, preferred_date, preferred_time, mode, exclude_booking_id):
            continue
        # Priority 0: first-mile search within 10 km; priority 1: radius extension.
        stage = 0 if distance <= 10.0 else 1
        candidates.append((stage, distance, worker, public_id, radius))

    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1], item[2]["id"]))
    stage, distance, chosen, public_id, radius = candidates[0]
    return {
        "worker": chosen,
        "worker_db_id": chosen["id"],
        "worker_id": public_id,
        "worker_name": str(chosen[name_col]) if name_col and chosen[name_col] else "Professional",
        "distance": distance,
        "distance_km": round(distance, 2),
        "search_stage": "within_10_km" if stage == 0 else "extended_radius",
        "service_radius_km": radius,
    }


def count_eligible_workers(
    conn, category, latitude, longitude, mode="normal",
    preferred_date=None, preferred_time=None, exclude_booking_id=None,
):
    """Count workers who could receive this booking under the same eligibility rules."""
    if not table_exists(conn, "workers") or latitude is None or longitude is None:
        return 0

    columns = table_columns(conn, "workers")
    lat_col = first_existing(columns, "latitude", "lat")
    lon_col = first_existing(columns, "longitude", "lng", "lon")
    if not lat_col or not lon_col:
        return 0

    radius_col = first_existing(
        columns, "service_radius_km", "service_radius", "travel_range_km",
        "radius_km", "travel_radius",
    )
    try:
        rows = conn.execute("SELECT * FROM workers ORDER BY id DESC LIMIT 2000").fetchall()
    except Exception:
        return 0

    count = 0
    full_day = normalise_booking_mode(mode) == "full-day"
    for worker in rows:
        worker_db_id = worker["id"]
        if not _worker_is_active(worker, columns, allow_offline=full_day):
            continue
        if not _worker_matches_category(worker, columns, category):
            continue
        if not _worker_matches_mode(worker, columns, mode):
            continue
        worker_lat = safe_float(worker[lat_col])
        worker_lon = safe_float(worker[lon_col])
        if worker_lat is None or worker_lon is None:
            continue
        distance = calculate_distance(latitude, longitude, worker_lat, worker_lon)
        if distance is None:
            continue
        radius = safe_float(worker[radius_col], 10.0) if radius_col else 10.0
        if radius is None:
            radius = 10.0
        if distance > max(0.0, radius):
            continue
        if _worker_has_schedule_conflict(
            conn, worker_db_id, preferred_date, preferred_time, mode, exclude_booking_id
        ):
            continue
        count += 1
    return count


# =========================================================
# LOCATION AND CUSTOMER CATALOGUE
# =========================================================

def customer_saved_coordinates(db=None):
    lat = safe_float(session.get("customer_latitude"))
    lon = safe_float(session.get("customer_longitude"))

    if lat is not None and lon is not None:
        return lat, lon

    opened_here = db is None

    if db is None:
        db = get_db()

    try:
        row = get_customer(db)

        if row:
            cols = row.keys()
            lat = safe_float(row["latitude"]) if "latitude" in cols else None
            lon = safe_float(row["longitude"]) if "longitude" in cols else None

            if lat is not None and lon is not None:
                session["customer_latitude"] = lat
                session["customer_longitude"] = lon
                session["customer_location_allowed"] = True
                return lat, lon

    finally:
        if opened_here:
            try:
                db.close()
            except Exception:
                pass

    return None, None


def _has_worker_covering_category(
    db, category_name, latitude, longitude, mode, require_recent_worker=False
):
    if not table_exists(db, "workers"):
        return False

    columns = table_columns(db, "workers")
    lat_col = first_existing(columns, "latitude", "lat")
    lon_col = first_existing(columns, "longitude", "lng", "lon")

    if not lat_col or not lon_col:
        return False

    radius_col = first_existing(
        columns,
        "service_radius_km",
        "service_radius",
        "travel_range_km",
        "radius_km",
        "travel_radius",
    )

    workers = db.execute(
        "SELECT * FROM workers ORDER BY id DESC LIMIT 1000"
    ).fetchall()

    for worker in workers:
        # Catalog display reflects registered, active coverage in the area.
        # Fresh online presence is strictly rechecked before checkout/payment.
        if not _worker_is_active(
            worker,
            columns,
            allow_offline=(
                not require_recent_worker
                or normalise_booking_mode(mode) == "full-day"
            ),
        ):
            continue

        if not _worker_matches_category(
            worker, columns, category_name
        ):
            continue

        if not _worker_matches_mode(worker, columns, mode):
            continue

        worker_lat = safe_float(worker[lat_col])
        worker_lon = safe_float(worker[lon_col])

        if worker_lat is None or worker_lon is None:
            continue

        distance = calculate_distance(
            latitude, longitude, worker_lat, worker_lon
        )

        radius = (
            safe_float(worker[radius_col], 10)
            if radius_col
            else 10
        )

        if distance is not None and distance <= max(
            0, radius if radius is not None else 10
        ):
            return True

    return False


def customer_service_rows(db, category_name):
    """
    Return customer-visible subcategories for a category.
    Admin-only commission or platform-fee columns are never returned.
    """

    columns = table_columns(db, "services")
    name_col = first_existing(columns, "name", "service_name", "title")
    category_col = first_existing(
        columns, "category", "service_category", "type"
    )

    if not name_col:
        return []

    where = []
    params = []

    if category_col:
        where.append(
            f'LOWER(COALESCE("{category_col}", ""))=LOWER(?)'
        )
        params.append(category_name)

    active_col = first_existing(columns, "active", "is_active", "status")

    if active_col and active_col != "status":
        where.append(f'COALESCE("{active_col}", 1)=1')
    elif active_col == "status":
        where.append(
            "LOWER(COALESCE(status,'active')) "
            "NOT IN ('inactive','disabled','0')"
        )

    sql = "SELECT * FROM services"

    if where:
        sql += " WHERE " + " AND ".join(where)

    sql += " ORDER BY id ASC"

    result = []

    for row in db.execute(sql, params).fetchall():
        item = row_dict(row)

        display_name = item.get(name_col) or "Service"
        display_icon = (
            item.get("icon")
            or item.get("icon_emoji")
            or "✦"
        )

        price = money(
            item.get("price")
            or item.get("base_price")
            or item.get("service_charge")
            or item.get("amount")
        )

        full_day_price = money(item.get("full_day_price"))

        for private_key in (
            "commission", "partner_commission_percent", "platform_commission_percent",
            "partner_commission_amount", "platform_commission_amount",
            "worker_earning_amount", "platform_fee", "visit_fee",
        ):
            item.pop(private_key, None)

        result.append({
            "id": safe_int(item.get("id")),
            "name": display_name,
            "display_name": display_name,
            "category": (
                item.get(category_col)
                if category_col
                else category_name
            ),
            "display_category": (
                item.get(category_col)
                if category_col
                else category_name
            ),
            "description": (
                item.get("description")
                or item.get("details")
                or ""
            ),
            "display_price": price,
            "display_full_day_price": full_day_price,
            "display_visit_fee": 0.0,
            "included_work": (
                item.get("included_work")
                or item.get("included")
                or ""
            ),
            "excluded_work": (
                item.get("excluded_work")
                or item.get("excluded")
                or ""
            ),
            "icon": display_icon,
            "display_icon": display_icon,
        })

    return result


def list_customer_categories(
    db, latitude, longitude, mode="normal", require_recent_worker=False
):
    """
    Return Admin-enabled categories with a subcategory and registered worker
    coverage in the customer's radius. For the customer catalog, worker
    coverage does not require a fresh heartbeat; checkout performs the strict
    real-time eligibility check before taking the booking advance.
    """

    if latitude is None or longitude is None:
        return []

    mode = normalise_booking_mode(mode)
    columns = table_columns(db, "service_categories")

    if "name" not in columns:
        return []

    mode_col = (
        "full_day_enabled"
        if mode == "full-day"
        else "normal_enabled"
    )

    if mode_col not in columns:
        return []

    where = [f"COALESCE({mode_col},0)=1"]

    if "is_active" in columns:
        where.append("COALESCE(is_active,1)=1")

    rows = db.execute(
        "SELECT * FROM service_categories WHERE "
        + " AND ".join(where)
        + " ORDER BY name COLLATE NOCASE"
    ).fetchall()

    categories = []

    for row in rows:
        item = row_dict(row)

        if not _has_worker_covering_category(
            db,
            item.get("name"),
            latitude,
            longitude,
            mode,
            require_recent_worker=require_recent_worker,
        ):
            continue

        subcategories = customer_service_rows(db, item["name"])
        price_key = "display_full_day_price" if mode == "full-day" else "display_price"
        subcategories = [service for service in subcategories if money(service.get(price_key)) > 0]
        if not subcategories:
            continue

        # Never leak internal commissions, settlement splits, or legacy fee columns
        # through customer page data or JSON APIs.
        for private_key in (
            "commission", "partner_commission_percent", "platform_commission_percent",
            "partner_commission_amount", "platform_commission_amount",
            "worker_earning_amount", "platform_fee", "visit_fee",
        ):
            item.pop(private_key, None)

        item.update({
            "display_name": item.get("name") or "Service",
            "display_icon": item.get("icon") or "✦",
            "subcategories": subcategories,
            "service_count": len(subcategories),
            "display_full_day_price": money(item.get("full_day_price")),
            "display_visit_fee": 0.0,
            "display_included_work": item.get("included_work") or "",
            "display_excluded_work": item.get("excluded_work") or "",
        })

        categories.append(item)

    return categories


def get_service(db, service_id):
    if not table_exists(db, "services"):
        return None

    columns = table_columns(db, "services")

    if "id" not in columns:
        return None

    row = db.execute(
        "SELECT * FROM services WHERE id=? LIMIT 1",
        (service_id,),
    ).fetchone()

    if not row:
        return None

    item = row_dict(row)

    name_col = first_existing(
        columns, "name", "service_name", "title"
    )
    category_col = first_existing(
        columns, "category", "service_category", "type"
    )

    item["display_name"] = (
        item.get(name_col) if name_col else "Service"
    ) or "Service"

    item["display_category"] = (
        item.get(category_col) if category_col else item["display_name"]
    ) or item["display_name"]

    item["display_price"] = money(
        item.get("price")
        or item.get("base_price")
        or item.get("service_charge")
        or item.get("amount")
    )

    item["display_full_day_price"] = money(item.get("full_day_price"))

    for private_key in (
        "commission", "partner_commission_percent", "platform_commission_percent",
        "partner_commission_amount", "platform_commission_amount",
        "worker_earning_amount", "platform_fee", "visit_fee",
    ):
        item.pop(private_key, None)
    item["display_visit_fee"] = 0.0
    item["display_icon"] = (
        item.get("icon") or item.get("icon_emoji") or "✦"
    )
    item["included_work"] = (
        item.get("included_work") or item.get("included") or ""
    )
    item["excluded_work"] = (
        item.get("excluded_work") or item.get("excluded") or ""
    )

    return item


def get_category(db, category_id):
    row = db.execute(
        "SELECT * FROM service_categories WHERE id=? LIMIT 1",
        (category_id,),
    ).fetchone()

    item = row_dict(row)

    if not item:
        return None

    item["display_name"] = item.get("name") or "Service"
    item["display_icon"] = item.get("icon") or "✦"
    item["display_full_day_price"] = money(item.get("full_day_price"))
    for private_key in (
        "commission", "partner_commission_percent", "platform_commission_percent",
        "partner_commission_amount", "platform_commission_amount",
        "worker_earning_amount", "platform_fee", "visit_fee",
    ):
        item.pop(private_key, None)
    item["display_visit_fee"] = 0.0
    item["included_work"] = item.get("included_work") or ""
    item["excluded_work"] = item.get("excluded_work") or ""

    return item


def get_problem_rows(db, service_id):
    rows = db.execute(
        """
        SELECT id, name, description, price
        FROM customer_service_problems
        WHERE service_id=? AND COALESCE(is_active,1)=1
        ORDER BY id ASC
        """,
        (service_id,),
    ).fetchall()

    return [
        {
            "id": safe_int(row["id"]),
            "name": row["name"],
            "description": row["description"] or "",
            "price": money(row["price"]),
        }
        for row in rows
    ]


def validate_category_available(db, category_id, mode):
    lat, lon = customer_saved_coordinates(db)

    if lat is None or lon is None:
        return None, "Allow location on the Home page to continue."

    categories = list_customer_categories(
        db, lat, lon, mode
    )

    for item in categories:
        if safe_int(item.get("id")) == safe_int(category_id):
            return item, None

    return None, (
        "This category is not currently available in your area "
        "for this booking type."
    )


def set_selected_service(service):
    session["selected_service_id"] = safe_int(service.get("id"))
    session["selected_service_name"] = (
        service.get("display_name")
        or service.get("name")
        or "Service"
    )
    session["selected_category"] = (
        service.get("display_category")
        or service.get("category")
        or "Service"
    )
    session["selected_service_icon"] = (
        service.get("display_icon")
        or service.get("icon")
        or "✦"
    )


def current_selected_service(db):
    service_id = safe_int(
        session.get("selected_service_id"), 0
    )

    return get_service(db, service_id) if service_id else None


def calculate_current_amount(db):
    """
    Recalculate prices from the database.
    Client-submitted prices are not trusted.
    Customer platform fee/commission is not added or displayed.
    """

    mode = normalise_booking_mode(
        session.get("booking_mode") or "normal"
    )
    # Sivdhi's current catalogue price is category Full-Day price or the sum
    # of selected problem prices. No legacy visit/platform surcharge is added.
    visit_fee = 0.0

    if mode == "full-day":
        base_price = money(
            session.get("selected_full_day_price", 0)
        )

        return {
            "mode": mode,
            "problems": [],
            "problem_total": 0,
            "service_price": base_price,
            "visit_fee": visit_fee,
            "total": money(base_price + visit_fee),
        }

    chosen_ids = session.get("selected_problem_ids") or []

    if not isinstance(chosen_ids, list) or not chosen_ids:
        chosen_ids = []

    problems = []
    service_id = safe_int(session.get("selected_service_id"), 0)

    # New catalogue model: a subcategory itself owns the Normal price.
    # Legacy problem selections are still read for old in-progress sessions only.
    if not chosen_ids and service_id:
        service = get_service(db, service_id)
        base_price = money((service or {}).get("display_price", 0))
        return {
            "mode": mode, "problems": [], "problem_total": 0,
            "service_price": base_price, "visit_fee": visit_fee,
            "total": money(base_price + visit_fee),
        }

    if chosen_ids and service_id:
        ids = sorted({
            safe_int(value)
            for value in chosen_ids
            if safe_int(value) > 0
        })

        if ids:
            placeholders = ",".join("?" for _ in ids)

            rows = db.execute(
                f"""
                SELECT id, name, description, price
                FROM customer_service_problems
                WHERE service_id=?
                  AND COALESCE(is_active,1)=1
                  AND id IN ({placeholders})
                ORDER BY id ASC
                """,
                [service_id] + ids,
            ).fetchall()

            problems = [
                {
                    "id": safe_int(row["id"]),
                    "name": row["name"],
                    "description": row["description"] or "",
                    "price": money(row["price"]),
                }
                for row in rows
            ]

    if problems:
        subtotal = money(sum(problem["price"] for problem in problems))
    else:
        selected_service = current_selected_service(db)
        subtotal = money((selected_service or {}).get("display_price", 0))

    return {
        "mode": mode,
        "problems": problems,
        "problem_total": subtotal,
        "service_price": subtotal,
        "visit_fee": visit_fee,
        "total": money(subtotal + visit_fee),
    }


# =========================================================
# LOGIN / SIGNUP
# =========================================================

@customer_bp.route("/login", methods=["GET", "POST"])
def login():
    if session.get("customer_id"):
        return redirect(url_for("customer.home"))

    if request.method == "POST":
        identifier = (
            request.form.get("login")
            or request.form.get("mobile")
            or request.form.get("phone")
            or request.form.get("email")
            or ""
        ).strip()

        password = request.form.get("password") or ""

        if not identifier or not password:
            flash(
                "Mobile/email and password are required.",
                "danger",
            )
            return render_template("customer/login.html")

        client_ip = request.remote_addr or "unknown"
        if not is_login_allowed("customer", identifier, client_ip):
            flash("Too many failed login attempts. Please try again later.", "danger")
            return render_template("customer/login.html")

        db = get_db()

        try:
            ensure_customer_schema(db)
            columns = table_columns(db, "customers")
            row = None

            if "@" in identifier and "email" in columns:
                row = db.execute(
                    """
                    SELECT * FROM customers
                    WHERE LOWER(email)=LOWER(?) LIMIT 1
                    """,
                    (normalize_email(identifier),),
                ).fetchone()

            if row is None and "mobile" in columns:
                row = db.execute(
                    "SELECT * FROM customers WHERE mobile=? LIMIT 1",
                    (normalize_mobile(identifier),),
                ).fetchone()

            if row is None and "customer_id" in columns:
                row = db.execute(
                    """
                    SELECT * FROM customers
                    WHERE customer_id=? LIMIT 1
                    """,
                    (identifier,),
                ).fetchone()

            if not row:
                record_login_failure("customer", identifier, client_ip)
                flash(
                    "Invalid mobile/email or password. Check your details or sign up.",
                    "danger",
                )
                return render_template("customer/login.html")

            stored_password = (
                row["password"]
                if "password" in row.keys()
                else ""
            )

            # Never accept a plaintext database password as a fallback. All
            # supported signup/reset flows store Werkzeug password hashes.
            try:
                password_ok = bool(stored_password) and check_password_hash(
                    stored_password, password
                )
            except (ValueError, TypeError):
                password_ok = False

            if not password_ok:
                record_login_failure("customer", identifier, client_ip)
                flash(
                    "Invalid mobile/email or password.",
                    "danger",
                )
                return render_template("customer/login.html")

            clear_login_failures("customer", identifier, client_ip)
            session.clear()
            session.permanent = True
            session["customer_id"] = row["id"]
            session["customer_code"] = (
                row["customer_id"]
                if "customer_id" in row.keys()
                else f"CUS-{row['id']}"
            )
            session["customer_name"] = customer_name(row)
            session["customer_email"] = (
                row["email"] if "email" in row.keys() else ""
            )
            session["customer_mobile"] = (
                row["mobile"] if "mobile" in row.keys() else ""
            )

            lat = (
                safe_float(row["latitude"])
                if "latitude" in row.keys()
                else None
            )
            lon = (
                safe_float(row["longitude"])
                if "longitude" in row.keys()
                else None
            )

            if lat is not None and lon is not None:
                session["customer_latitude"] = lat
                session["customer_longitude"] = lon
                session["customer_location_allowed"] = True

            next_url = request.args.get("next") or request.form.get("next")
            parsed = urlsplit(next_url or "")

            if (
                next_url
                and next_url.startswith("/")
                and not next_url.startswith("//")
                and "\\" not in next_url
                and "\r" not in next_url
                and "\n" not in next_url
                and not parsed.scheme
                and not parsed.netloc
            ):
                return redirect(next_url)

            return redirect(url_for("customer.home"))

        finally:
            db.close()

    return render_template("customer/login.html")


@customer_bp.route("/signup", methods=["GET", "POST"])
def signup():
    if session.get("customer_id"):
        return redirect(url_for("customer.home"))

    if request.method == "POST":
        name = (
            request.form.get("fullname")
            or request.form.get("name")
            or ""
        ).strip()

        mobile = normalize_mobile(
            request.form.get("mobile")
            or request.form.get("phone")
        )

        email = normalize_email(
            request.form.get("email")
            or request.form.get("email_address")
        )

        password = request.form.get("password") or ""

        confirm = (
            request.form.get("confirm_password")
            or request.form.get("password_confirmation")
            or password
        )

        if not name or not mobile or not email or not password:
            flash("Please complete all required fields.", "danger")
            return render_template("customer/signup.html")

        if len(mobile) != 10 or not mobile.isdigit():
            flash(
                "Enter a valid 10-digit mobile number.",
                "danger",
            )
            return render_template("customer/signup.html")

        if "@" not in email or "." not in email.rsplit("@", 1)[-1]:
            flash("Enter a valid email address.", "danger")
            return render_template("customer/signup.html")

        if (
            len(password) < 8
            or not re.search(r"[A-Za-z]", password)
            or not re.search(r"\d", password)
            or password != confirm
        ):
            flash(
                "Use at least 8 characters including a letter and a number, and make both passwords match.",
                "danger",
            )
            return render_template("customer/signup.html")

        db = get_db()

        try:
            ensure_customer_schema(db)

            if db.execute(
                "SELECT id FROM customers WHERE mobile=? LIMIT 1",
                (mobile,),
            ).fetchone():
                flash(
                    "This mobile number is already registered. Please login.",
                    "danger",
                )
                return redirect(url_for("customer.login"))

            if db.execute(
                """
                SELECT id FROM customers
                WHERE LOWER(email)=LOWER(?) LIMIT 1
                """,
                (email,),
            ).fetchone():
                flash(
                    "This email is already registered. Please login.",
                    "danger",
                )
                return redirect(url_for("customer.login"))

            columns = table_columns(db, "customers")
            payload = {}

            if "customer_id" in columns:
                payload["customer_id"] = (
                    f"CUS-{secrets.token_hex(4).upper()}"
                )

            if "fullname" in columns:
                payload["fullname"] = name
            elif "name" in columns:
                payload["name"] = name
            elif "customer_name" in columns:
                payload["customer_name"] = name

            if "mobile" in columns:
                payload["mobile"] = mobile
            if "email" in columns:
                payload["email"] = email
            if "password" in columns:
                payload["password"] = generate_password_hash(password)
            if "created_at" in columns:
                payload["created_at"] = datetime.now().isoformat(
                    timespec="seconds"
                )

            cols = list(payload)

            cursor = db.execute(
                f"""
                INSERT INTO customers ({",".join(cols)})
                VALUES ({",".join("?" for _ in cols)})
                """,
                [payload[col] for col in cols],
            )

            db.commit()

            session.clear()
            session["customer_id"] = cursor.lastrowid
            session["customer_code"] = payload.get(
                "customer_id", f"CUS-{cursor.lastrowid}"
            )
            session["customer_name"] = name
            session["customer_email"] = email
            session["customer_mobile"] = mobile

            # Signup opens Home immediately, where location is requested.
            return redirect(url_for("customer.home"))

        except Exception:
            db.rollback()
            current_app.logger.exception("Customer signup failed")
            flash(
                "Account could not be created. Please try again.",
                "danger",
            )
            return render_template("customer/signup.html")

        finally:
            db.close()

    return render_template("customer/signup.html")


@customer_bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("customer.login"))


# =========================================================
# HOME AND LOCATION PERMISSION
# =========================================================

@customer_bp.route("/api/location", methods=["POST"])
@customer_login_required
def save_customer_location_api():
    data = request.get_json(silent=True) or request.form

    latitude = safe_float(
        data.get("latitude", data.get("lat"))
    )
    longitude = safe_float(
        data.get("longitude", data.get("lng", data.get("lon")))
    )

    if (
        latitude is None
        or longitude is None
        or not -90 <= latitude <= 90
        or not -180 <= longitude <= 180
    ):
        return jsonify({
            "success": False,
            "message": "Valid location coordinates are required.",
        }), 400

    db = get_db()

    try:
        ensure_customer_schema(db)

        row = get_customer(db)

        if not row:
            return jsonify({
                "success": False,
                "message": "Customer account was not found.",
            }), 401

        columns = table_columns(db, "customers")
        updates = {}

        if "latitude" in columns:
            updates["latitude"] = latitude
        if "longitude" in columns:
            updates["longitude"] = longitude

        if updates:
            assignments = ", ".join(
                f'"{column}"=?' for column in updates
            )

            db.execute(
                f"UPDATE customers SET {assignments} WHERE id=?",
                list(updates.values()) + [row["id"]],
            )
            db.commit()

        session["customer_latitude"] = latitude
        session["customer_longitude"] = longitude
        session["customer_location_allowed"] = True
        session["customer_location_name"] = "Current location"

        mode = normalise_booking_mode(
            data.get("mode")
            or session.get("booking_mode")
            or "normal"
        )
        session["booking_mode"] = mode

        categories = list_customer_categories(
            db, latitude, longitude, mode
        )

        return jsonify({
            "success": True,
            "mode": mode,
            "location": {"label": "Current location"},
            "categories": categories,
            "service_area_available": bool(categories),
            "message": (
                "Location saved."
                if categories
                else
                "Location saved, but no eligible category is "
                "available in this area for this service type."
            ),
        })

    except Exception:
        db.rollback()
        current_app.logger.exception(
            "Could not save customer location"
        )

        return jsonify({
            "success": False,
            "message": "Could not save location. Please try again.",
        }), 500

    finally:
        db.close()


@customer_bp.route("/api/categories")
@customer_login_required
def customer_categories_api():
    mode = normalise_booking_mode(
        request.args.get("mode")
        or session.get("booking_mode")
        or "normal"
    )
    session["booking_mode"] = mode

    db = get_db()

    try:
        ensure_customer_schema(db)

        latitude, longitude = customer_saved_coordinates(db)

        if latitude is None or longitude is None:
            return jsonify({
                "success": True,
                "requires_location": True,
                "categories": [],
            })

        categories = list_customer_categories(
            db, latitude, longitude, mode
        )

        return jsonify({
            "success": True,
            "mode": mode,
            "categories": categories,
            "service_area_available": bool(categories),
        })

    except Exception:
        current_app.logger.exception(
            "Could not load customer categories"
        )

        return jsonify({
            "success": False,
            "message": "Could not load categories.",
            "categories": [],
        }), 500

    finally:
        db.close()


@customer_bp.route("/")
@customer_bp.route("/home")
@customer_login_required
def home():
    mode = normalise_booking_mode(
        request.args.get("mode")
        or session.get("booking_mode")
        or "normal"
    )
    session["booking_mode"] = mode

    db = get_db()

    try:
        ensure_customer_schema(db)

        customer = get_customer(db)
        latitude, longitude = customer_saved_coordinates(db)
        location_allowed = (
            latitude is not None and longitude is not None
        )

        categories = (
            list_customer_categories(
                db, latitude, longitude, mode
            )
            if location_allowed
            else []
        )

        return render_template(
            "customer/home.html",
            customer=customer,
            categories=categories,
            location_allowed=location_allowed,
            location_name=session.get(
                "customer_location_name",
                "Current location" if location_allowed else "",
            ),
            booking_mode=mode,
        )

    finally:
        db.close()


# =========================================================
# CATEGORY -> SUBCATEGORY (NORMAL)
# CATEGORY -> DETAILS (FULL-DAY)
# =========================================================

@customer_bp.route("/category/<int:category_id>")
@customer_login_required
def category(category_id):
    mode = normalise_booking_mode(
        request.args.get("mode") or session.get("booking_mode") or "normal"
    )
    session["booking_mode"] = mode
    db = get_db()
    try:
        ensure_customer_schema(db)
        category_data, error = validate_category_available(db, category_id, mode)
        if error:
            flash(error, "warning")
            return redirect(url_for("customer.home", mode=mode))
        subcategories = customer_service_rows(db, category_data.get("name"))
        price_key = "display_full_day_price" if mode == "full-day" else "display_price"
        subcategories = [service for service in subcategories if money(service.get(price_key)) > 0]
        if not subcategories:
            flash("No subcategories with a configured price are available in this category yet.", "warning")
            return redirect(url_for("customer.home", mode=mode))
        return render_template(
            "customer/subcategories.html",
            category=category_data,
            subcategories=subcategories,
            booking_mode=mode,
        )
    finally:
        db.close()


@customer_bp.route("/service/<int:service_id>")
@customer_login_required
def service(service_id):
    mode = normalise_booking_mode(
        request.args.get("mode") or session.get("booking_mode") or "normal"
    )
    db = get_db()
    try:
        ensure_customer_schema(db)
        service_data = get_service(db, service_id)
        if not service_data:
            flash("Subcategory not found.", "warning")
            return redirect(url_for("customer.home", mode=mode))

        latitude, longitude = customer_saved_coordinates(db)
        if latitude is None or longitude is None:
            flash("Allow location on Home before continuing.", "warning")
            return redirect(url_for("customer.home", mode=mode))

        permitted = {
            str(item.get("name", "")).lower()
            for item in list_customer_categories(db, latitude, longitude, mode)
        }
        if str(service_data.get("display_category", "")).lower() not in permitted:
            flash("This service is not currently available in your area.", "warning")
            return redirect(url_for("customer.home", mode=mode))

        category_row = db.execute(
            "SELECT * FROM service_categories WHERE LOWER(name)=LOWER(?) LIMIT 1",
            (service_data.get("display_category"),),
        ).fetchone()
        category_data = row_dict(category_row) or {"name": service_data.get("display_category")}
        if mode == "full-day" and money(service_data.get("display_full_day_price")) <= 0:
            flash("Full-day price is not configured for this subcategory yet.", "warning")
            return redirect(url_for("customer.category", category_id=safe_int(category_data.get("id"), 0), mode=mode))
        if mode == "normal" and money(service_data.get("display_price")) <= 0:
            flash("Normal price is not configured for this subcategory yet.", "warning")
            return redirect(url_for("customer.category", category_id=safe_int(category_data.get("id"), 0), mode=mode))

        session["selected_service_id"] = service_id
        session["selected_service_name"] = service_data.get("display_name")
        session["selected_category"] = service_data.get("display_category")
        session["selected_category_id"] = safe_int(category_data.get("id"), 0)
        session["selected_service_icon"] = service_data.get("display_icon")
        session["selected_problem_ids"] = []
        session["selected_problems_snapshot"] = []
        session["selected_visit_fee"] = 0
        session["booking_mode"] = mode
        if mode == "full-day":
            session["selected_full_day_price"] = money(service_data.get("display_full_day_price"))
        else:
            session.pop("selected_full_day_price", None)
        return redirect(url_for("customer.service_details"))
    finally:
        db.close()


@customer_bp.route(
    "/problems/<int:service_id>",
    methods=["GET", "POST"],
)
@customer_login_required
def problems(service_id):
    # Legacy URL retained for old bookmarks; problem-level selection is retired.
    return redirect(url_for("customer.service", service_id=service_id, mode="normal"))


# =========================================================
# DETAILS PAGE -> PAYMENT
# =========================================================

@customer_bp.route("/service-details", methods=["GET", "POST"])
@customer_login_required
def service_details():
    db = get_db()
    try:
        ensure_customer_schema(db)
        ensure_finance_schema(db)
        mode = normalise_booking_mode(session.get("booking_mode") or "normal")
        selected = current_selected_service(db)
        category_data = None

        if mode == "full-day":
            category_id = safe_int(session.get("selected_category_id"), 0)
            category_data = get_category(db, category_id) if category_id else None
            if not selected and not category_data:
                flash("Choose a Full-Day category first.", "warning")
                return redirect(url_for("customer.home", mode="full-day"))
        else:
            if not selected:
                return redirect(url_for("customer.home", mode="normal"))
            amount = calculate_current_amount(db)
            if amount["total"] <= 0:
                flash("Normal price is not configured for this subcategory.", "warning")
                return redirect(url_for("customer.home", mode="normal"))

        latitude, longitude = customer_saved_coordinates(db)
        if latitude is None or longitude is None:
            flash("Allow location on Home before continuing.", "warning")
            return redirect(url_for("customer.home", mode=mode))

        amount = calculate_current_amount(db)
        if request.method == "POST":
            requested_date = request.form.get("booking_date", "").strip()
            requested_time = request.form.get("booking_time", "").strip()
            valid_date, valid_time, slot_error = validate_customer_booking_slot(mode, requested_date, requested_time)
            if slot_error:
                flash(slot_error, "warning")
                return redirect(url_for("customer.service_details"))
            session["booking_date"] = valid_date
            session["booking_time"] = valid_time
            session["booking_description"] = (request.form.get("description") or "").strip()[:1000]
            return redirect(url_for("customer.payment"))

        today_min = _minimum_booking_date(mode)
        booking_date_value = session.get("booking_date") or today_min
        booking_time_value = session.get("booking_time") or ("09:00" if mode == "full-day" else "")

        if mode == "full-day":
            included = (category_data or {}).get("included_work") or (selected or {}).get("included_work") or ""
            excluded = (category_data or {}).get("excluded_work") or (selected or {}).get("excluded_work") or ""
            return render_template(
                "customer/service_details.html", service=selected or {}, category=category_data or {},
                problems=[], selected_problems=[], booking_mode="full-day", total_amount=amount["total"],
                visit_fee=amount["visit_fee"], included_work=included, excluded_work=excluded,
                min_booking_date=today_min, booking_date=booking_date_value, booking_time=booking_time_value,
            )

        return render_template(
            "customer/service_details.html", service=selected,
            category={"name": selected.get("display_category")},
            problems=amount["problems"], selected_problems=amount["problems"],
            booking_mode="normal", total_amount=amount["total"], visit_fee=amount["visit_fee"],
            included_work=selected.get("included_work") or "", excluded_work=selected.get("excluded_work") or "",
            min_booking_date=today_min, booking_date=booking_date_value, booking_time=booking_time_value,
        )
    finally:
        db.close()


# =========================================================
# PAYMENT
# =========================================================

@customer_bp.route("/payment", methods=["GET", "POST"])
@customer_bp.route("/payments", methods=["GET", "POST"])
@customer_login_required
def payment():
    db = get_db()
    try:
        ensure_customer_schema(db)
        ensure_finance_schema(db)
        mode = normalise_booking_mode(session.get("booking_mode") or "normal")
        selected = current_selected_service(db)
        if mode == "normal":
            if not selected:
                flash("Choose a subcategory first.", "warning")
                return redirect(url_for("customer.home", mode="normal"))
        elif not session.get("selected_category"):
            flash("Choose a Full-Day category first.", "warning")
            return redirect(url_for("customer.home", mode="full-day"))

        amount = calculate_current_amount(db)
        total = money(amount["total"])
        if total <= 0:
            flash("The service price is not configured yet. Please contact Sivdhi support.", "warning")
            return redirect(url_for("customer.service_details"))

        scheduled_date = session.get("booking_date")
        scheduled_time = session.get("booking_time") or ("09:00" if mode == "full-day" else "")
        valid_date, valid_time, slot_error = validate_customer_booking_slot(mode, scheduled_date, scheduled_time)
        if slot_error:
            flash(slot_error, "warning")
            return redirect(url_for("customer.service_details"))
        session["booking_date"] = valid_date
        session["booking_time"] = valid_time

        latitude, longitude = customer_saved_coordinates(db)
        if latitude is None or longitude is None:
            flash("Allow location on Home before continuing.", "warning")
            return redirect(url_for("customer.home", mode=mode))

        category_name = str(session.get("selected_category") or (selected or {}).get("display_category") or "Service")
        # Revalidate Admin mode switches at checkout; a customer may have left
        # a detail/payment tab open after a category was disabled.
        category_row = db.execute("""
            SELECT id, name, is_active, normal_enabled, full_day_enabled
            FROM service_categories
            WHERE LOWER(name)=LOWER(?)
            LIMIT 1
        """, (category_name,)).fetchone()
        mode_flag = "full_day_enabled" if mode == "full-day" else "normal_enabled"
        if (not category_row or not int(category_row["is_active"] or 0)
                or not int(category_row[mode_flag] or 0)):
            flash("This service has been disabled by Sivdhi Admin. Please choose another service.", "warning")
            return redirect(url_for("customer.home", mode=mode))

        demo_mode = is_demo_mode()
        advance_amount = money(min(ADVANCE_CAP, total))
        balance_due = money(max(0.0, total - advance_amount))

        # Check eligibility before showing/accepting any payment. Re-check on POST
        # under a write lock to avoid recording a payment when no worker is available.
        if request.method == "GET":
            candidate = find_nearest_worker(
                db, category_name, latitude, longitude, mode=mode,
                preferred_date=valid_date, preferred_time=valid_time,
            )
            if not candidate:
                flash("Abhi is slot ke liye aapke area mein koi eligible worker available nahi hai. Payment nahi liya gaya.", "warning")
                return redirect(url_for("customer.home", mode=mode))

            return render_template(
                "customer/payment.html",
                service=selected or {"display_name": category_name, "display_icon": session.get("selected_service_icon", "✦")},
                category={"name": category_name}, booking_mode=mode,
                problems=amount["problems"], service_charge=amount["service_price"],
                visit_fee=amount["visit_fee"], total_amount=total,
                advance_amount=advance_amount, balance_due=balance_due,
                payment_mode="demo" if demo_mode else "live",
                booking_date=valid_date, booking_time=valid_time,
            )

        # A form submit can create a demo receipt, not a real gateway success.
        # Live mode is intentionally blocked until an actual provider verifies it.
        if not demo_mode and not session.get("payment_verified"):
            flash("Live payment provider abhi configure nahi hai; koi payment ya booking create nahi hui.", "warning")
            return redirect(url_for("customer.payment"))

        customer_row = get_customer(db)
        if not customer_row:
            flash("Customer account not found. Please login again.", "warning")
            return redirect(url_for("customer.login"))

        payment_method = (request.form.get("payment_method") or "demo").strip().lower()
        if demo_mode:
            payment_method = "demo"
        elif payment_method not in {"upi", "card", "netbanking", "cash"}:
            payment_method = "online"

        db.execute("BEGIN IMMEDIATE")
        candidate = find_nearest_worker(
            db, category_name, latitude, longitude, mode=mode,
            preferred_date=valid_date, preferred_time=valid_time,
        )
        if not candidate:
            db.rollback()
            flash("Booking ke samay worker unavailable ho gaya. Payment nahi liya gaya; doosra slot choose karein.", "warning")
            return redirect(url_for("customer.home", mode=mode))

        booking_id = booking_number()
        payment_id = payment_number()
        worker_db_id = int(candidate["worker_db_id"])
        partner_id = find_partner_for_worker(db, worker_db_id)
        partner_rate, platform_rate = get_commission_rates(db)
        if selected:
            try:
                service_partner_rate = float(selected.get("partner_commission_percent") or 0)
                service_platform_rate = float(selected.get("platform_commission_percent") or 0)
                if 0 <= service_partner_rate <= 100 and 0 <= service_platform_rate <= 100 and service_partner_rate + service_platform_rate <= 100:
                    partner_rate, platform_rate = service_partner_rate, service_platform_rate
            except (TypeError, ValueError):
                pass
        service_id_for_rates = safe_int(session.get("selected_service_id"), 0)
        service_cols_for_rates = set(table_columns(db, "services"))
        if service_id_for_rates and {"partner_commission_percent", "platform_commission_percent"}.issubset(service_cols_for_rates):
            rates_row = db.execute(
                "SELECT partner_commission_percent, platform_commission_percent FROM services WHERE id=? LIMIT 1",
                (service_id_for_rates,),
            ).fetchone()
            if rates_row:
                try:
                    candidate_partner_rate = float(rates_row["partner_commission_percent"] or 0)
                    candidate_platform_rate = float(rates_row["platform_commission_percent"] or 0)
                    if 0 <= candidate_partner_rate <= 100 and 0 <= candidate_platform_rate <= 100 and candidate_partner_rate + candidate_platform_rate <= 100:
                        partner_rate, platform_rate = candidate_partner_rate, candidate_platform_rate
                except (TypeError, ValueError):
                    pass
        payment_status = "PARTIALLY_PAID_DEMO" if demo_mode else "PARTIALLY_PAID"
        advance_status = "DEMO_PAID" if demo_mode else "PAID"
        values = {
            "booking_id": booking_id,
            "customer_id": customer_row["id"],
            "service_id": safe_int(session.get("selected_service_id"), 0) or None,
            "category": category_name,
            "service": (selected or {}).get("display_name") or category_name,
            "description": session.get("booking_description") or (selected or {}).get("display_name") or category_name,
            "address": "GPS location shared by customer",
            "latitude": latitude,
            "longitude": longitude,
            "service_charge": amount["service_price"],
            "visit_fee": amount["visit_fee"],
            "platform_fee": 0,
            "total_amount": total,
            "amount": total,
            "payment_method": payment_method,
            "payment_id": payment_id,
            "payment_status": payment_status,
            "advance_amount": advance_amount,
            "advance_paid_amount": advance_amount,
            "balance_due_amount": balance_due,
            "balance_paid_amount": 0,
            "advance_payment_status": advance_status,
            "balance_payment_status": "DUE" if balance_due > 0 else advance_status,
            "advance_payment_id": payment_id,
            "booking_status": "assigned",
            "status": "assigned",
            "booking_type": mode,
            "service_type": mode,
            "booking_date": valid_date,
            "booking_time": valid_time,
            "preferred_date": valid_date,
            "preferred_time": valid_time,
            "worker_id": worker_db_id,
            "assigned_worker_id": worker_db_id,
            "partner_id": partner_id,
            "assigned_partner_id": partner_id,
            "assigned_at": _india_now().isoformat(timespec="seconds"),
            # Split the one-hour assignment window evenly across all eligible workers
            # in this service area; the same allocation is reused for each retry.
            "assignment_response_seconds": response_seconds_for_count(count_eligible_workers(
                db, category_name, latitude, longitude, mode=mode,
                preferred_date=valid_date, preferred_time=valid_time,
            )),
            "partner_commission_percent": partner_rate if partner_id else 0,
            "platform_commission_percent": platform_rate,
            "created_at": _india_now().isoformat(timespec="seconds"),
            "updated_at": _india_now().isoformat(timespec="seconds"),
        }
        booking_columns = set(table_columns(db, "bookings"))
        usable = {key: value for key, value in values.items() if key in booking_columns}
        cols = list(usable)
        db.execute(
            f"INSERT INTO bookings ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})",
            [usable[col] for col in cols],
        )
        booking_db_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]

        for problem in amount["problems"]:
            db.execute("""
                INSERT INTO customer_booking_problem_items
                    (booking_id, problem_id, problem_name, price)
                VALUES (?, ?, ?, ?)
            """, (booking_id, problem["id"], problem["name"], problem["price"]))

        record_worker_attempt(db, booking_db_id, worker_db_id, candidate.get("distance_km"), "assigned")
        db.execute("""
            INSERT INTO customer_payment_records
                (customer_id, booking_id, payment_id, amount, method, status, payment_type, is_demo)
            VALUES (?, ?, ?, ?, ?, ?, 'advance', ?)
        """, (customer_row["id"], booking_id, payment_id, advance_amount,
              payment_method, advance_status, 1 if demo_mode else 0))
        add_finance_ledger_entry(
            db, booking_id, "customer_advance", advance_amount,
            advance_status.lower(), "demo" if demo_mode else "live",
            payment_id, "Customer booking advance",
        )

        def add_notification(user_type, user_id, title, message):
            if not user_id:
                return
            db.execute("""
                INSERT INTO notifications(user_type,user_id,title,message,is_read,created_at)
                VALUES(?,?,?,?,0,CURRENT_TIMESTAMP)
            """, (user_type, str(user_id), title, message))

        response_window = int(values.get("assignment_response_seconds") or 3600)
        response_window_text = format_response_window(response_window)
        customer_notice = (f"Your booking {booking_id} is confirmed.")
        worker_notice = (f"Booking {booking_id} has been assigned to you. You have {response_window_text} to accept or reject this request. If you do not respond, the system will notify your partner and offer the booking to the next eligible worker. Once accepted, reach the customer's location within 1 hour, or at the later scheduled slot shown in the booking.")
        add_notification("customer", customer_row["id"], "Booking Confirmed", customer_notice)
        add_notification("worker", worker_db_id, "New booking assigned", worker_notice)
        if partner_id:
            partner_notice = f"Worker assigned to booking {booking_id}. The worker has {response_window_text} to respond. If they do not respond, Sivdhi will notify you and reassign the booking to the next eligible worker."
            add_notification("partner", partner_id, "Worker assigned to booking", partner_notice)

        db.commit()

        for key in (
            "selected_service_id", "selected_service_name", "selected_category",
            "selected_category_id", "selected_service_icon", "selected_problem_ids",
            "selected_problems_snapshot", "selected_visit_fee", "selected_full_day_price",
            "booking_description", "booking_date", "booking_time", "payment_verified",
        ):
            session.pop(key, None)
        session["last_booking_id"] = booking_id
        flash("Booking confirmed. ₹50 advance record ho gaya (demo payment: real money charge nahi hua). Baaki amount worker ke pahunchne par due hoga.", "success")
        return redirect(url_for("customer.bookings"))

    except Exception as error:
        try:
            db.rollback()
        except Exception:
            pass
        current_app.logger.exception("Customer booking/payment failed: %s", error)
        flash("Booking save nahi hui. Koi charge record nahi kiya gaya; dobara try karein.", "danger")
        return redirect(url_for("customer.payment"))
    finally:
        db.close()


# =========================================================
# JSON ENDPOINTS FOR THE CUSTOMER FLOW
# =========================================================

# =========================================================
# REMAINING BALANCE — CUSTOMER PAYS ONLY AFTER WORKER ARRIVES
# =========================================================

@customer_bp.route("/booking/<booking_ref>/balance-payment", methods=["POST"])
@customer_login_required
def pay_booking_balance(booking_ref):
    """Record the remaining balance after the assigned worker has arrived.

    In demo/test mode this creates a clearly-labelled simulated receipt only;
    no real money is collected. Live mode fails closed until a payment gateway
    verifies a provider callback. Worker arrival itself never marks the balance
    as paid.
    """
    db = get_db()
    try:
        ensure_customer_schema(db)
        ensure_finance_schema(db)
        customer_row = get_customer(db)
        if not customer_row:
            flash("Customer account not found. Please login again.", "warning")
            return redirect(url_for("customer.login"))

        demo_mode = is_demo_mode()
        db.execute("BEGIN IMMEDIATE")
        booking = db.execute(
            """
            SELECT * FROM bookings
            WHERE customer_id = ?
              AND (CAST(booking_id AS TEXT) = ? OR CAST(id AS TEXT) = ?)
            ORDER BY CASE WHEN CAST(booking_id AS TEXT) = ? THEN 0 ELSE 1 END
            LIMIT 1
            """,
            (customer_row["id"], str(booking_ref), str(booking_ref), str(booking_ref)),
        ).fetchone()

        if not booking:
            db.rollback()
            flash("Booking not found in your account.", "warning")
            return redirect(url_for("customer.bookings"))

        if not booking["worker_arrived_at"]:
            db.rollback()
            flash("Remaining balance tabhi pay karein jab assigned worker location par pahunch kar arrival mark kare.", "warning")
            return redirect(url_for("customer.bookings"))

        booking_status = str(
            booking["booking_status"] or booking["status"] or "pending"
        ).strip().lower()
        if booking_status in {"cancelled", "canceled", "refunded", "failed"}:
            db.rollback()
            flash("This booking is not eligible for balance payment.", "warning")
            return redirect(url_for("customer.bookings"))

        paid_states = {"PAID", "SUCCESS", "DEMO_PAID", "COLLECTED", "COMPLETED"}
        current_balance_state = str(booking["balance_payment_status"] or "DUE").upper()
        if current_balance_state in paid_states:
            db.rollback()
            flash("Remaining balance is already recorded as paid.", "info")
            return redirect(url_for("customer.bookings"))

        total = money(
            booking["total_amount"]
            if booking["total_amount"] is not None
            else booking["amount"]
        )
        advance_paid = money(booking["advance_paid_amount"])
        previous_balance_paid = money(booking["balance_paid_amount"])
        stored_due = money(booking["balance_due_amount"])
        balance_due = stored_due if stored_due > 0 else money(
            max(0.0, total - advance_paid - previous_balance_paid)
        )

        if balance_due <= 0:
            db.rollback()
            flash("There is no remaining balance due for this booking.", "info")
            return redirect(url_for("customer.bookings"))

        # Do not fabricate a live payment success without a verified provider.
        if not demo_mode:
            db.rollback()
            flash(
                "Live balance payment is not enabled yet. No payment was taken. "
                "Sivdhi must connect and verify a real payment gateway before live payments can be accepted.",
                "warning",
            )
            return redirect(url_for("customer.bookings"))

        pay_id = payment_number()
        payment_status = "DEMO_PAID"
        db.execute(
            """
            INSERT INTO customer_payment_records
                (customer_id, booking_id, payment_id, amount, method, status, payment_type, is_demo)
            VALUES (?, ?, ?, ?, 'demo', ?, 'balance', 1)
            """,
            (
                customer_row["id"],
                str(booking["booking_id"]),
                pay_id,
                balance_due,
                payment_status,
            ),
        )
        add_finance_ledger_entry(
            db,
            str(booking["booking_id"]),
            "customer_balance",
            balance_due,
            "demo_paid",
            "demo",
            pay_id,
            "Customer recorded remaining balance after worker arrival; simulated only, no real money collected.",
        )

        cur = db.execute(
            """
            UPDATE bookings
            SET balance_paid_amount = ?,
                balance_due_amount = 0,
                balance_payment_status = 'DEMO_PAID',
                balance_payment_id = ?,
                payment_status = 'PAID_DEMO',
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND customer_id = ? AND worker_arrived_at IS NOT NULL
              AND UPPER(COALESCE(balance_payment_status, 'DUE')) NOT IN
                  ('PAID','SUCCESS','DEMO_PAID','COLLECTED','COMPLETED')
            """,
            (
                money(previous_balance_paid + balance_due),
                pay_id,
                booking["id"],
                customer_row["id"],
            ),
        )
        if cur.rowcount != 1:
            db.rollback()
            flash("Payment state changed. Refresh your bookings and check the balance status.", "warning")
            return redirect(url_for("customer.bookings"))

        worker_id = booking["worker_id"] or booking["assigned_worker_id"]
        if worker_id:
            db.execute(
                """
                INSERT INTO notifications (user_type, user_id, title, message, is_read, created_at)
                VALUES ('worker', ?, ?, ?, 0, CURRENT_TIMESTAMP)
                """,
                (
                    str(worker_id),
                    "Customer Balance Recorded (Demo)",
                    f"Customer recorded ₹{balance_due:.2f} as a simulated remaining-balance payment for booking {booking['booking_id']}. No real money was collected. You may start the service after confirming with the customer.",
                ),
            )

        db.execute(
            """
            INSERT INTO notifications (user_type, user_id, title, message, is_read, created_at)
            VALUES ('customer', ?, ?, ?, 0, CURRENT_TIMESTAMP)
            """,
            (
                str(customer_row["id"]),
                "Remaining Balance Recorded (Demo)",
                f"₹{balance_due:.2f} was recorded in demo mode for booking {booking['booking_id']}. This is a simulation; no real money was collected.",
            ),
        )
        partner_id = booking["assigned_partner_id"] or booking["partner_id"]
        if not partner_id and worker_id:
            partner_id = find_partner_for_worker(db, int(worker_id))
        if partner_id:
            db.execute(
                "INSERT INTO notifications(user_type,user_id,title,message,is_read,created_at) VALUES('partner',?,'Customer Balance Recorded (Demo)',?,0,CURRENT_TIMESTAMP)",
                (str(partner_id), f"Customer recorded ₹{balance_due:.2f} as a simulated balance payment for booking {booking['booking_id']}; no real money was collected."),
            )

        db.commit()
        flash(
            f"₹{balance_due:.2f} remaining balance demo record ho gaya. Dhyan dein: demo mode mein real payment nahi hua.",
            "success",
        )
        return redirect(url_for("customer.bookings"))

    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        current_app.logger.exception("Customer balance payment failed")
        flash("Remaining balance record nahi ho saka. Status check karke dobara try karein.", "danger")
        return redirect(url_for("customer.bookings"))
    finally:
        db.close()


@customer_bp.route("/api/category-services")
@customer_login_required
def customer_category_services_api():
    category_name = (
        request.args.get("category") or ""
    ).strip()

    mode = normalise_booking_mode(
        request.args.get("mode")
        or session.get("booking_mode")
        or "normal"
    )

    db = get_db()

    try:
        ensure_customer_schema(db)

        latitude, longitude = customer_saved_coordinates(db)

        if latitude is None or longitude is None:
            return jsonify({
                "success": False,
                "requires_location": True,
                "services": [],
            }), 400

        permitted = {
            str(category["name"]).lower()
            for category in list_customer_categories(
                db, latitude, longitude, mode
            )
        }

        if category_name.lower() not in permitted:
            return jsonify({
                "success": False,
                "services": [],
                "message": "Category unavailable in this area.",
            }), 404

        return jsonify({
            "success": True,
            "services": customer_service_rows(
                db, category_name
            ),
        })

    finally:
        db.close()


@customer_bp.route("/api/service-problems")
@customer_login_required
def customer_service_problems_api():
    service_id = safe_int(
        request.args.get("service_id"), 0
    )

    db = get_db()

    try:
        ensure_customer_schema(db)

        service_data = get_service(db, service_id)

        if not service_data:
            return jsonify({
                "success": False,
                "problems": [],
                "message": "Service not found.",
            }), 404

        latitude, longitude = customer_saved_coordinates(db)

        permitted = set()

        if latitude is not None and longitude is not None:
            permitted = {
                str(category["name"]).lower()
                for category in list_customer_categories(
                    db, latitude, longitude, "normal"
                )
            }

        if str(
            service_data.get("display_category", "")
        ).lower() not in permitted:
            return jsonify({
                "success": False,
                "problems": [],
                "message": "Service unavailable in this area.",
            }), 404

        return jsonify({
            "success": True,
            "service": service_data["display_name"],
            "problems": get_problem_rows(db, service_id),
        })

    finally:
        db.close()


# =========================================================
# INITIALIZATION
# =========================================================

@customer_bp.before_app_request
def customer_blueprint_setup():
    """
    Run safe, additive schema setup before a customer request.
    Existing data is not deleted or reset.
    """

    if not request.path.startswith("/customer"):
        return

    db = None

    try:
        db = get_db()
        ensure_customer_schema(db)

    except Exception:
        current_app.logger.exception(
            "Sivdhi customer schema initialization failed"
        )

    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass# =========================================================
# CUSTOMER BOOKINGS PAGE
# =========================================================

@customer_bp.route("/booking/<booking_ref>/cancel", methods=["POST"])
@customer_login_required
def cancel_booking(booking_ref):
    db = get_db()
    try:
        ensure_customer_schema(db)
        ensure_finance_schema(db)
        customer_row = get_customer(db)
        if not customer_row:
            flash("Please login again.", "warning")
            return redirect(url_for("customer.login"))
        db.execute("BEGIN IMMEDIATE")
        booking = db.execute("""
            SELECT * FROM bookings
            WHERE CAST(booking_id AS TEXT)=CAST(? AS TEXT)
              AND customer_id=? LIMIT 1
        """, (booking_ref, customer_row["id"])).fetchone()
        if not booking:
            db.rollback()
            flash("Booking not found.", "danger")
            return redirect(url_for("customer.bookings"))
        status = str(booking["booking_status"] or booking["status"] or "pending").lower()
        assigned = booking["worker_id"] or booking["assigned_worker_id"]
        if assigned or status not in {"pending", "matching", "no_worker", "unassigned"}:
            db.rollback()
            flash("Worker assign ho chuka hai, isliye app se booking cancel nahi ki ja sakti.", "warning")
            return redirect(url_for("customer.bookings"))
        db.execute("""
            UPDATE bookings SET booking_status='cancelled', status='cancelled',
                cancellation_reason='Cancelled by customer before assignment',
                updated_at=CURRENT_TIMESTAMP WHERE id=?
        """, (booking["id"],))
        if float(booking["advance_paid_amount"] or 0) > 0:
            refund_id = payment_number()
            db.execute("""
                INSERT INTO customer_payment_records
                    (customer_id, booking_id, payment_id, amount, method, status, payment_type, is_demo)
                VALUES (?, ?, ?, ?, 'demo', 'REFUNDED', 'refund', 1)
            """, (customer_row["id"], booking["booking_id"], refund_id, float(booking["advance_paid_amount"] or 0)))
            add_finance_ledger_entry(db, booking["booking_id"], "customer_refund", float(booking["advance_paid_amount"] or 0), "refunded", "demo", refund_id, "Advance refunded after pre-assignment cancellation")
            db.execute("UPDATE bookings SET advance_payment_status='REFUNDED', payment_status='REFUNDED' WHERE id=?", (booking["id"],))
        db.commit()
        flash("Booking cancelled before worker assignment.", "success")
        return redirect(url_for("customer.bookings"))
    except Exception as error:
        db.rollback()
        current_app.logger.exception("Customer booking cancellation failed: %s", error)
        flash("Booking could not be cancelled.", "danger")
        return redirect(url_for("customer.bookings"))
    finally:
        db.close()


@customer_bp.route("/bookings")
@customer_login_required
def bookings():
    db = get_db()

    try:
        ensure_customer_schema(db)
        ensure_finance_schema(db)

        customer_row = get_customer(db)

        if not customer_row:
            session.clear()
            flash("Please login again.", "warning")
            return redirect(url_for("customer.login"))

        booking_rows = []

        if table_exists(db, "bookings"):
            columns = table_columns(db, "bookings")

            # Only display bookings connected to this customer.
            customer_column = first_existing(
                columns,
                "customer_id",
                "customer_code",
                "user_id",
            )

            if customer_column:
                values = [customer_row["id"]]

                if (
                    customer_column == "customer_code"
                    and "customer_id" in customer_row.keys()
                ):
                    values = [customer_row["customer_id"]]

                sql = (
                    f'SELECT * FROM bookings '
                    f'WHERE "{customer_column}" = ? '
                )

                if "created_at" in columns:
                    sql += " ORDER BY created_at DESC"
                elif "id" in columns:
                    sql += " ORDER BY id DESC"

                booking_rows = db.execute(
                    sql, values
                ).fetchall()

        result = []

        for row in booking_rows:
            item = row_dict(row)
            columns = set(item.keys())

            # Resolve the display name from the existing service record.
            display_name = first_existing(
                columns,
                "service_name",
                "service",
                "category",
            )

            service_name = (
                item.get(display_name)
                if display_name
                else None
            )

            service_id = safe_int(item.get("service_id"), 0)

            if not service_name and service_id:
                service_row = get_service(db, service_id)

                if service_row:
                    service_name = service_row.get("display_name")

            item["service_name"] = (
                service_name or "Home Service"
            )

            item["booking_id"] = (
                item.get("booking_id")
                or item.get("id")
                or "—"
            )

            item["booking_status"] = (
                item.get("booking_status")
                or item.get("status")
                or "Pending"
            )

            item["payment_status"] = (
                item.get("payment_status")
                or "Not available"
            )

            item["booking_type"] = normalise_booking_mode(
                item.get("booking_type")
                or item.get("booking_mode")
                or "normal"
            )
            item["booking_date"] = item.get("booking_date") or item.get("preferred_date") or ""
            item["booking_time"] = item.get("booking_time") or item.get("preferred_time") or ""
            item["advance_amount"] = money(item.get("advance_amount"))
            item["advance_paid_amount"] = money(item.get("advance_paid_amount"))
            item["balance_due_amount"] = money(item.get("balance_due_amount"))
            item["balance_paid_amount"] = money(item.get("balance_paid_amount"))
            item["balance_payment_status"] = item.get("balance_payment_status") or "DUE"
            item["worker_arrived_at"] = item.get("worker_arrived_at") or None
            current_status = str(item.get("booking_status") or item.get("status") or "pending").lower()
            # Customer sees only the four approved lifecycle labels; never internal retries/rejections.
            if current_status in {"completed", "complete"}:
                item["customer_status_label"] = "Booking Completed"
            elif current_status in {"in_progress", "started"}:
                item["customer_status_label"] = "Booking Started"
            elif item.get("worker_arrived_at"):
                item["customer_status_label"] = "Worker Arrived"
            elif current_status in {"accepted", "assigned", "pending", "matching", "unassigned", "no_worker"}:
                item["customer_status_label"] = "Booking Confirmed"
            else:
                item["customer_status_label"] = "Booking Cancelled" if current_status in {"cancelled", "canceled", "failed", "refunded"} else "Booking Confirmed"
            is_assigned = bool(item.get("worker_id") or item.get("assigned_worker_id"))
            item["can_cancel"] = (not is_assigned and current_status in {"pending", "matching", "no_worker", "unassigned"})

            item["total_amount"] = money(
                item.get("total_amount")
                if item.get("total_amount") is not None
                else item.get("amount")
                if item.get("amount") is not None
                else item.get("service_charge")
            )

            result.append(item)

        return render_template(
            "customer/bookings.html",
            bookings=result,
            demo_mode=is_demo_mode(),
        )

    finally:
        db.close()


# =========================================================
# CUSTOMER PROFILE PAGE
# =========================================================

@customer_bp.route("/profile")
@customer_login_required
def profile():
    db = get_db()

    try:
        ensure_customer_schema(db)

        customer_row = get_customer(db)

        if not customer_row:
            session.clear()
            flash("Please login again.", "warning")
            return redirect(url_for("customer.login"))

        return render_template(
            "customer/profile.html",
            customer=customer_row,
        )

    finally:
        db.close()

# =========================================================
# CUSTOMER ACCOUNT FEATURES (separate pages, partner-style menu)
# =========================================================

@customer_bp.route("/edit-profile", methods=["GET", "POST"])
@customer_login_required
def edit_profile():
    db = get_db()
    try:
        ensure_customer_schema(db)
        customer_row = get_customer(db)
        if not customer_row:
            session.clear()
            flash("Please login again.", "warning")
            return redirect(url_for("customer.login"))

        if request.method == "POST":
            fullname = (request.form.get("fullname") or "").strip()
            mobile = normalize_mobile(request.form.get("mobile"))
            email = normalize_email(request.form.get("email"))
            address = (request.form.get("address") or "").strip()
            city = (request.form.get("city") or "").strip()
            state = (request.form.get("state") or "").strip()
            pincode = re.sub(r"\D", "", request.form.get("pincode") or "")

            if not fullname or len(fullname) > 100:
                flash("Enter a valid name (up to 100 characters).", "danger")
                return redirect(url_for("customer.edit_profile"))
            if len(mobile) != 10 or not mobile.isdigit():
                flash("Enter a valid 10-digit mobile number.", "danger")
                return redirect(url_for("customer.edit_profile"))
            if "@" not in email or "." not in email.rsplit("@", 1)[-1] or len(email) > 254:
                flash("Enter a valid email address.", "danger")
                return redirect(url_for("customer.edit_profile"))
            if pincode and len(pincode) != 6:
                flash("PIN code must contain 6 digits.", "danger")
                return redirect(url_for("customer.edit_profile"))

            duplicate = db.execute(
                "SELECT id FROM customers WHERE id<>? AND (mobile=? OR LOWER(email)=LOWER(?)) LIMIT 1",
                (customer_row["id"], mobile, email),
            ).fetchone()
            if duplicate:
                flash("That mobile number or email is already used by another account.", "danger")
                return redirect(url_for("customer.edit_profile"))

            columns = set(table_columns(db, "customers"))
            updates = {"fullname": fullname, "mobile": mobile, "email": email}
            for key, value in (("address", address), ("city", city), ("state", state), ("pincode", pincode)):
                if key in columns:
                    updates[key] = value

            photo = request.files.get("profile_photo")
            if photo and photo.filename:
                original = os.path.basename(photo.filename)
                ext = original.rsplit(".", 1)[-1].lower() if "." in original else ""
                allowed = {"jpg", "jpeg", "png", "webp"}
                if ext not in allowed:
                    flash("Profile photo must be JPG, PNG or WEBP.", "danger")
                    return redirect(url_for("customer.edit_profile"))
                try:
                    photo.stream.seek(0, os.SEEK_END)
                    size = photo.stream.tell()
                    photo.stream.seek(0)
                    header = photo.stream.read(12)
                    photo.stream.seek(0)
                except Exception:
                    flash("Could not read the uploaded profile photo.", "danger")
                    return redirect(url_for("customer.edit_profile"))
                is_jpeg = header.startswith(b"\xff\xd8\xff")
                is_png = header.startswith(b"\x89PNG\r\n\x1a\n")
                is_webp = header[:4] == b"RIFF" and header[8:12] == b"WEBP"
                signature_matches = ((ext in {"jpg", "jpeg"} and is_jpeg) or (ext == "png" and is_png) or (ext == "webp" and is_webp))
                if size <= 0 or size > 5 * 1024 * 1024:
                    flash("Profile photo must be greater than 0 bytes and 5 MB or smaller.", "danger")
                    return redirect(url_for("customer.edit_profile"))
                if not signature_matches:
                    flash("The image contents do not match its file extension.", "danger")
                    return redirect(url_for("customer.edit_profile"))
                if "profile_photo" not in columns:
                    flash("Profile photo uploads are not enabled for this account database.", "danger")
                    return redirect(url_for("customer.edit_profile"))
                upload_dir = os.path.join(current_app.root_path, "static", "uploads")
                os.makedirs(upload_dir, exist_ok=True)
                filename = "customer_" + str(customer_row["id"]) + "_" + secrets.token_hex(8) + "." + ext
                try:
                    photo.save(os.path.join(upload_dir, filename))
                except OSError:
                    current_app.logger.exception("Could not save customer profile photo")
                    flash("Profile photo could not be saved. Please try again.", "danger")
                    return redirect(url_for("customer.edit_profile"))
                updates["profile_photo"] = filename

            set_sql = ", ".join(f'"{column}"=?' for column in updates)
            db.execute(
                f"UPDATE customers SET {set_sql} WHERE id=?",
                [*updates.values(), customer_row["id"]],
            )
            db.commit()
            session["customer_name"] = fullname
            session["customer_email"] = email
            session["customer_mobile"] = mobile
            flash("Profile updated successfully.", "success")
            return redirect(url_for("customer.profile"))

        return render_template("customer/edit_profile.html", customer=customer_row)
    finally:
        db.close()


CUSTOMER_INDIAN_BANKS = [
    "State Bank of India", "HDFC Bank", "ICICI Bank", "Axis Bank", "Kotak Mahindra Bank",
    "IndusInd Bank", "Yes Bank", "Punjab National Bank", "Bank of Baroda", "Canara Bank",
    "Union Bank of India", "Bank of India", "Indian Bank", "Central Bank of India",
    "Indian Overseas Bank", "UCO Bank", "Bank of Maharashtra", "Punjab & Sind Bank", "IDBI Bank",
    "Federal Bank", "South Indian Bank", "Bandhan Bank", "RBL Bank", "AU Small Finance Bank",
    "Equitas Small Finance Bank", "ESAF Small Finance Bank", "Jana Small Finance Bank",
    "Ujjivan Small Finance Bank", "Karnataka Bank", "Karur Vysya Bank", "Tamilnad Mercantile Bank",
    "DCB Bank", "City Union Bank", "Dhanlaxmi Bank", "CSB Bank", "Jammu & Kashmir Bank",
]


def _valid_customer_bank_details(bank_name, account_holder, account_number, ifsc):
    bank_name = (bank_name or "").strip()
    account_holder = (account_holder or "").strip()
    account_number = (account_number or "").strip()
    ifsc = (ifsc or "").strip().upper()
    if not bank_name or len(bank_name) > 100:
        return False, "Enter a valid bank name."
    if not account_holder or len(account_holder) > 100:
        return False, "Enter a valid account holder name."
    if not re.fullmatch(r"\d{9,18}", account_number):
        return False, "Account number must contain 9 to 18 digits."
    if not re.fullmatch(r"[A-Z]{4}0[A-Z0-9]{6}", ifsc):
        return False, "Please enter a valid IFSC code."
    return True, ""


@customer_bp.route("/bank", methods=["GET", "POST"])
@customer_login_required
def bank():
    if request.method == "POST":
        return change_bank()

    db = get_db()
    try:
        ensure_customer_schema(db)
        customer_row = get_customer(db)
        if not customer_row:
            session.clear()
            flash("Please login again.", "warning")
            return redirect(url_for("customer.login"))
        stored = dict(customer_row)
        number = str(stored.get("account_number") or "")
        stored["masked_account_number"] = ("•" * max(0, len(number)-4) + number[-4:]) if number else ""
        return render_template("customer/bank.html", customer=stored, indian_banks=CUSTOMER_INDIAN_BANKS)
    finally:
        db.close()


@customer_bp.route("/change-bank", methods=["POST"])
@customer_login_required
def change_bank():
    db = get_db()
    try:
        ensure_customer_schema(db)
        customer_row = get_customer(db)
    finally:
        db.close()
    if not customer_row:
        session.clear()
        flash("Please login again.", "warning")
        return redirect(url_for("customer.login"))

    bank_name = (request.form.get("bank_name") or "").strip()
    account_holder = (request.form.get("account_holder") or "").strip()
    account_number = re.sub(r"\s+", "", request.form.get("account_number") or "")
    ifsc = (request.form.get("ifsc") or request.form.get("ifsc_code") or "").strip().upper()
    confirmed_account_number = re.sub(r"\s+", "", request.form.get("confirm_account_number") or "")
    if confirmed_account_number and confirmed_account_number != account_number:
        flash("Account numbers do not match.", "danger")
        return redirect(url_for("customer.bank"))
    branch_name = (request.form.get("branch_name") or "").strip()[:100]
    account_type = (request.form.get("account_type") or "").strip()
    if account_type not in {"", "Savings", "Current"}:
        flash("Choose a valid account type.", "danger")
        return redirect(url_for("customer.bank"))
    valid, message = _valid_customer_bank_details(bank_name, account_holder, account_number, ifsc)
    if not valid:
        flash(message, "danger")
        return redirect(url_for("customer.bank"))

    customer_id = str(customer_row["id"])
    allowed, retry_after = reserve_otp_request("customer_bank_change", customer_id, request.remote_addr or "unknown")
    if not allowed:
        flash(f"Please wait {retry_after} seconds before requesting another OTP.", "warning")
        return redirect(url_for("customer.bank"))

    otp = str(secrets.randbelow(900000) + 100000)
    token = issue_pending_change(
        "customer", customer_id, "bank_change",
        {
            "bank_name": bank_name,
            "account_holder": account_holder,
            "account_number": account_number,
            "ifsc": ifsc,
            "branch_name": branch_name,
            "account_type": account_type,
        }, otp,
    )
    session["pending_customer_bank_token"] = token
    try:
        send_otp(customer_row["email"], otp)
    except Exception:
        delete_pending_change("customer", customer_id, "bank_change", token)
        session.pop("pending_customer_bank_token", None)
        current_app.logger.exception("Could not send customer bank verification OTP")
        flash("OTP could not be sent. Please try again later.", "danger")
        return redirect(url_for("customer.bank"))
    flash("Verification OTP sent to your registered email.", "success")
    return redirect(url_for("customer.verify_bank_otp"))


@customer_bp.route("/verify-bank-otp", methods=["GET", "POST"])
@customer_login_required
def verify_bank_otp():
    customer_id = session.get("customer_id")
    token = session.get("pending_customer_bank_token")
    if not customer_id or not token:
        flash("Please start the bank detail update again.", "warning")
        return redirect(url_for("customer.bank"))
    if request.method == "POST":
        status, bank_data, attempts = verify_pending_change(
            "customer", str(customer_id), "bank_change", token, request.form.get("otp", "").strip()
        )
        if status != "ok":
            if status in {"missing", "expired", "locked", "invalid_state"}:
                session.pop("pending_customer_bank_token", None)
                flash("OTP expired or too many attempts. Please submit the bank details again.", "danger")
                return redirect(url_for("customer.bank"))
            flash(f"Invalid OTP. Attempt {attempts} of 5.", "danger")
            return redirect(url_for("customer.verify_bank_otp"))
        session.pop("pending_customer_bank_token", None)
        db = get_db()
        try:
            db.execute(
                "UPDATE customers SET bank_name=?,account_holder=?,account_number=?,ifsc=?,branch_name=?,account_type=?,bank_verified=1 WHERE id=?",
                (
                    bank_data["bank_name"], bank_data["account_holder"],
                    bank_data["account_number"], bank_data["ifsc"],
                    bank_data.get("branch_name", ""), bank_data.get("account_type", ""),
                    customer_id,
                ),
            )
            db.commit()
        except Exception:
            db.rollback()
            current_app.logger.exception("Customer bank detail update failed")
            flash("Bank details could not be updated. Please try again.", "danger")
            return redirect(url_for("customer.bank"))
        finally:
            db.close()
        flash("Bank details verified and saved successfully.", "success")
        return redirect(url_for("customer.bank"))
    return render_template("customer/verify_bank_otp.html")


@customer_bp.route("/resend-bank-otp", methods=["POST"])
@customer_login_required
def resend_bank_otp():
    customer_id = session.get("customer_id")
    token = session.get("pending_customer_bank_token")
    if not customer_id or not token:
        flash("Please start the bank detail update again.", "warning")
        return redirect(url_for("customer.bank"))
    allowed, retry_after = reserve_otp_request("customer_bank_change", str(customer_id), request.remote_addr or "unknown")
    if not allowed:
        flash(f"Please wait {retry_after} seconds before requesting another OTP.", "warning")
        return redirect(url_for("customer.verify_bank_otp"))
    db = get_db()
    try:
        customer_row = get_customer(db)
    finally:
        db.close()
    if not customer_row:
        session.pop("pending_customer_bank_token", None)
        flash("Please login again.", "warning")
        return redirect(url_for("customer.login"))
    otp = str(secrets.randbelow(900000) + 100000)
    status = refresh_pending_change("customer", str(customer_id), "bank_change", token, otp)
    if status != "ok":
        session.pop("pending_customer_bank_token", None)
        flash("Bank update request expired. Please start again.", "warning")
        return redirect(url_for("customer.bank"))
    try:
        send_otp(customer_row["email"], otp)
    except Exception:
        current_app.logger.exception("Could not resend customer bank OTP")
        flash("OTP could not be resent. Please try again later.", "danger")
        return redirect(url_for("customer.verify_bank_otp"))
    flash("A new OTP was sent to your email.", "success")
    return redirect(url_for("customer.verify_bank_otp"))


@customer_bp.route("/settings", methods=["GET", "POST"])
@customer_login_required
def settings():
    db = get_db()
    try:
        ensure_customer_schema(db)
        customer_row = get_customer(db)
        if not customer_row:
            session.clear()
            flash("Please login again.", "warning")
            return redirect(url_for("customer.login"))

        if request.method == "POST":
            language_value = (request.form.get("language") or "en").strip().lower()
            if language_value not in {"en", "hi"}:
                language_value = "en"
            notifications_enabled = 1 if request.form.get("notifications_enabled") else 0
            updates = {
                "notifications_enabled": notifications_enabled,
                "email_notification": notifications_enabled,
                "booking_notifications": 1 if request.form.get("booking_notifications") else 0,
                "promotional_notifications": 1 if request.form.get("promotional_notifications") else 0,
                "language": language_value,
            }
            assignments = ", ".join(f'"{key}"=?' for key in updates)
            db.execute(
                f"UPDATE customers SET {assignments} WHERE id=?",
                [*updates.values(), customer_row["id"]],
            )
            db.commit()
            session["customer_language"] = language_value
            flash("Settings saved successfully.", "success")
            return redirect(url_for("customer.settings"))

        return render_template("customer/settings.html", customer=customer_row)
    finally:
        db.close()


@customer_bp.route("/help", methods=["GET", "POST"])
@customer_login_required
def help():
    db = get_db()
    try:
        ensure_customer_schema(db)
        customer_row = get_customer(db)
        if not customer_row:
            session.clear()
            flash("Please login again.", "warning")
            return redirect(url_for("customer.login"))

        if request.method == "POST":
            allowed_categories = {
                "booking", "worker_assignment", "payment", "cancellation",
                "account", "technical", "other",
            }
            category = (request.form.get("support_category") or "").strip()
            booking_id = (request.form.get("booking_id") or "").strip()[:50]
            subject = (request.form.get("subject") or "").strip()[:120]
            message = (request.form.get("message") or "").strip()[:2000]
            if category not in allowed_categories:
                flash("Choose a valid support topic.", "danger")
                return redirect(url_for("customer.help"))
            if len(subject) < 4 or len(message) < 10:
                flash("Enter a subject and a description of at least 10 characters.", "danger")
                return redirect(url_for("customer.help"))
            db.execute(
                "INSERT INTO customer_support_tickets (customer_id,category,booking_id,subject,message) VALUES (?,?,?,?,?)",
                (customer_row["id"], category, booking_id or None, subject, message),
            )
            db.commit()
            flash("Your support request has been submitted.", "success")
            return redirect(url_for("customer.help"))

        return render_template("customer/help.html")
    finally:
        db.close()


@customer_bp.route("/faq")
@customer_login_required
def faq():
    return render_template("customer/faq.html")


@customer_bp.route("/privacy")
@customer_login_required
def privacy():
    return render_template("customer/privacy.html")


@customer_bp.route("/terms")
@customer_login_required
def terms():
    return render_template("customer/terms.html")


@customer_bp.route("/about")
@customer_login_required
def about():
    return render_template("customer/about.html")


@customer_bp.route("/language", methods=["GET", "POST"])
@customer_login_required
def language():
    db = get_db()
    try:
        ensure_customer_schema(db)
        customer_row = get_customer(db)
        if not customer_row:
            session.clear()
            flash("Please login again.", "warning")
            return redirect(url_for("customer.login"))

        if request.method == "POST":
            selected = (request.form.get("language") or "en").strip().lower()
            if selected not in {"en", "hi"}:
                flash("Choose a supported language.", "danger")
                return redirect(url_for("customer.language"))
            db.execute("UPDATE customers SET language=? WHERE id=?", (selected, customer_row["id"]))
            db.commit()
            session["customer_language"] = selected
            flash("Language preference saved.", "success")
            return redirect(url_for("customer.language"))

        selected = customer_row["language"] if "language" in customer_row.keys() else "en"
        return render_template("customer/language.html", customer=customer_row, selected_language=selected)
    finally:
        db.close()


@customer_bp.route("/change-password", methods=["GET", "POST"])
@customer_login_required
def change_password():
    db = get_db()
    try:
        ensure_customer_schema(db)
        customer_row = get_customer(db)
        if not customer_row:
            session.clear()
            flash("Please login again.", "warning")
            return redirect(url_for("customer.login"))

        if request.method == "POST":
            current_password = request.form.get("current_password") or ""
            new_password = request.form.get("new_password") or ""
            confirm_password = request.form.get("confirm_password") or ""
            stored_hash = customer_row["password"] if "password" in customer_row.keys() else ""
            if not stored_hash or not check_password_hash(stored_hash, current_password):
                flash("Current password is incorrect.", "danger")
                return redirect(url_for("customer.change_password"))
            if len(new_password) < 8 or not re.search(r"[A-Za-z]", new_password) or not re.search(r"\d", new_password):
                flash("New password must be at least 8 characters and include a letter and a number.", "danger")
                return redirect(url_for("customer.change_password"))
            if new_password != confirm_password:
                flash("New passwords do not match.", "danger")
                return redirect(url_for("customer.change_password"))

            customer_id = str(customer_row["id"])
            allowed, retry_after = reserve_otp_request(
                "customer_password_change", customer_id, request.remote_addr or "unknown"
            )
            if not allowed:
                flash(f"Please wait {retry_after} seconds before requesting another OTP.", "warning")
                return redirect(url_for("customer.change_password"))

            otp = str(secrets.randbelow(900000) + 100000)
            token = issue_pending_change(
                "customer", customer_id, "password_change",
                {"password_hash": generate_password_hash(new_password)}, otp,
            )
            session["pending_customer_password_token"] = token
            try:
                send_otp(customer_row["email"], otp)
            except Exception:
                delete_pending_change("customer", customer_id, "password_change", token)
                session.pop("pending_customer_password_token", None)
                current_app.logger.exception("Could not send customer password-change OTP")
                flash("OTP could not be sent. Please try again later.", "danger")
                return redirect(url_for("customer.change_password"))

            flash("OTP sent to your registered email.", "success")
            return redirect(url_for("customer.verify_password_otp"))

        return render_template("customer/change_password.html")
    finally:
        db.close()


@customer_bp.route("/verify-password-otp", methods=["GET", "POST"])
@customer_login_required
def verify_password_otp():
    customer_id = session.get("customer_id")
    token = session.get("pending_customer_password_token")
    if not customer_id or not token:
        flash("Please start the password change again.", "warning")
        return redirect(url_for("customer.change_password"))

    if request.method == "POST":
        status, payload, attempts = verify_pending_change(
            "customer", str(customer_id), "password_change", token,
            request.form.get("otp", "").strip(),
        )
        if status != "ok":
            if status in {"missing", "expired", "locked", "invalid_state"}:
                session.pop("pending_customer_password_token", None)
                flash("OTP expired or too many attempts. Please start the password change again.", "danger")
                return redirect(url_for("customer.change_password"))
            flash(f"Invalid OTP. Attempt {attempts} of 5.", "danger")
            return redirect(url_for("customer.verify_password_otp"))

        session.pop("pending_customer_password_token", None)
        db = get_db()
        try:
            cur = db.execute(
                "UPDATE customers SET password=? WHERE id=?",
                (payload["password_hash"], customer_id),
            )
            if cur.rowcount != 1:
                db.rollback()
                flash("Customer account could not be found.", "danger")
                return redirect(url_for("customer.change_password"))
            db.commit()
        except Exception:
            db.rollback()
            current_app.logger.exception("Customer password update failed")
            flash("Password could not be updated. Please try again.", "danger")
            return redirect(url_for("customer.change_password"))
        finally:
            db.close()

        flash("Password changed successfully.", "success")
        return redirect(url_for("customer.settings"))

    return render_template("customer/verify_password_otp.html")


@customer_bp.route("/resend-password-otp", methods=["POST"])
@customer_login_required
def resend_password_otp():
    customer_id = session.get("customer_id")
    token = session.get("pending_customer_password_token")
    if not customer_id or not token:
        flash("Please start the password change again.", "warning")
        return redirect(url_for("customer.change_password"))

    allowed, retry_after = reserve_otp_request(
        "customer_password_change", str(customer_id), request.remote_addr or "unknown"
    )
    if not allowed:
        flash(f"Please wait {retry_after} seconds before requesting another OTP.", "warning")
        return redirect(url_for("customer.verify_password_otp"))

    db = get_db()
    try:
        customer_row = get_customer(db)
    finally:
        db.close()
    if not customer_row:
        session.pop("pending_customer_password_token", None)
        flash("Please login again.", "warning")
        return redirect(url_for("customer.login"))

    otp = str(secrets.randbelow(900000) + 100000)
    status = refresh_pending_change("customer", str(customer_id), "password_change", token, otp)
    if status != "ok":
        session.pop("pending_customer_password_token", None)
        flash("Password-change request expired. Please start again.", "warning")
        return redirect(url_for("customer.change_password"))
    try:
        send_otp(customer_row["email"], otp)
    except Exception:
        current_app.logger.exception("Could not resend customer password-change OTP")
        flash("OTP could not be resent. Please try again later.", "danger")
        return redirect(url_for("customer.verify_password_otp"))
    flash("A new OTP was sent to your email.", "success")
    return redirect(url_for("customer.verify_password_otp"))


@customer_bp.route("/api/notifications", methods=["GET"])
@customer_login_required
def customer_notifications_api():
    """Return the signed-in customer's recent notifications and unread count."""
    db = get_db()
    try:
        ensure_customer_schema(db)
        customer_row = get_customer(db)
        if not customer_row:
            return jsonify({"success": False, "message": "Please login again."}), 401

        if not table_exists(db, "notifications"):
            return jsonify({"success": True, "notifications": [], "unread_count": 0})

        identifiers = [str(customer_row["id"])]
        if "customer_id" in customer_row.keys() and customer_row["customer_id"]:
            identifiers.append(str(customer_row["customer_id"]))
        placeholders = ",".join("?" for _ in identifiers)
        rows = db.execute(
            f"SELECT id,title,message,is_read,created_at FROM notifications "
            f"WHERE LOWER(COALESCE(user_type,''))='customer' "
            f"AND CAST(user_id AS TEXT) IN ({placeholders}) "
            "ORDER BY id DESC LIMIT 30",
            identifiers,
        ).fetchall()
        unread_count = sum(1 for row in rows if not int(row["is_read"] or 0))
        return jsonify({
            "success": True,
            "unread_count": unread_count,
            "notifications": [
                {
                    "id": row["id"],
                    "title": row["title"] or "Sivdhi update",
                    "message": row["message"] or "",
                    "is_read": bool(row["is_read"]),
                    "created_at": str(row["created_at"] or ""),
                }
                for row in rows
            ],
        })
    finally:
        db.close()


@customer_bp.route("/notifications", methods=["GET"])
@customer_login_required
def notifications():
    db = get_db()
    try:
        ensure_customer_schema(db)
        customer_row = get_customer(db)
        if not customer_row:
            session.clear()
            flash("Please login again.", "warning")
            return redirect(url_for("customer.login"))
        if not table_exists(db, "notifications"):
            items = []
        else:
            identifiers = [str(customer_row["id"])]
            if "customer_id" in customer_row.keys() and customer_row["customer_id"]:
                identifiers.append(str(customer_row["customer_id"]))

            open_id = request.args.get("open_id", type=int)
            placeholders = ",".join("?" for _ in identifiers)

            if open_id:
                db.execute(
                    f"UPDATE notifications SET is_read=1 "
                    f"WHERE id=? AND LOWER(COALESCE(user_type,''))='customer' "
                    f"AND CAST(user_id AS TEXT) IN ({placeholders})",
                    [open_id, *identifiers],
                )
                db.commit()

            items = db.execute(
                f"SELECT id,title,message,is_read,created_at FROM notifications WHERE LOWER(COALESCE(user_type,''))='customer' AND CAST(user_id AS TEXT) IN ({placeholders}) ORDER BY id DESC LIMIT 50",
                identifiers,
            ).fetchall()
        return render_template("customer/notifications.html", notifications=items)
    finally:
        db.close()


@customer_bp.route("/notification/<int:notification_id>/read", methods=["POST"])
@customer_login_required
def mark_notification_read(notification_id):
    db = get_db()
    try:
        customer_row = get_customer(db)
        if not customer_row or not table_exists(db, "notifications"):
            flash("Notification could not be updated.", "warning")
            return redirect(url_for("customer.notifications"))
        identifiers = [str(customer_row["id"])]
        if "customer_id" in customer_row.keys() and customer_row["customer_id"]:
            identifiers.append(str(customer_row["customer_id"]))
        placeholders = ",".join("?" for _ in identifiers)
        db.execute(
            f"UPDATE notifications SET is_read=1 WHERE id=? AND LOWER(COALESCE(user_type,''))='customer' AND CAST(user_id AS TEXT) IN ({placeholders})",
            [notification_id, *identifiers],
        )
        db.commit()
        return redirect(url_for("customer.notifications"))
    finally:
        db.close()


@customer_bp.route("/notifications/mark-all-read", methods=["POST"])
@customer_login_required
def mark_all_notifications_read():
    db = get_db()
    try:
        customer_row = get_customer(db)
        if not customer_row or not table_exists(db, "notifications"):
            flash("Notifications could not be updated.", "warning")
            return redirect(url_for("customer.notifications"))
        identifiers = [str(customer_row["id"])]
        if "customer_id" in customer_row.keys() and customer_row["customer_id"]:
            identifiers.append(str(customer_row["customer_id"]))
        placeholders = ",".join("?" for _ in identifiers)
        db.execute(
            f"UPDATE notifications SET is_read=1 WHERE LOWER(COALESCE(user_type,''))='customer' AND CAST(user_id AS TEXT) IN ({placeholders})",
            identifiers,
        )
        db.commit()
        flash("All notifications marked as read.", "success")
        return redirect(url_for("customer.notifications"))
    finally:
        db.close()


@customer_bp.route("/rate-app", methods=["GET", "POST"])
@customer_login_required
def rate_app():
    db = get_db()
    try:
        customer_row = get_customer(db)
        if not customer_row:
            session.clear()
            flash("Please login again.", "warning")
            return redirect(url_for("customer.login"))
        if request.method == "POST":
            rating = safe_int(request.form.get("rating"), 0)
            feedback = (request.form.get("feedback") or "").strip()[:1000]
            if rating < 1 or rating > 5:
                flash("Please choose a rating from 1 to 5.", "danger")
                return redirect(url_for("customer.rate_app"))
            db.execute(
                "INSERT INTO customer_app_ratings (customer_id,rating,feedback) VALUES (?,?,?)",
                (customer_row["id"], rating, feedback),
            )
            db.commit()
            flash("Thank you for your feedback!", "success")
            return redirect(url_for("customer.rate_app"))
        return render_template("customer/rate_app.html")
    finally:
        db.close()


@customer_bp.route("/reset-password", methods=["GET", "POST"])
@customer_login_required
def reset_password():
    """Complete a password reset only after a separate OTP flow authorizes it."""
    if not session.get("customer_password_reset_verified"):
        flash("Please verify your password-reset request first, or use Change Password from Settings.", "warning")
        return redirect(url_for("customer.change_password"))

    db = get_db()
    try:
        ensure_customer_schema(db)
        customer_row = get_customer(db)
        if not customer_row:
            session.clear()
            flash("Please login again.", "warning")
            return redirect(url_for("customer.login"))

        if request.method == "POST":
            password = request.form.get("new_password") or ""
            confirmation = request.form.get("confirm_password") or ""
            if len(password) < 8 or not re.search(r"[A-Z]", password) or not re.search(r"[a-z]", password) or not re.search(r"\d", password):
                flash("Use at least 8 characters including uppercase, lowercase and a number.", "danger")
                return redirect(url_for("customer.reset_password"))
            if password != confirmation:
                flash("The new passwords do not match.", "danger")
                return redirect(url_for("customer.reset_password"))
            db.execute(
                "UPDATE customers SET password=? WHERE id=?",
                (generate_password_hash(password), customer_row["id"]),
            )
            db.commit()
            session.pop("customer_password_reset_verified", None)
            flash("Password reset successfully.", "success")
            return redirect(url_for("customer.login"))

        return render_template("customer/reset_password.html")
    finally:
        db.close()


@customer_bp.route("/check-update", methods=["GET", "POST"])
@customer_login_required
def check_update():
    current_version = str(current_app.config.get("APP_VERSION", "Not configured"))
    latest_version = str(current_app.config.get("LATEST_APP_VERSION", "Not configured"))
    update_available = (
        current_version != "Not configured"
        and latest_version != "Not configured"
        and current_version != latest_version
    )
    last_checked = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%d %b %Y, %I:%M %p")
    if request.method == "POST":
        if latest_version == "Not configured":
            flash("No update service is configured yet; a live version check is unavailable.", "info")
        elif update_available:
            flash("A newer version is configured as available. Follow your administrator's update instructions.", "warning")
        else:
            flash("The configured version information shows no newer version.", "success")
    return render_template(
        "customer/check_update.html",
        current_version=current_version,
        latest_version=latest_version,
        update_available=update_available,
        update_status=("Update available" if update_available else ("Current" if latest_version != "Not configured" and current_version == latest_version else "Not configured")),
        last_checked=last_checked,
    )


@customer_bp.route("/app-version")
@customer_login_required
def app_version():
    current_version = str(current_app.config.get("APP_VERSION", "Not configured"))
    latest_version = str(current_app.config.get("LATEST_APP_VERSION", "Not configured"))
    return render_template(
        "customer/app_version.html",
        version=current_version,
        current_version=current_version,
        latest_version=latest_version,
        last_checked="Not checked in this session",
    )
