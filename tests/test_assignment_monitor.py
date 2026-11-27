import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Barrier

from routes.customer import _worker_has_active_booking, _worker_has_schedule_conflict

from utils.assignment_monitor import (
    IST,
    _assignment_monitor_skip_booking_ids,
    _assignment_clock_start,
    _auto_complete_full_day_jobs,
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



def _add_notification_email_schema(conn):
    conn.execute("""
        CREATE TABLE customers (
            id INTEGER PRIMARY KEY,
            email TEXT,
            email_notification INTEGER DEFAULT 1
        )
    """)
    conn.execute("""
        CREATE TABLE workers (
            id INTEGER PRIMARY KEY,
            worker_id TEXT,
            email TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE partners (
            partner_id TEXT PRIMARY KEY,
            id INTEGER,
            email TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE notifications (
            id INTEGER PRIMARY KEY,
            user_type TEXT,
            user_id TEXT,
            title TEXT,
            message TEXT
        )
    """)
    conn.execute(
        "INSERT INTO customers(id,email) VALUES(1,'schema-test@example.com')"
    )


def test_notification_email_trigger_rebuild_is_transactional():
    conn = _conn_with_bookings()
    try:
        _add_notification_email_schema(conn)
        trigger_creation_transactions = []

        def trace(sql):
            if sql.lstrip().upper().startswith(
                "CREATE TRIGGER TRG_SIVDHI_NOTIFICATION_EMAIL_OUTBOX"
            ):
                trigger_creation_transactions.append(conn.in_transaction)

        conn.set_trace_callback(trace)
        ensure_assignment_monitor_schema(conn)
        conn.set_trace_callback(None)

        assert trigger_creation_transactions == [True]

        conn.execute("""
            INSERT INTO notifications(user_type,user_id,title,message)
            VALUES('customer','1','Schema test','Transactional trigger works')
        """)
        queued = conn.execute("""
            SELECT recipient, subject, body
            FROM notification_email_outbox
        """).fetchone()

        assert queued is not None
        assert queued["recipient"] == "schema-test@example.com"
        assert "Schema test" in queued["subject"]
        assert "Transactional trigger works" in queued["body"]
    finally:
        conn.close()


def test_assignment_monitor_schema_serializes_concurrent_connections(tmp_path):
    db_path = tmp_path / "concurrent-schema.db"
    seed = _conn_with_bookings()
    _add_notification_email_schema(seed)
    seed.commit()
    disk = sqlite3.connect(str(db_path))
    try:
        seed.backup(disk)
    finally:
        disk.close()
        seed.close()

    start = Barrier(4)

    def initialize_schema():
        conn = sqlite3.connect(str(db_path), timeout=20)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=20000")
        try:
            start.wait(timeout=10)
            ensure_assignment_monitor_schema(conn)
            conn.commit()
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(initialize_schema) for _ in range(4)]
        for future in futures:
            future.result(timeout=30)

    verify = sqlite3.connect(str(db_path))
    try:
        count = verify.execute("""
            SELECT COUNT(*) FROM sqlite_master
            WHERE type='trigger'
              AND name='trg_sivdhi_notification_email_outbox'
        """).fetchone()[0]
        assert count == 1
    finally:
        verify.close()

def test_assignment_monitor_schema_preserves_existing_transaction():
    conn = _conn_with_bookings()
    try:
        conn.execute(
            "INSERT INTO bookings (id, booking_id, category) "
            "VALUES (1, 'SIV-TX', 'AC')"
        )
        assert conn.in_transaction

        ensure_assignment_monitor_schema(conn)
        assert conn.in_transaction

        conn.rollback()

        assert conn.execute(
            "SELECT COUNT(*) FROM bookings"
        ).fetchone()[0] == 0
        assert conn.execute("""
            SELECT COUNT(*) FROM sqlite_master
            WHERE type='table' AND name='booking_assignment_events'
        """).fetchone()[0] == 0
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


