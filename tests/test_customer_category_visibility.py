import sqlite3

from routes.customer import (
    _backfill_unlinked_service_categories,
    list_customer_categories,
)


def _catalog_connection():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE service_categories (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            icon TEXT DEFAULT '✦',
            description TEXT DEFAULT '',
            is_active INTEGER DEFAULT 1,
            normal_enabled INTEGER DEFAULT 1,
            full_day_enabled INTEGER DEFAULT 0,
            full_day_price REAL DEFAULT 0,
            visit_fee REAL DEFAULT 0,
            included_work TEXT DEFAULT '',
            excluded_work TEXT DEFAULT ''
        );
        CREATE TABLE services (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            category TEXT,
            active INTEGER DEFAULT 1,
            status TEXT DEFAULT 'active',
            icon TEXT DEFAULT '✦',
            description TEXT DEFAULT '',
            price REAL DEFAULT 0,
            full_day_price REAL DEFAULT 0,
            visit_fee REAL DEFAULT 0,
            included_work TEXT DEFAULT '',
            excluded_work TEXT DEFAULT ''
        );
        CREATE TABLE workers (
            id INTEGER PRIMARY KEY,
            worker_id TEXT,
            fullname TEXT,
            status TEXT DEFAULT 'Active',
            skills TEXT,
            latitude REAL,
            longitude REAL,
            service_radius_km REAL DEFAULT 10,
            work_type TEXT DEFAULT 'both',
            last_seen_at TEXT,
            is_online INTEGER DEFAULT 1,
            is_available INTEGER DEFAULT 1,
            is_deleted INTEGER DEFAULT 0
        );
        """
    )
    conn.execute(
        "INSERT INTO service_categories "
        "(id,name,is_active,normal_enabled,full_day_enabled) "
        "VALUES (1,'AC',1,1,1)"
    )
    conn.execute(
        "INSERT INTO services (id,name,category,active,status,icon,price) "
        "VALUES (5,'AC Repair',NULL,1,'Active','❄️',150)"
    )
    conn.execute(
        "INSERT INTO services (id,name,category,active,status,icon,price) "
        "VALUES (6,'Action Camera Repair',NULL,1,'Active','🛠️',100)"
    )
    conn.execute(
        "INSERT INTO workers "
        "(id,worker_id,fullname,status,skills,latitude,longitude,"
        "service_radius_km,work_type,last_seen_at,is_online,is_available,is_deleted) "
        "VALUES (1,'SV10001','Test Worker','Active','AC',"
        "29.1195,78.5971,10,'both','2000-01-01 00:00:00',1,1,0)"
    )
    return conn


def test_legacy_unassigned_service_is_linked_to_obvious_parent_only():
    conn = _catalog_connection()
    try:
        _backfill_unlinked_service_categories(conn)
        values = {
            row["name"]: row["category"]
            for row in conn.execute("SELECT name, category FROM services")
        }
        assert values["AC Repair"] == "AC"
        # Word boundary check: AC must not be inferred from the word Action.
        assert values["Action Camera Repair"] is None

        # Migration is idempotent and won't change an existing parent link.
        conn.execute(
            "UPDATE services SET category='Custom Parent' WHERE id=5"
        )
        _backfill_unlinked_service_categories(conn)
        assert conn.execute(
            "SELECT category FROM services WHERE id=5"
        ).fetchone()[0] == "Custom Parent"
    finally:
        conn.close()


def test_normal_category_is_visible_with_local_active_worker_even_if_heartbeat_is_stale():
    conn = _catalog_connection()
    try:
        _backfill_unlinked_service_categories(conn)
        categories = list_customer_categories(
            conn, 29.1195, 78.5971, "normal"
        )
        assert [item["name"] for item in categories] == ["AC"]
        assert [item["name"] for item in categories[0]["subcategories"]] == [
            "AC Repair"
        ]

        # The stricter mode remains available for real-time checks.
        strict_categories = list_customer_categories(
            conn, 29.1195, 78.5971, "normal", require_recent_worker=True
        )
        assert strict_categories == []
    finally:
        conn.close()
