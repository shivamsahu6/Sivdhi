import sqlite3
from datetime import datetime

from utils.assignment_monitor import (
    IST,
    _assignment_clock_start,
    _record_event_once,
    ensure_assignment_monitor_schema,
    reminder_minutes,
    reassign_minutes,
    _booking_age_minutes,
    _format_duration,
    _notify_assignment_event,
    _response_window_seconds,
    response_seconds_for_count,
    reminder_thresholds,
)


def test_worker_count_splits_the_one_hour_window_evenly():
    assert response_seconds_for_count(1) == 3600
    assert response_seconds_for_count(6) == 600
    assert response_seconds_for_count(12) == 300
    assert response_seconds_for_count(0) == 3600


def test_worker_and_partner_reminders_are_inside_allocated_response_window():
    # A worker allocated 10 minutes is reminded at ~3m20s and ~6m40s,
    # before the 10-minute timeout triggers reassignment.
    first, second = reminder_thresholds(600)
    assert first == 200
    assert second == 400
    assert 0 < first < second < 600

    first_short, second_short = reminder_thresholds(30)
    assert 0 < first_short < second_short < 30


def test_response_window_duration_formatting():
    assert _format_duration(600) == "10 minute(s)"
    assert _format_duration(514) == "8 minute(s) 34 second(s)"
    assert _format_duration(30) == "30 second(s)"


def test_saved_response_window_is_reused_without_recounting_workers():
    class NoQueryConnection:
        def execute(self, *args, **kwargs):
            raise AssertionError("Saved allocation should not require a worker recount")

    booking = {"assignment_response_seconds": 300}
    assert _response_window_seconds(NoQueryConnection(), booking) == 300