def test_full_day_jobs_auto_complete_at_5pm_only_after_start(monkeypatch):
    import utils.assignment_monitor as monitor

    conn = _conn_with_bookings()
    notifications = []
    monkeypatch.setattr(
        monitor,
        "_notify_assignment_event",
        lambda *_args: notifications.append(_args[1:]),
    )
    try:
        ensure_assignment_monitor_schema(conn)
        conn.execute("ALTER TABLE bookings ADD COLUMN total_amount REAL DEFAULT 0")
        conn.execute("ALTER TABLE bookings ADD COLUMN amount REAL DEFAULT 0")
        conn.execute("""
            INSERT INTO bookings (
                id, booking_id, customer_id, category,
                booking_status, status, worker_id, assigned_worker_id,
                booking_type, service_type, preferred_date, booking_date,
                preferred_time, booking_time, worker_arrived_at,
                total_amount, amount, advance_amount, advance_paid_amount,
                advance_payment_status, balance_due_amount,
                balance_payment_status
            ) VALUES (
                1, 'SIV-FULL-1', 1, 'Cleaning',
                'in_progress', 'in_progress', 7, 7,
                'full-day', 'full-day', '2026-11-27', '2026-11-27',
                '09:00', '09:00', '2026-11-27T09:05:00+05:30',
                1000, 1000, 50, 50, 'PAID', 950, 'DUE'
            )
        """)
        conn.execute("""
            INSERT INTO bookings (
                id, booking_id, customer_id, category,
                booking_status, status, worker_id, assigned_worker_id,
                booking_type, service_type, preferred_date, booking_date,
                worker_arrived_at
            ) VALUES (
                2, 'SIV-FULL-NOT-STARTED', 1, 'Cleaning',
                'accepted', 'accepted', 8, 8,
                'full-day', 'full-day', '2026-11-27', '2026-11-27',
                '2026-11-27T09:05:00+05:30'
            )
        """)
        conn.execute("""
            INSERT INTO bookings (
                id, booking_id, customer_id, category,
                booking_status, status, worker_id, assigned_worker_id,
                booking_type, service_type, preferred_date, booking_date,
                worker_arrived_at
            ) VALUES (
                3, 'SIV-NORMAL', 1, 'Cleaning',
                'in_progress', 'in_progress', 9, 9,
                'normal', 'normal', '2026-11-27', '2026-11-27',
                '2026-11-27T09:05:00+05:30'
            )
        """)
        conn.commit()

        before_five = datetime(2026, 11, 27, 16, 59, tzinfo=IST)
        assert _auto_complete_full_day_jobs(conn, before_five) == 0

        at_five = datetime(2026, 11, 27, 17, 0, tzinfo=IST)
        assert _auto_complete_full_day_jobs(conn, at_five) == 1

        completed = conn.execute(
            "SELECT booking_status, auto_completed_at, commission_settled_at "
            "FROM bookings WHERE id=1"
        ).fetchone()
        assert completed["booking_status"] == "completed"
        assert completed["auto_completed_at"]
        # The balance remains due, so service completion must not settle wallets.
        assert completed["commission_settled_at"] is None

        assert conn.execute(
            "SELECT booking_status FROM bookings WHERE id=2"
        ).fetchone()[0] == "accepted"
        assert conn.execute(
            "SELECT booking_status FROM bookings WHERE id=3"
        ).fetchone()[0] == "in_progress"
        assert len(notifications) == 2
        assert {row[0] for row in notifications} == {"customer", "worker"}

        # The same monitor pass must never send duplicate completion notices.
        assert _auto_complete_full_day_jobs(conn, at_five) == 0
        assert len(notifications) == 2
    finally:
        conn.close()


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
        assert updated["refund_amount"] == 0
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

        # At the rebalanced response deadline, stale offers are closed even
        # when a worker never opened the request or clicked Reject.
        expiry_time = start + timedelta(minutes=40)
        booking = conn.execute("SELECT * FROM bookings WHERE id=1").fetchone()
        assert _discover_live_worker_offers(conn, booking, expiry_time) == 0
        offer_statuses = {
            row[0] for row in conn.execute(
                "SELECT status FROM booking_assignment_offers WHERE booking_db_id=1"
            ).fetchall()
        }
        assert offer_statuses == {"expired"}
    finally:
        conn.close()


def _worker_capacity_test_connection():
    conn = _conn_with_bookings()
    conn.execute("""
        CREATE TABLE workers (
            id INTEGER PRIMARY KEY,
            worker_id TEXT
        )
    """)
    conn.execute(
        "INSERT INTO workers (id, worker_id) VALUES (7, 'PUBLIC-WORKER-7')"
    )
    conn.execute("""
        CREATE TABLE booking_assignment_offers (
            id INTEGER PRIMARY KEY,
            booking_db_id INTEGER NOT NULL,
            worker_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            response_deadline TEXT
        )
    """)
    return conn


def test_active_worker_is_blocked_even_for_a_different_future_slot():
    conn = _worker_capacity_test_connection()
    try:
        conn.execute("""
            INSERT INTO bookings (
                id, booking_id, booking_status, status,
                worker_id, assigned_worker_id,
                preferred_date, preferred_time
            ) VALUES (
                1, 'SIV-TEST-1', 'accepted', 'accepted',
                7, 7, '2026-11-16', '09:00'
            )
        """)

        assert _worker_has_active_booking(conn, 7) is True
        assert _worker_has_schedule_conflict(
            conn, 7, "2026-11-20", "18:00", "normal"
        ) is True

        # Reassignment checks must ignore the booking being reassigned.
        assert _worker_has_active_booking(
            conn, 7, exclude_booking_id=1
        ) is False
    finally:
        conn.close()


def test_completed_or_cancelled_booking_releases_worker():
    conn = _worker_capacity_test_connection()
    try:
        for status in ("completed", "cancelled", "canceled"):
            conn.execute("DELETE FROM bookings")
            conn.execute("""
                INSERT INTO bookings (
                    id, booking_id, booking_status, status,
                    worker_id, assigned_worker_id
                ) VALUES (1, ?, ?, ?, 7, 7)
            """, ("SIV-" + status, status, status))

            assert _worker_has_active_booking(conn, 7) is False
    finally:
        conn.close()



def test_live_offer_temporarily_reserves_worker_from_other_bookings():
    conn = _worker_capacity_test_connection()
    try:
        conn.execute("""
            INSERT INTO bookings (
                id, booking_id, booking_status, status
            ) VALUES (2, 'SIV-OFFER', 'pending', 'pending')
        """)
        conn.execute("""
            INSERT INTO booking_assignment_offers (
                id, booking_db_id, worker_id, status, response_deadline
            ) VALUES (
                1, 2, 7, 'offered', '2099-01-01T10:00:00+05:30'
            )
        """)

        assert _worker_has_schedule_conflict(
            conn, 7, "2026-11-16", "09:00", "normal"
        ) is True

        # The worker's own offer must not exclude them from the same booking.
        assert _worker_has_schedule_conflict(
            conn, 7, "2026-11-16", "09:00", "normal",
            exclude_booking_id=2
        ) is False
    finally:
        conn.close()


def test_expired_or_terminal_booking_offer_does_not_reserve_worker():
    conn = _worker_capacity_test_connection()
    try:
        conn.execute("""
            INSERT INTO bookings (
                id, booking_id, booking_status, status
            ) VALUES (2, 'SIV-OLD-OFFER', 'pending', 'pending')
        """)
        conn.execute("""
            INSERT INTO booking_assignment_offers (
                id, booking_db_id, worker_id, status, response_deadline
            ) VALUES (
                1, 2, 7, 'offered', '2000-01-01T10:00:00+05:30'
            )
        """)

        assert _worker_has_schedule_conflict(
            conn, 7, "2026-11-16", "09:00", "normal"
        ) is False

        conn.execute("""
            UPDATE booking_assignment_offers
            SET response_deadline='2099-01-01T10:00:00+05:30'
        """)
        conn.execute("""
            UPDATE bookings SET booking_status='cancelled', status='cancelled'
            WHERE id=2
        """)

        assert _worker_has_schedule_conflict(
            conn, 7, "2026-11-16", "09:00", "normal"
        ) is False
    finally:
        conn.close()

def test_unassigned_pending_booking_does_not_mark_worker_busy():
    conn = _worker_capacity_test_connection()
    try:
        conn.execute("""
            INSERT INTO bookings (
                id, booking_id, booking_status, status,
                worker_id, assigned_worker_id
            ) VALUES (1, 'SIV-PENDING', 'pending', 'pending', NULL, NULL)
        """)
        assert _worker_has_active_booking(conn, 7) is False
    finally:
        conn.close()