def _conn_with_bookings():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row

    conn.execute(
        """
        CREATE TABLE bookings (
            id INTEGER PRIMARY KEY,
            booking_id TEXT UNIQUE,
            customer_id INTEGER,
            category TEXT,
            latitude REAL,
            longitude REAL,
            booking_status TEXT DEFAULT 'assigned',
            status TEXT DEFAULT 'assigned',
            worker_id INTEGER,
            assigned_worker_id INTEGER,
            booking_type TEXT DEFAULT 'normal',
            service_type TEXT DEFAULT 'normal',
            preferred_date TEXT,
            preferred_time TEXT,
            assigned_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    return conn


def test_assignment_monitor_schema_is_additive_and_idempotent():
    conn = _conn_with_bookings()

    try:
        ensure_assignment_monitor_schema(conn)
        ensure_assignment_monitor_schema(conn)

        columns = {
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(bookings)"
            )
        }

        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }

        assert "partner_acknowledged_at" in columns
        assert "assignment_retry_after" in columns
        assert "assignment_response_seconds" in columns
        assert "worker_seen_at" in columns
        assert "booking_assignment_events" in tables
        assert "notification_email_outbox" in tables

    finally:
        conn.close()


def test_assignment_event_is_idempotent_per_assignment():
    conn = _conn_with_bookings()

    try:
        ensure_assignment_monitor_schema(conn)

        assert _record_event_once(
            conn,
            7,
            12,
            "partner_escalation",
            "2026-10-16T10:00:00+05:30",
        ) is True

        assert _record_event_once(
            conn,
            7,
            12,
            "partner_escalation",
            "2026-10-16T10:00:00+05:30",
        ) is False

        # A new assignment timestamp represents a new assignment.
        assert _record_event_once(
            conn,
            7,
            13,
            "partner_escalation",
            "2026-10-16T10:20:00+05:30",
        ) is True

    finally:
        conn.close()


def test_normal_assignment_clock_uses_assignment_timestamp():
    row = {
        "assigned_at": "2026-10-16T10:00:00+05:30",
        "created_at": "2026-10-16 09:50:00",
        "booking_type": "normal",
        "service_type": "normal",
        "preferred_date": "2026-10-16",
        "booking_date": "2026-10-16",
    }

    now = datetime(
        2026, 10, 16, 10, 10,
        tzinfo=IST,
    )

    assert _assignment_clock_start(
        row,
        now,
    ) == datetime(
        2026, 10, 16, 10, 0,
        tzinfo=IST,
    )


def test_full_day_monitor_clock_waits_until_7am_on_service_date():
    row = {
        "assigned_at": "2026-10-15T22:00:00+05:30",
        "created_at": "2026-10-15 22:00:00",
        "booking_type": "full-day",
        "service_type": "full-day",
        "preferred_date": "2026-10-16",
        "booking_date": "2026-10-16",
    }

    now = datetime(
        2026, 10, 16, 6, 30,
        tzinfo=IST,
    )

    assert _assignment_clock_start(
        row,
        now,
    ) == datetime(
        2026, 10, 16, 7, 0,
        tzinfo=IST,
    )


def test_reassignment_timeout_is_after_reminder_timeout(monkeypatch):
    monkeypatch.setenv(
        "SIVDHI_ASSIGNMENT_REMINDER_MINUTES",
        "5",
    )
    monkeypatch.setenv(
        "SIVDHI_ASSIGNMENT_REASSIGN_MINUTES",
        "4",
    )

    assert reminder_minutes() == 5
    assert reassign_minutes() == 6

def test_booking_age_uses_original_booking_creation_time():
    from datetime import datetime

    booking = {
        "created_at": "2026-10-16T10:00:00+05:30",
        "assigned_at": "2026-10-16T10:45:00+05:30",
    }
    now = datetime(2026, 10, 16, 11, 1, tzinfo=IST)
    assert _booking_age_minutes(booking, now) == 61.0


def test_notifications_are_queued_for_email_delivery():
    conn = _conn_with_bookings()
    try:
        conn.executescript("""
            CREATE TABLE notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_type TEXT, user_id TEXT, title TEXT, message TEXT,
                is_read INTEGER DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE customers (id INTEGER PRIMARY KEY, email TEXT, email_notification INTEGER DEFAULT 1);
            CREATE TABLE workers (id INTEGER PRIMARY KEY, email TEXT, email_notification INTEGER DEFAULT 1);
            CREATE TABLE partners (partner_id TEXT PRIMARY KEY, email TEXT, email_notification INTEGER DEFAULT 1);
            INSERT INTO customers(id,email,email_notification) VALUES (4,'customer@example.com',1);
        """)
        ensure_assignment_monitor_schema(conn)
        conn.execute("INSERT INTO notifications(user_type,user_id,title,message) VALUES ('customer','4','Booking update','Your booking is being assigned.')")
        queued = conn.execute("SELECT recipient, subject, body FROM notification_email_outbox").fetchone()
        assert queued["recipient"] == "customer@example.com"
        assert "Booking update" in queued["subject"]
        assert "being assigned" in queued["body"]
    finally:
        conn.close()



def test_legacy_customer_schema_can_insert_notifications_and_queue_email():
    """A legacy customers table without email_notification must not break alerts."""
    conn = _conn_with_bookings()
    try:
        conn.executescript("""
            CREATE TABLE notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_type TEXT, user_id TEXT, title TEXT, message TEXT,
                is_read INTEGER DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE customers (id INTEGER PRIMARY KEY, email TEXT);
            CREATE TABLE workers (id INTEGER PRIMARY KEY, email TEXT, email_notification INTEGER DEFAULT 1);
            CREATE TABLE partners (partner_id TEXT PRIMARY KEY, email TEXT, email_notification INTEGER DEFAULT 1);
            INSERT INTO customers(id,email) VALUES (4,'legacy-customer@example.com');
        """)
        ensure_assignment_monitor_schema(conn)
        conn.execute(
            "INSERT INTO notifications(user_type,user_id,title,message) VALUES ('customer','4','Booking update','Your booking is being assigned.')"
        )
        queued = conn.execute(
            "SELECT recipient FROM notification_email_outbox"
        ).fetchone()
        assert queued["recipient"] == "legacy-customer@example.com"
        assert "email_notification" in {
            row[1] for row in conn.execute("PRAGMA table_info(customers)")
        }
    finally:
        conn.close()

def test_worker_and_partner_notifications_queue_emails():
    conn = _conn_with_bookings()
    try:
        conn.executescript("""
            CREATE TABLE notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_type TEXT, user_id TEXT, title TEXT, message TEXT,
                is_read INTEGER DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE customers (id INTEGER PRIMARY KEY, email TEXT, email_notification INTEGER DEFAULT 1);
            CREATE TABLE workers (id INTEGER PRIMARY KEY, email TEXT, email_notification INTEGER DEFAULT 1);
            CREATE TABLE partners (partner_id TEXT PRIMARY KEY, email TEXT, email_notification INTEGER DEFAULT 1);
            INSERT INTO workers(id,email,email_notification) VALUES (12,'worker@example.com',1);
            INSERT INTO partners(partner_id,email,email_notification) VALUES ('P-4','partner@example.com',1);
        """)
        ensure_assignment_monitor_schema(conn)
        conn.execute("INSERT INTO notifications(user_type,user_id,title,message) VALUES ('worker','12','Response expired','Please respond.')")
        conn.execute("INSERT INTO notifications(user_type,user_id,title,message) VALUES ('partner','P-4','Worker did not respond','Please follow up.')")
        recipients = {row[0] for row in conn.execute("SELECT recipient FROM notification_email_outbox")}
        assert recipients == {'worker@example.com', 'partner@example.com'}
    finally:
        conn.close()


def test_critical_assignment_email_is_queued_even_if_general_email_alerts_are_off():
    conn = _conn_with_bookings()
    try:
        conn.executescript("""
            CREATE TABLE notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_type TEXT, user_id TEXT, title TEXT, message TEXT,
                is_read INTEGER DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE customers (id INTEGER PRIMARY KEY, email TEXT, email_notification INTEGER DEFAULT 1);
            CREATE TABLE workers (id INTEGER PRIMARY KEY, email TEXT, email_notification INTEGER DEFAULT 0);
            CREATE TABLE partners (partner_id TEXT PRIMARY KEY, email TEXT, email_notification INTEGER DEFAULT 0);
            INSERT INTO workers(id,email,email_notification) VALUES (12,'worker-critical@example.com',0);
        """)
        ensure_assignment_monitor_schema(conn)
        _notify_assignment_event(conn, "worker", 12, "Response Time Expired", "Booking SIV-9 needs reassignment.")
        queued = conn.execute(
            "SELECT recipient, subject, body FROM notification_email_outbox WHERE recipient='worker-critical@example.com'"
        ).fetchone()
        assert queued is not None
        assert "Response Time Expired" in queued["subject"]
        assert "needs reassignment" in queued["body"]
    finally:
        conn.close()


def test_deadline_finalizer_cancels_booking_and_marks_manual_refund_pending(monkeypatch):
    from utils.assignment_monitor import _finalize_unassigned_booking

    monkeypatch.setenv("SIVDHI_PAYMENT_MODE", "demo")
    monkeypatch.setenv("SIVDHI_ALLOW_DEMO_PAYMENTS", "1")
    conn = _conn_with_bookings()
    try:
        conn.executescript("""
            CREATE TABLE notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_type TEXT, user_id TEXT, title TEXT, message TEXT,
                is_read INTEGER DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE customers (id INTEGER PRIMARY KEY, email TEXT, email_notification INTEGER DEFAULT 1);
            CREATE TABLE workers (id INTEGER PRIMARY KEY, email TEXT, email_notification INTEGER DEFAULT 1);
            CREATE TABLE partners (partner_id TEXT PRIMARY KEY, email TEXT, email_notification INTEGER DEFAULT 1);
            INSERT INTO customers(id,email,email_notification) VALUES (1,'customer@example.com',1);
            ALTER TABLE bookings ADD COLUMN payment_status TEXT DEFAULT 'pending';
            ALTER TABLE bookings ADD COLUMN payment_method TEXT;
            ALTER TABLE bookings ADD COLUMN total_amount REAL DEFAULT 0;
            ALTER TABLE bookings ADD COLUMN partner_id TEXT;
            ALTER TABLE bookings ADD COLUMN assigned_partner_id TEXT;
            ALTER TABLE bookings ADD COLUMN cancellation_reason TEXT;
            ALTER TABLE bookings ADD COLUMN updated_at TEXT;
        """)
        ensure_assignment_monitor_schema(conn)
        conn.execute("""
            INSERT INTO bookings(id, booking_id, customer_id, category, booking_status, status,
                worker_id, assigned_worker_id, payment_status, payment_method, total_amount,
                advance_paid_amount, created_at)
            VALUES (1,'SIV-TEST-1',1,'AC','pending','pending',NULL,NULL,'PAID_DEMO','demo',100,25,'2026-10-16T10:00:00+05:30')
        """)
        booking = conn.execute("SELECT * FROM bookings WHERE id=1").fetchone()
        _finalize_unassigned_booking(conn, booking)
        updated = conn.execute("SELECT booking_status,payment_status,advance_payment_status,refund_status,refund_amount FROM bookings WHERE id=1").fetchone()
        assert updated["booking_status"] == "cancelled"
        assert updated["payment_status"] == "REFUND_PENDING"
        assert updated["advance_payment_status"] == "REFUND_PENDING"
        assert updated["refund_status"] == "pending_admin"
        assert updated["refund_amount"] == 25
        ledger = conn.execute("SELECT amount,status FROM sivdhi_finance_ledger WHERE booking_id='SIV-TEST-1' AND entry_type='customer_refund'").fetchone()
        assert ledger is None
    finally:
        conn.close()


def test_newly_eligible_worker_joins_live_booking_without_resetting_deadline(monkeypatch):
    import sys
    import types
    from datetime import timedelta
    from utils.assignment_monitor import (
        _booking_assignment_deadline,
        _discover_live_worker_offers,
    )

    conn = _conn_with_bookings()
    conn.executescript("""
        ALTER TABLE bookings ADD COLUMN updated_at TEXT;
        ALTER TABLE bookings ADD COLUMN partner_id TEXT;
        ALTER TABLE bookings ADD COLUMN assigned_partner_id TEXT;
        ALTER TABLE bookings ADD COLUMN booking_date TEXT;
        ALTER TABLE bookings ADD COLUMN booking_time TEXT;
        CREATE TABLE notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_type TEXT, user_id TEXT,
            title TEXT, message TEXT, is_read INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE customers (id INTEGER PRIMARY KEY, email TEXT, email_notification INTEGER DEFAULT 1);
        CREATE TABLE workers (id INTEGER PRIMARY KEY, worker_id TEXT, email TEXT, email_notification INTEGER DEFAULT 1);
        CREATE TABLE partners (id INTEGER PRIMARY KEY, partner_id TEXT, email TEXT, email_notification INTEGER DEFAULT 1);
        CREATE TABLE partner_workers (id INTEGER PRIMARY KEY, partner_id TEXT, worker_id TEXT);
        CREATE TABLE booking_worker_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, booking_id TEXT, worker_id INTEGER,
            status TEXT, attempt_status TEXT, distance_km REAL, reason TEXT,
            attempted_at TEXT DEFAULT 'initial', rejection_reason TEXT, created_at TEXT, updated_at TEXT,
            UNIQUE(booking_id, worker_id)
        );
        INSERT INTO customers(id,email) VALUES (1,'customer@example.com');
        INSERT INTO workers(id,worker_id,email) VALUES (1,'W1','w1@example.com');
        INSERT INTO workers(id,worker_id,email) VALUES (2,'W2','w2@example.com');
        INSERT INTO partners(id,partner_id,email) VALUES (1,'P1','p1@example.com');
        INSERT INTO partners(id,partner_id,email) VALUES (2,'P2','p2@example.com');
        INSERT INTO partner_workers(partner_id,worker_id) VALUES ('P1','W1');
        INSERT INTO partner_workers(partner_id,worker_id) VALUES ('P2','W2');
    """)
    start = datetime(2026, 10, 23, 10, 0, tzinfo=IST)
    conn.execute("""
        INSERT INTO bookings(
            id,booking_id,customer_id,category,latitude,longitude,booking_status,status,
            worker_id,assigned_worker_id,partner_id,assigned_partner_id,assigned_at,created_at,
            booking_type,service_type,preferred_date,preferred_time,booking_date,booking_time
        ) VALUES (1,'SIV-TEST',1,'Cleaning',28.6,77.2,'assigned','assigned',1,1,'P1','P1',?,?,'normal','normal','2026-10-23','10:00','2026-10-23','10:00')
    """, (start.isoformat(), start.isoformat()))
    conn.execute("INSERT INTO booking_worker_attempts(booking_id,worker_id,status,attempt_status) VALUES ('1',1,'assigned','assigned')")
    ensure_assignment_monitor_schema(conn)

    eligible = {1}
    fake_customer_routes = types.ModuleType("routes.customer")

    def attempted(db, booking_id):
        return {str(row[0]) for row in db.execute(
            "SELECT worker_id FROM booking_worker_attempts WHERE booking_id=?", (str(booking_id),)
        ).fetchall()}

    def record(db, booking_id, worker_id, distance_km=None, status="assigned"):
        db.execute(
            "INSERT OR IGNORE INTO booking_worker_attempts(booking_id,worker_id,status,attempt_status,distance_km) VALUES(?,?,?,?,?)",
            (str(booking_id), int(worker_id), status, status, distance_km),
        )

    def nearest(db, category, latitude, longitude, excluded, **kwargs):
        for worker_id in sorted(eligible):
            if str(worker_id) not in {str(value) for value in excluded} and f"W{worker_id}" not in {str(value) for value in excluded}:
                return {"worker_db_id": worker_id, "worker_id": f"W{worker_id}", "distance_km": float(worker_id)}
        return None

    fake_customer_routes.get_booking_attempted_worker_ids = attempted
    fake_customer_routes.record_worker_attempt = record
    fake_customer_routes.find_nearest_worker = nearest
    monkeypatch.setitem(sys.modules, "routes.customer", fake_customer_routes)

    try:
        booking = conn.execute("SELECT * FROM bookings WHERE id=1").fetchone()
        assert _discover_live_worker_offers(conn, booking, start) == 0
        first_deadline = conn.execute(
            "SELECT response_deadline FROM booking_assignment_offers WHERE booking_db_id=1 AND worker_id=1"
        ).fetchone()[0]
        assert datetime.fromisoformat(first_deadline) == start + timedelta(minutes=60)

        # Worker B registers/activates at 10:20 during the same pending booking.
        eligible.add(2)
        join_time = start + timedelta(minutes=20)
        booking = conn.execute("SELECT * FROM bookings WHERE id=1").fetchone()
        assert _discover_live_worker_offers(conn, booking, join_time) == 1
        offers = conn.execute(
            "SELECT worker_id,response_deadline FROM booking_assignment_offers WHERE booking_db_id=1 AND status='offered' ORDER BY worker_id"
        ).fetchall()
        expected_deadline = start + timedelta(minutes=40)
        assert len(offers) == 2
        assert all(datetime.fromisoformat(row["response_deadline"]) == expected_deadline for row in offers)
        assert _booking_assignment_deadline(booking, join_time) == start + timedelta(minutes=60)
        assert conn.execute("SELECT COUNT(*) FROM notification_email_outbox WHERE recipient IN ('w1@example.com','w2@example.com')").fetchone()[0] >= 2
    finally:
        conn.close()