def test_released_worker_attempt_can_be_offered_again():
    from routes.customer import (
        get_booking_attempted_worker_ids,
        record_worker_attempt,
        release_worker_attempt,
    )

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE booking_worker_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            booking_id TEXT NOT NULL,
            worker_id INTEGER NOT NULL,
            distance_km REAL,
            status TEXT,
            attempt_status TEXT,
            reason TEXT,
            rejection_reason TEXT,
            attempted_at TEXT,
            created_at TEXT,
            updated_at TEXT,
            UNIQUE(booking_id, worker_id)
        )
    """)
    conn.execute("""
        INSERT INTO booking_worker_attempts
            (booking_id, worker_id, status, attempt_status)
        VALUES ('25', 7, 'offered', 'offered')
    """)

    try:
        assert get_booking_attempted_worker_ids(conn, 25) == {"7"}
        assert release_worker_attempt(conn, 25, 7) is True
        assert get_booking_attempted_worker_ids(conn, 25) == set()

        assert record_worker_attempt(
            conn, 25, 7, distance_km=3.5, status="offered"
        ) is True
        assert get_booking_attempted_worker_ids(conn, 25) == {"7"}

        row = conn.execute("""
            SELECT status, attempt_status, distance_km,
                   reason, rejection_reason
            FROM booking_worker_attempts
            WHERE booking_id='25' AND worker_id=7
        """).fetchone()

        assert row["status"] == "offered"
        assert row["attempt_status"] == "offered"
        assert row["distance_km"] == 3.5
        assert row["reason"] is None
        assert row["rejection_reason"] is None
        assert conn.execute(
            "SELECT COUNT(*) FROM booking_worker_attempts"
        ).fetchone()[0] == 1
    finally:
        conn.close()

def test_assignment_monitor_skip_booking_ids_are_process_configured(monkeypatch):
    monkeypatch.setenv(
        "SIVDHI_ASSIGNMENT_MONITOR_SKIP_BOOKING_IDS",
        "SIV-261115-088009, 3, ,SIV-OTHER",
    )
    assert _assignment_monitor_skip_booking_ids() == {
        "SIV-261115-088009", "3", "SIV-OTHER"
    }

    monkeypatch.delenv("SIVDHI_ASSIGNMENT_MONITOR_SKIP_BOOKING_IDS")
    assert _assignment_monitor_skip_booking_ids() == set()


def _worker_accept_route_test_client(tmp_path, monkeypatch):
    """Isolated Flask/SQLite fixture for the real worker acceptance route."""
    import sqlite3
    from flask import Flask, session
    import routes.worker as worker_routes
    import routes.decorators as route_decorators
    import utils.sivdhi_finance as finance_utils

    database_path = tmp_path / "worker-accept-route.sqlite"
    conn = sqlite3.connect(database_path)
    conn.executescript("""
        CREATE TABLE workers (
            id INTEGER PRIMARY KEY,
            worker_id TEXT NOT NULL UNIQUE,
            fullname TEXT,
            email TEXT,
            mobile TEXT,
            is_deleted INTEGER DEFAULT 0,
            status TEXT DEFAULT 'active'
        );
        INSERT INTO workers(id, worker_id, fullname, email, mobile)
        VALUES
          (7, 'PUBLIC-WORKER-7', 'Worker Seven', 'w7@example.test', '9000000007'),
          (8, 'PUBLIC-WORKER-8', 'Worker Eight', 'w8@example.test', '9000000008');

        CREATE TABLE bookings (
            id INTEGER PRIMARY KEY,
            booking_id TEXT UNIQUE,
            customer_id INTEGER,
            category TEXT,
            latitude REAL,
            longitude REAL,
            address TEXT,
            city TEXT,
            state TEXT,
            pincode TEXT,
            booking_status TEXT DEFAULT 'pending',
            status TEXT DEFAULT 'pending',
            worker_id INTEGER,
            assigned_worker_id INTEGER,
            partner_id TEXT,
            assigned_partner_id TEXT,
            booking_type TEXT DEFAULT 'normal',
            service_type TEXT DEFAULT 'normal',
            preferred_date TEXT,
            preferred_time TEXT,
            assigned_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            worker_seen_at TEXT,
            partner_acknowledged_at TEXT,
            updated_at TEXT
        );

        CREATE TABLE booking_assignment_offers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            booking_db_id INTEGER NOT NULL,
            worker_id INTEGER NOT NULL,
            partner_id TEXT,
            status TEXT NOT NULL DEFAULT 'offered',
            offered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            allocation_started_at TEXT NOT NULL DEFAULT '',
            response_deadline TEXT NOT NULL,
            responded_at TEXT,
            UNIQUE(booking_db_id, worker_id)
        );

        CREATE TABLE booking_worker_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            booking_id TEXT NOT NULL,
            worker_id INTEGER NOT NULL,
            distance_km REAL,
            status TEXT DEFAULT 'offered',
            attempt_status TEXT DEFAULT 'offered',
            reason TEXT,
            rejection_reason TEXT,
            attempted_at TEXT,
            created_at TEXT,
            updated_at TEXT,
            UNIQUE(booking_id, worker_id)
        );

        CREATE TABLE notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_type TEXT NOT NULL,
            user_id TEXT NOT NULL,
            title TEXT NOT NULL,
            message TEXT NOT NULL,
            is_read INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.commit()
    conn.close()

    def db_factory():
        db = sqlite3.connect(database_path, timeout=5)
        db.row_factory = sqlite3.Row
        return db

    def current_worker_id():
        db = db_factory()
        try:
            row = db.execute(
                "SELECT id FROM workers WHERE worker_id=? LIMIT 1",
                (str(session.get("user_id")),),
            ).fetchone()
            return row["id"] if row else None
        finally:
            db.close()

    def visible_booking(booking_value):
        db = db_factory()
        try:
            if str(booking_value).isdigit():
                return db.execute(
                    "SELECT * FROM bookings WHERE id=? LIMIT 1",
                    (int(booking_value),),
                ).fetchone()
            return db.execute(
                "SELECT * FROM bookings WHERE booking_id=? LIMIT 1",
                (str(booking_value),),
            ).fetchone()
        finally:
            db.close()

    monkeypatch.setattr(worker_routes, "get_db", db_factory)
    monkeypatch.setattr(worker_routes, "get_current_worker_db_id", current_worker_id)
    monkeypatch.setattr(worker_routes, "get_booking_for_worker", visible_booking)
    monkeypatch.setattr(
        worker_routes, "ensure_assignment_monitor_schema", lambda _db: None
    )
    monkeypatch.setattr(route_decorators, "get_db", db_factory)
    monkeypatch.setattr(
        finance_utils, "find_partner_for_worker", lambda _db, _worker_id: None
    )

    app = Flask(__name__)
    app.secret_key = "isolated-worker-accept-test-secret"
    app.config["TESTING"] = True
    app.register_blueprint(worker_routes.worker_bp)
    return app.test_client(), database_path


def _worker_accept_seed_booking(path, booking_db_id, *, status="pending", worker_id=None):
    import sqlite3

    conn = sqlite3.connect(path)
    conn.execute("""
        INSERT INTO bookings (
            id, booking_id, customer_id, category,
            booking_status, status, worker_id, assigned_worker_id,
            created_at
        ) VALUES (?, ?, 1, 'Cleaning', ?, ?, ?, ?,
                  '2099-01-01T00:00:00+05:30')
    """, (
        booking_db_id, f"SIV-WORKER-ACCEPT-{booking_db_id}",
        status, status, worker_id, worker_id,
    ))
    conn.commit()
    conn.close()


def _worker_accept_seed_offer(path, booking_db_id, worker_id):
    import sqlite3

    conn = sqlite3.connect(path)
    conn.execute("""
        INSERT INTO booking_assignment_offers (
            booking_db_id, worker_id, status, offered_at, response_deadline
        ) VALUES (?, ?, 'offered', '2099-01-01T00:00:00+05:30',
                  '2099-01-01T01:00:00+05:30')
    """, (booking_db_id, worker_id))
    conn.commit()
    conn.close()


def _worker_accept_login(client, public_worker_id="PUBLIC-WORKER-7"):
    with client.session_transaction() as session:
        session["role"] = "worker"
        session["user_id"] = public_worker_id


def test_worker_live_offer_acceptance_releases_other_offers_and_is_single_use(
    tmp_path, monkeypatch
):
    import sqlite3

    client, path = _worker_accept_route_test_client(tmp_path, monkeypatch)
    _worker_accept_seed_booking(path, 1)
    _worker_accept_seed_booking(path, 2)
    _worker_accept_seed_offer(path, 1, 7)
    _worker_accept_seed_offer(path, 1, 8)
    _worker_accept_seed_offer(path, 2, 7)

    conn = sqlite3.connect(path)
    conn.execute("""
        INSERT INTO booking_worker_attempts (
            booking_id, worker_id, status, attempt_status
        ) VALUES ('1', 7, 'offered', 'offered')
    """)
    conn.execute("""
        INSERT INTO booking_worker_attempts (
            booking_id, worker_id, status, attempt_status
        ) VALUES ('2', 7, 'offered', 'offered')
    """)
    conn.commit()
    conn.close()

    _worker_accept_login(client)
    accepted = client.post("/worker/job/1/accept")
    assert accepted.status_code == 200
    assert accepted.get_json()["success"] is True

    # A different worker cannot accept the booking after the winner accepts.
    _worker_accept_login(client, "PUBLIC-WORKER-8")
    competing = client.post("/worker/job/1/accept")
    assert competing.status_code == 409

    # The winning worker cannot accept the same booking twice.
    _worker_accept_login(client)
    duplicate = client.post("/worker/job/1/accept")
    assert duplicate.status_code == 409

    conn = sqlite3.connect(path)
    booking = conn.execute("""
        SELECT booking_status, status, worker_id, assigned_worker_id
        FROM bookings WHERE id=1
    """).fetchone()
    offers = {
        (booking_id, worker_id): status
        for booking_id, worker_id, status in conn.execute("""
            SELECT booking_db_id, worker_id, status
            FROM booking_assignment_offers
        """).fetchall()
    }
    released_attempt = conn.execute("""
        SELECT status, attempt_status, reason, rejection_reason
        FROM booking_worker_attempts
        WHERE booking_id='2' AND worker_id=7
    """).fetchone()
    accepted_attempt = conn.execute("""
        SELECT status, attempt_status
        FROM booking_worker_attempts
        WHERE booking_id='1' AND worker_id=7
    """).fetchone()
    customer_notifications = conn.execute("""
        SELECT user_type, user_id, title, message
        FROM notifications
        WHERE user_type='customer' AND user_id='1'
        ORDER BY id
    """).fetchall()
    conn.close()

    assert booking == ("accepted", "accepted", 7, 7)
    assert accepted_attempt == ("accepted", "accepted")
    assert offers == {
        (1, 7): "accepted",
        (1, 8): "cancelled",
        (2, 7): "released",
    }
    assert released_attempt[0:2] == ("released", "released")
    assert "Temporarily released" in released_attempt[2]
    assert released_attempt[3] is None
    assert customer_notifications == [
        (
            "customer",
            "1",
            "Booking Confirmed",
            "Your booking SIV-WORKER-ACCEPT-1 is confirmed.",
        )
    ]


def test_worker_legacy_assignee_acceptance_notifies_customer(
    tmp_path, monkeypatch
):
    import sqlite3

    client, path = _worker_accept_route_test_client(tmp_path, monkeypatch)
    _worker_accept_seed_booking(path, 1, status="pending", worker_id=7)
    _worker_accept_seed_offer(path, 1, 7)
    _worker_accept_login(client)

    response = client.post("/worker/job/1/accept")
    assert response.status_code == 200
    assert response.get_json()["success"] is True

    conn = sqlite3.connect(path)
    booking_status = conn.execute(
        "SELECT booking_status, status, worker_id FROM bookings WHERE id=1"
    ).fetchone()
    customer_notifications = conn.execute("""
        SELECT user_type, user_id, title, message
        FROM notifications
        WHERE user_type='customer' AND user_id='1'
    """).fetchall()
    conn.close()

    assert booking_status == ("accepted", "accepted", 7)
    assert customer_notifications == [
        (
            "customer",
            "1",
            "Booking Confirmed",
            "Your booking SIV-WORKER-ACCEPT-1 is confirmed.",
        )
    ]


def test_worker_cannot_accept_live_offer_while_another_active_booking_exists(
    tmp_path, monkeypatch
):
    import sqlite3

    client, path = _worker_accept_route_test_client(tmp_path, monkeypatch)
    _worker_accept_seed_booking(path, 1)
    _worker_accept_seed_booking(path, 2, status="accepted", worker_id=7)
    _worker_accept_seed_offer(path, 1, 7)
    _worker_accept_login(client)

    response = client.post("/worker/job/1/accept")
    assert response.status_code == 409
    assert "already have an active booking" in response.get_json()["message"].lower()

    conn = sqlite3.connect(path)
    booking = conn.execute(
        "SELECT booking_status, worker_id FROM bookings WHERE id=1"
    ).fetchone()
    offer_status = conn.execute("""
        SELECT status FROM booking_assignment_offers
        WHERE booking_db_id=1 AND worker_id=7
    """).fetchone()[0]
    conn.close()

    assert booking == ("pending", None)
    assert offer_status == "offered"


def test_released_offer_is_reactivated_after_worker_booking_completes(
    monkeypatch,
):
    import sys
    import types
    from datetime import timedelta

    import utils.assignment_monitor as monitor
    from routes.customer import (
        _ensure_worker_attempt_table,
        get_booking_attempted_worker_ids,
        record_worker_attempt,
    )

    conn = _conn_with_bookings()
    conn.row_factory = sqlite3.Row

    conn.executescript("""
        ALTER TABLE bookings ADD COLUMN updated_at TEXT;
        ALTER TABLE bookings ADD COLUMN partner_id TEXT;
        ALTER TABLE bookings ADD COLUMN assigned_partner_id TEXT;
        ALTER TABLE bookings ADD COLUMN booking_date TEXT;
        ALTER TABLE bookings ADD COLUMN booking_time TEXT;

        CREATE TABLE notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_type TEXT,
            user_id TEXT,
            title TEXT,
            message TEXT,
            is_read INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE customers (
            id INTEGER PRIMARY KEY,
            email TEXT,
            email_notification INTEGER DEFAULT 1
        );

        CREATE TABLE workers (
            id INTEGER PRIMARY KEY,
            worker_id TEXT,
            email TEXT,
            email_notification INTEGER DEFAULT 1
        );

        CREATE TABLE partners (
            id INTEGER PRIMARY KEY,
            partner_id TEXT,
            email TEXT,
            email_notification INTEGER DEFAULT 1
        );

        CREATE TABLE partner_workers (
            id INTEGER PRIMARY KEY,
            partner_id TEXT,
            worker_id TEXT
        );

        INSERT INTO customers(id, email)
        VALUES (1, 'customer@example.test');

        INSERT INTO workers(id, worker_id, email)
        VALUES (7, 'PUBLIC-WORKER-7', 'worker7@example.test');
    """)

    now = monitor._now_ist()
    booking_created = now - timedelta(minutes=20)
    old_offer_time = now - timedelta(minutes=15)
    original_deadline = booking_created + timedelta(minutes=60)

    conn.execute("""
        INSERT INTO bookings (
            id, booking_id, customer_id, category, latitude, longitude,
            booking_status, status, worker_id, assigned_worker_id,
            booking_type, service_type, preferred_date, preferred_time,
            booking_date, booking_time, created_at, assigned_at
        ) VALUES (
            1, 'SIV-REOFFER-TARGET', 1, 'Cleaning', 28.6, 77.2,
            'pending', 'pending', NULL, NULL,
            'normal', 'normal', '2026-11-20', '10:00',
            '2026-11-20', '10:00', ?, NULL
        )
    """, (booking_created.isoformat(timespec="seconds"),))

    # The worker's previous active booking is now completed.
    conn.execute("""
        INSERT INTO bookings (
            id, booking_id, customer_id, category,
            booking_status, status, worker_id, assigned_worker_id,
            booking_type, service_type, created_at
        ) VALUES (
            2, 'SIV-REOFFER-OLD', 1, 'Cleaning',
            'completed', 'completed', 7, 7,
            'normal', 'normal', ?
        )
    """, (booking_created.isoformat(timespec="seconds"),))

    monitor.ensure_assignment_monitor_schema(conn)
    _ensure_worker_attempt_table(conn)

    conn.execute("""
        INSERT INTO booking_assignment_offers (
            booking_db_id, worker_id, status, offered_at,
            allocation_started_at, response_deadline, responded_at
        ) VALUES (1, 7, 'released', ?, '', ?, ?)
    """, (
        old_offer_time.isoformat(timespec="seconds"),
        original_deadline.isoformat(timespec="seconds"),
        old_offer_time.isoformat(timespec="seconds"),
    ))

    conn.execute("""
        INSERT INTO booking_worker_attempts (
            booking_id, worker_id, status, attempt_status,
            reason, rejection_reason
        ) VALUES (
            '1', 7, 'released', 'released',
            'Temporarily released while the worker completes another active booking.',
            NULL
        )
    """)
    conn.commit()

    fake_customer_routes = types.ModuleType("routes.customer")
    fake_customer_routes.get_booking_attempted_worker_ids = (
        get_booking_attempted_worker_ids
    )
    fake_customer_routes.record_worker_attempt = record_worker_attempt

    def nearest_worker(
        db, category, latitude, longitude, excluded_workers=None,
        exclude_booking_id=None, **kwargs
    ):
        excluded = {str(value) for value in (excluded_workers or set())}
        if "7" in excluded or "PUBLIC-WORKER-7" in excluded:
            return None

        # Use the real capacity helpers: a completed old booking no longer
        # blocks the worker, and the worker's own released offer is excluded.
        if _worker_has_active_booking(
            db, 7, exclude_booking_id=exclude_booking_id
        ):
            return None
        if _worker_has_schedule_conflict(
            db, 7, kwargs.get("preferred_date"), kwargs.get("preferred_time"),
            kwargs.get("mode", "normal"),
            exclude_booking_id=exclude_booking_id,
        ):
            return None

        return {
            "worker_db_id": 7,
            "worker_id": "PUBLIC-WORKER-7",
            "distance_km": 2.5,
        }

    fake_customer_routes.find_nearest_worker = nearest_worker

    monkeypatch.setitem(sys.modules, "routes.customer", fake_customer_routes)
    monkeypatch.setattr(monitor, "find_partner_for_worker", lambda *_args: None)

    try:
        booking = conn.execute(
            "SELECT * FROM bookings WHERE id=1"
        ).fetchone()

        added = monitor._discover_live_worker_offers(conn, booking, now)
        assert added == 1

        offer = conn.execute("""
            SELECT status, offered_at, response_deadline, responded_at
            FROM booking_assignment_offers
            WHERE booking_db_id=1 AND worker_id=7
        """).fetchone()

        attempt = conn.execute("""
            SELECT status, attempt_status, reason, rejection_reason,
                   distance_km
            FROM booking_worker_attempts
            WHERE booking_id='1' AND worker_id=7
        """).fetchone()

        assert offer["status"] == "offered"
        assert offer["offered_at"] == now.isoformat(timespec="seconds")
        assert offer["responded_at"] is None
        assert datetime.fromisoformat(
            offer["response_deadline"]
        ) <= original_deadline

        assert attempt["status"] == "offered"
        assert attempt["attempt_status"] == "offered"
        assert attempt["reason"] is None
        assert attempt["rejection_reason"] is None
        assert attempt["distance_km"] == 2.5

        assert get_booking_attempted_worker_ids(conn, 1) == {"7"}

        # The reactivated offer now reserves the worker for this booking,
        # rather than allowing another booking to offer the same worker.
        assert _worker_has_schedule_conflict(
            conn, 7, "2026-11-21", "10:00", "normal",
            exclude_booking_id=2,
        ) is True

        assert conn.execute("""
            SELECT COUNT(*) FROM booking_assignment_offers
            WHERE booking_db_id=1 AND worker_id=7
        """).fetchone()[0] == 1
    finally:
        conn.close()



def _partner_accept_route_test_client(tmp_path, monkeypatch):
    """Isolated Flask/SQLite fixture for partner-side booking acceptance."""
    import sqlite3
    from flask import Flask
    import routes.partner as partner_routes
    import routes.decorators as route_decorators

    database_path = tmp_path / "partner-accept-route.sqlite"
    conn = sqlite3.connect(database_path)
    conn.executescript("""
        CREATE TABLE partners (
            id INTEGER PRIMARY KEY,
            partner_id TEXT NOT NULL UNIQUE,
            is_deleted INTEGER DEFAULT 0,
            status TEXT DEFAULT 'active'
        );
        INSERT INTO partners(id, partner_id, is_deleted, status)
        VALUES (1, 'PARTNER-1', 0, 'active');

        CREATE TABLE workers (
            id INTEGER PRIMARY KEY,
            worker_id TEXT NOT NULL UNIQUE,
            fullname TEXT,
            is_deleted INTEGER DEFAULT 0,
            status TEXT DEFAULT 'active'
        );
        INSERT INTO workers(id, worker_id, fullname)
        VALUES (7, 'PUBLIC-WORKER-7', 'Worker Seven');

        CREATE TABLE partner_workers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            partner_id TEXT NOT NULL,
            worker_id TEXT NOT NULL
        );
        INSERT INTO partner_workers(partner_id, worker_id)
        VALUES ('PARTNER-1', 'PUBLIC-WORKER-7');

        CREATE TABLE bookings (
            id INTEGER PRIMARY KEY,
            booking_id TEXT UNIQUE,
            customer_id INTEGER,
            worker_id INTEGER,
            assigned_worker_id INTEGER,
            partner_id TEXT,
            assigned_partner_id TEXT,
            booking_status TEXT DEFAULT 'pending',
            status TEXT DEFAULT 'pending',
            partner_acknowledged_at TEXT,
            assigned_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT
        );

        CREATE TABLE booking_assignment_offers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            booking_db_id INTEGER NOT NULL,
            worker_id INTEGER NOT NULL,
            partner_id TEXT,
            status TEXT NOT NULL DEFAULT 'offered',
            offered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            allocation_started_at TEXT NOT NULL DEFAULT '',
            response_deadline TEXT NOT NULL DEFAULT '2099-01-01T01:00:00+05:30',
            responded_at TEXT,
            UNIQUE(booking_db_id, worker_id)
        );

        CREATE TABLE booking_worker_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            booking_id TEXT NOT NULL,
            worker_id INTEGER NOT NULL,
            distance_km REAL,
            status TEXT DEFAULT 'offered',
            attempt_status TEXT DEFAULT 'offered',
            reason TEXT,
            rejection_reason TEXT,
            attempted_at TEXT,
            created_at TEXT,
            updated_at TEXT,
            UNIQUE(booking_id, worker_id)
        );

        CREATE TABLE notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_type TEXT NOT NULL,
            user_id TEXT NOT NULL,
            title TEXT NOT NULL,
            message TEXT NOT NULL,
            is_read INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.commit()
    conn.close()

    def db_factory():
        db = sqlite3.connect(database_path, timeout=5)
        db.row_factory = sqlite3.Row
        return db

    monkeypatch.setattr(partner_routes, "get_db", db_factory)
    monkeypatch.setattr(
        partner_routes, "ensure_assignment_monitor_schema", lambda _db: None
    )
    monkeypatch.setattr(partner_routes, "ensure_finance_schema", lambda _db: None)
    monkeypatch.setattr(route_decorators, "get_db", db_factory)

    app = Flask(__name__)
    app.secret_key = "isolated-partner-accept-test-secret"
    app.config["TESTING"] = True
    app.register_blueprint(partner_routes.partner_bp)
    client = app.test_client()

    with client.session_transaction() as session:
        session["role"] = "partner"
        session["user_id"] = "PARTNER-1"

    return client, database_path


def _partner_accept_seed_booking(
    path, booking_db_id, *, status="pending", worker_id=None, partner_id=None
):
    import sqlite3

    conn = sqlite3.connect(path)
    conn.execute("""
        INSERT INTO bookings (
            id, booking_id, customer_id, worker_id, assigned_worker_id,
            partner_id, assigned_partner_id, booking_status, status
        ) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?)
    """, (
        booking_db_id, f"SIV-PARTNER-ACCEPT-{booking_db_id}",
        worker_id, worker_id, partner_id, partner_id, status, status,
    ))
    conn.commit()
    conn.close()


def test_partner_acceptance_rejects_worker_with_another_active_booking(
    tmp_path, monkeypatch
):
    import sqlite3

    client, database_path = _partner_accept_route_test_client(tmp_path, monkeypatch)
    _partner_accept_seed_booking(
        database_path, 1, status="pending", worker_id=7, partner_id="PARTNER-1"
    )
    _partner_accept_seed_booking(
        database_path, 2, status="in_progress", worker_id=7, partner_id="PARTNER-1"
    )

    conn = sqlite3.connect(database_path)
    conn.execute("""
        INSERT INTO booking_assignment_offers (booking_db_id, worker_id, status)
        VALUES (1, 7, 'offered')
    """)
    conn.commit()
    conn.close()

    response = client.post("/partner/job/1/accept")
    assert response.status_code == 302

    conn = sqlite3.connect(database_path)
    target = conn.execute(
        "SELECT booking_status, status FROM bookings WHERE id=1"
    ).fetchone()
    offer_status = conn.execute("""
        SELECT status FROM booking_assignment_offers
        WHERE booking_db_id=1 AND worker_id=7
    """).fetchone()[0]
    conn.close()

    assert target == ("pending", "pending")
    assert offer_status == "offered"


def test_partner_acceptance_releases_workers_other_offers_and_attempts(
    tmp_path, monkeypatch
):
    import sqlite3

    client, database_path = _partner_accept_route_test_client(tmp_path, monkeypatch)
    _partner_accept_seed_booking(
        database_path, 1, status="pending", worker_id=7, partner_id="PARTNER-1"
    )
    _partner_accept_seed_booking(database_path, 2, status="pending")

    conn = sqlite3.connect(database_path)
    conn.executemany("""
        INSERT INTO booking_assignment_offers (booking_db_id, worker_id, status)
        VALUES (?, ?, 'offered')
    """, [(1, 7), (1, 8), (2, 7)])
    conn.execute("""
        INSERT INTO booking_worker_attempts (
            booking_id, worker_id, status, attempt_status
        ) VALUES ('1', 7, 'offered', 'offered')
    """)
    conn.execute("""
        INSERT INTO booking_worker_attempts (
            booking_id, worker_id, status, attempt_status
        ) VALUES ('2', 7, 'offered', 'offered')
    """)
    conn.commit()
    conn.close()

    response = client.post("/partner/job/1/accept")
    assert response.status_code == 302

    conn = sqlite3.connect(database_path)
    target_status = conn.execute(
        "SELECT booking_status, status FROM bookings WHERE id=1"
    ).fetchone()
    current_offer_status = conn.execute("""
        SELECT status FROM booking_assignment_offers
        WHERE booking_db_id=1 AND worker_id=7
    """).fetchone()[0]
    same_booking_other_offer = conn.execute("""
        SELECT status FROM booking_assignment_offers
        WHERE booking_db_id=1 AND worker_id=8
    """).fetchone()[0]
    other_booking_offer = conn.execute("""
        SELECT status FROM booking_assignment_offers
        WHERE booking_db_id=2 AND worker_id=7
    """).fetchone()[0]
    attempt_status = conn.execute("""
        SELECT status, attempt_status FROM booking_worker_attempts
        WHERE booking_id='2' AND worker_id=7
    """).fetchone()
    accepted_attempt = conn.execute("""
        SELECT status, attempt_status FROM booking_worker_attempts
        WHERE booking_id='1' AND worker_id=7
    """).fetchone()
    conn.close()

    assert target_status == ("accepted", "accepted")
    assert accepted_attempt == ("accepted", "accepted")
    assert current_offer_status == "accepted"
    assert same_booking_other_offer == "cancelled"
    assert other_booking_offer == "released"
    assert attempt_status == ("released", "released")
