import os
import sqlite3
from pathlib import Path
from config import Config


# ==========================================================
# DATABASE CONNECTION
# ==========================================================

def get_db():
    """Open a properly configured SQLite connection.

    A finite busy timeout reduces transient "database is locked" failures
    when multiple requests write at once. SQLite still permits only one
    writer at a time; use a client/server database for high-write scale.
    """
    database_path = Path(Config.DATABASE).expanduser().resolve()
    # Keep the database outside static/uploads by default and create only the
    # parent directory needed by an explicitly configured DATABASE_PATH.
    database_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(database_path),
        timeout=Config.SQLITE_TIMEOUT_SECONDS,
        check_same_thread=True,
    )
    try:
        # Best-effort owner-only permissions on POSIX. Windows permission
        # semantics differ; filesystem ACLs must be configured by the operator.
        os.chmod(database_path, 0o600)
    except OSError:
        pass
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {int(Config.SQLITE_BUSY_TIMEOUT_MS)}")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA temp_store = MEMORY")
    return conn


# ==========================================================
# HELPERS
# ==========================================================

def table_exists(cursor, table):
    cursor.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table' AND name = ?
        """,
        (table,)
    )

    return cursor.fetchone() is not None


def get_table_columns(cursor, table):
    cursor.execute(f"PRAGMA table_info({table})")
    return [row[1] for row in cursor.fetchall()]


def add_column_if_not_exists(cursor, table, column, column_type):

    if not table_exists(cursor, table):
        return

    columns = get_table_columns(cursor, table)

    if column not in columns:

        cursor.execute(
            f"ALTER TABLE {table} "
            f"ADD COLUMN {column} {column_type}"
        )


# ==========================================================
# CREATE TABLES
# ==========================================================

def create_tables():

    conn = get_db()
    cur = conn.cursor()

    try:
        # WAL allows readers to continue while a writer commits. It improves
        # local concurrent access but does not turn SQLite into a multi-writer
        # distributed database. A short timeout above handles brief contention.
        try:
            cur.execute("PRAGMA journal_mode = WAL")
            cur.execute("PRAGMA synchronous = NORMAL")
        except sqlite3.Error as pragma_error:
            print(f"SQLite performance setting warning: {pragma_error}")

        # Hold the write lock while checking and applying additive migrations,
        # so multiple WSGI workers starting together do not race ALTER TABLE.
        cur.execute("BEGIN IMMEDIATE")

        # ==================================================
        # WORKERS
        # ==================================================

        cur.execute("""
            CREATE TABLE IF NOT EXISTS workers (

                id INTEGER PRIMARY KEY AUTOINCREMENT,

                worker_id TEXT UNIQUE NOT NULL,

                fullname TEXT NOT NULL,

                email TEXT UNIQUE NOT NULL,

                mobile TEXT UNIQUE NOT NULL,

                password TEXT NOT NULL,

                profile_photo TEXT,

                address TEXT,

                city TEXT,

                state TEXT,

                pincode TEXT,

                skills TEXT,

                experience TEXT,

                aadhar_no TEXT,

                referral_code TEXT,

                referred_by TEXT DEFAULT 'SV00000',

                wallet REAL DEFAULT 0,

                rating REAL DEFAULT 0,

                status TEXT DEFAULT 'Active',

                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

                bank_name TEXT,

                account_holder TEXT,

                account_number TEXT,

                ifsc TEXT,

                app_notification INTEGER DEFAULT 1,

                email_notification INTEGER DEFAULT 1,

                job_notification INTEGER DEFAULT 1,

                payment_notification INTEGER DEFAULT 1,

                promo_notification INTEGER DEFAULT 0,

                language TEXT DEFAULT 'English',

                is_deleted INTEGER DEFAULT 0,

                latitude REAL,

                longitude REAL
            )
        """)


        # ==================================================
        # BOOKING WORKER ATTEMPTS
        # ==================================================

        cur.execute("""
            CREATE TABLE IF NOT EXISTS booking_worker_attempts (

                id INTEGER PRIMARY KEY AUTOINCREMENT,

                booking_id INTEGER NOT NULL,

                worker_id INTEGER NOT NULL,

                distance_km REAL,

                attempt_status TEXT DEFAULT 'assigned',

                rejection_reason TEXT,

                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)


        # ==================================================
        # PARTNERS
        # ==================================================

        cur.execute("""
            CREATE TABLE IF NOT EXISTS partners (

                id INTEGER PRIMARY KEY AUTOINCREMENT,

                partner_id TEXT UNIQUE NOT NULL,

                referral_code TEXT UNIQUE,

                fullname TEXT NOT NULL,

                email TEXT UNIQUE NOT NULL,

                mobile TEXT UNIQUE NOT NULL,

                password TEXT NOT NULL,

                company_name TEXT,

                profile_photo TEXT,

                address TEXT,

                city TEXT,

                state TEXT,

                pincode TEXT,

                wallet REAL DEFAULT 0,

                bank_name TEXT,

                account_holder TEXT,

                account_number TEXT,

                ifsc TEXT,

                rating REAL DEFAULT 0,

                status TEXT DEFAULT 'Active',

                dark_mode INTEGER DEFAULT 0,

                language TEXT DEFAULT 'English',

                app_notification INTEGER DEFAULT 1,

                email_notification INTEGER DEFAULT 1,

                job_notification INTEGER DEFAULT 1,

                withdraw_notification INTEGER DEFAULT 1,

                promo_notification INTEGER DEFAULT 0,

                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

                is_deleted INTEGER DEFAULT 0
            )
        """)


        # ==================================================
        # JOBS
        # ==================================================

        cur.execute("""
            CREATE TABLE IF NOT EXISTS jobs (

                id INTEGER PRIMARY KEY AUTOINCREMENT,

                job_id TEXT UNIQUE NOT NULL,

                customer_name TEXT NOT NULL,

                customer_mobile TEXT NOT NULL,

                customer_address TEXT NOT NULL,

                service TEXT NOT NULL,

                description TEXT,

                booking_date TEXT,

                booking_time TEXT,

                amount REAL DEFAULT 0,

                partner_id TEXT,

                worker_id TEXT,

                job_status TEXT DEFAULT 'Pending',

                payment_status TEXT DEFAULT 'Pending',

                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)


        # ==================================================
        # JOB REQUESTS
        # ==================================================

        cur.execute("""
            CREATE TABLE IF NOT EXISTS job_requests (

                id INTEGER PRIMARY KEY AUTOINCREMENT,

                job_id TEXT NOT NULL,

                partner_id TEXT NOT NULL,

                worker_id TEXT NOT NULL,

                request_status TEXT DEFAULT 'Pending',

                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)


        # ==================================================
        # NOTIFICATIONS
        # ==================================================

        cur.execute("""
            CREATE TABLE IF NOT EXISTS notifications (

                id INTEGER PRIMARY KEY AUTOINCREMENT,

                user_type TEXT,

                user_id TEXT,

                title TEXT,

                message TEXT,

                is_read INTEGER DEFAULT 0,

                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)


        # ==================================================
        # WALLET TRANSACTIONS
        # ==================================================

        cur.execute("""
            CREATE TABLE IF NOT EXISTS wallet_transactions (

                id INTEGER PRIMARY KEY AUTOINCREMENT,

                user_type TEXT,

                user_id TEXT,

                amount REAL,

                transaction_type TEXT,

                description TEXT,

                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)


        # ==================================================
        # PARTNER WORKERS
        # ==================================================

        cur.execute("""
            CREATE TABLE IF NOT EXISTS partner_workers (

                id INTEGER PRIMARY KEY AUTOINCREMENT,

                partner_id TEXT,

                worker_id TEXT,

                joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)


        # ==================================================
        # ADMIN
        # ==================================================

        cur.execute("""
            CREATE TABLE IF NOT EXISTS admin (

                id INTEGER PRIMARY KEY AUTOINCREMENT,

                admin_id TEXT UNIQUE NOT NULL,

                fullname TEXT NOT NULL,

                email TEXT UNIQUE NOT NULL,

                password TEXT NOT NULL,

                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)


        # ==================================================
        # WITHDRAW REQUESTS
        # ==================================================

        cur.execute("""
            CREATE TABLE IF NOT EXISTS withdraw_requests (

                id INTEGER PRIMARY KEY AUTOINCREMENT,

                partner_id TEXT NOT NULL,

                amount REAL NOT NULL,

                bank_name TEXT NOT NULL,

                account_holder TEXT,

                account_number TEXT NOT NULL,

                ifsc TEXT NOT NULL,

                status TEXT DEFAULT 'Pending',

                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)


        # ==================================================
        # PARTNER BANK
        # ==================================================

        cur.execute("""
            CREATE TABLE IF NOT EXISTS partner_bank (

                id INTEGER PRIMARY KEY AUTOINCREMENT,

                partner_id TEXT UNIQUE NOT NULL,

                account_holder TEXT NOT NULL,

                bank_name TEXT NOT NULL,

                account_number TEXT NOT NULL,

                ifsc TEXT NOT NULL,

                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

                FOREIGN KEY (partner_id)
                REFERENCES partners(partner_id)
            )
        """)


        # ==================================================
        # APP RATINGS
        # ==================================================

        cur.execute("""
            CREATE TABLE IF NOT EXISTS app_ratings (

                id INTEGER PRIMARY KEY AUTOINCREMENT,

                partner_id TEXT NOT NULL,

                rating INTEGER NOT NULL,

                feedback TEXT,

                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)


        # ==================================================
        # WORKER RATINGS
        # ==================================================

        cur.execute("""
            CREATE TABLE IF NOT EXISTS worker_ratings (

                id INTEGER PRIMARY KEY AUTOINCREMENT,

                worker_id TEXT NOT NULL,

                rating INTEGER NOT NULL,

                feedback TEXT,

                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)


        # ==================================================
        # CUSTOMERS
        # ==================================================

        cur.execute("""
            CREATE TABLE IF NOT EXISTS customers (

                id INTEGER PRIMARY KEY AUTOINCREMENT,

                customer_id TEXT UNIQUE NOT NULL,

                fullname TEXT NOT NULL,

                mobile TEXT UNIQUE NOT NULL,

                email TEXT UNIQUE NOT NULL,

                password TEXT NOT NULL,

                email_verified INTEGER DEFAULT 0,

                profile_photo TEXT,

                address TEXT,

                city TEXT,

                state TEXT,

                pincode TEXT,

                latitude REAL,

                longitude REAL,

                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)


        # ==================================================
        # CUSTOMER OTP
        # ==================================================

        cur.execute("""
            CREATE TABLE IF NOT EXISTS customer_otps (

                id INTEGER PRIMARY KEY AUTOINCREMENT,

                email TEXT NOT NULL,

                otp TEXT NOT NULL,

                purpose TEXT NOT NULL,

                expires_at TEXT NOT NULL,

                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)


        # ==================================================
        # SERVICES
        # ==================================================

        cur.execute("""
            CREATE TABLE IF NOT EXISTS services (

                id INTEGER PRIMARY KEY AUTOINCREMENT,

                name TEXT UNIQUE NOT NULL,

                icon TEXT,

                description TEXT,

                active INTEGER DEFAULT 1,

                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)


        # ==================================================
        # BOOKINGS
        # ==================================================

        cur.execute("""
            CREATE TABLE IF NOT EXISTS bookings (

                id INTEGER PRIMARY KEY AUTOINCREMENT,

                booking_id TEXT UNIQUE NOT NULL,

                customer_id INTEGER NOT NULL,

                service_id INTEGER,

                category TEXT NOT NULL,

                description TEXT,

                address TEXT NOT NULL,

                city TEXT,

                state TEXT,

                pincode TEXT,

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

                cancellation_reason TEXT,

                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

                FOREIGN KEY (customer_id)
                REFERENCES customers(id),

                FOREIGN KEY (service_id)
                REFERENCES services(id),

                FOREIGN KEY (worker_id)
                REFERENCES workers(id)
            )
        """)


        # ==================================================
        # REVIEWS
        # ==================================================

        cur.execute("""
            CREATE TABLE IF NOT EXISTS reviews (

                id INTEGER PRIMARY KEY AUTOINCREMENT,

                booking_id INTEGER UNIQUE NOT NULL,

                customer_id INTEGER NOT NULL,

                worker_id INTEGER,

                rating INTEGER NOT NULL,

                review TEXT,

                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

                FOREIGN KEY (booking_id)
                REFERENCES bookings(id),

                FOREIGN KEY (customer_id)
                REFERENCES customers(id),

                FOREIGN KEY (worker_id)
                REFERENCES workers(id)
            )
        """)


        # ==================================================
        # UPDATE OLD WORKERS TABLE
        # ==================================================

        worker_columns = [

            ("bank_name", "TEXT"),
            ("account_holder", "TEXT"),
            ("account_number", "TEXT"),
            ("ifsc", "TEXT"),

            ("app_notification", "INTEGER DEFAULT 1"),
            ("email_notification", "INTEGER DEFAULT 1"),
            ("job_notification", "INTEGER DEFAULT 1"),
            ("payment_notification", "INTEGER DEFAULT 1"),
            ("promo_notification", "INTEGER DEFAULT 0"),

            ("language", "TEXT DEFAULT 'English'"),
            ("is_deleted", "INTEGER DEFAULT 0"),

            ("latitude", "REAL"),
            ("longitude", "REAL"),
            ("service_radius_km", "REAL DEFAULT 10"),
            ("work_type", "TEXT DEFAULT 'both'"),
            ("last_seen_at", "TEXT"),
            ("is_online", "INTEGER DEFAULT 0")
        ]

        for column, column_type in worker_columns:

            add_column_if_not_exists(
                cur,
                "workers",
                column,
                column_type
            )


        # ==================================================
        # UPDATE OLD PARTNERS TABLE
        # ==================================================

        partner_columns = [

            ("bank_name", "TEXT"),
            ("account_holder", "TEXT"),
            ("account_number", "TEXT"),
            ("ifsc", "TEXT"),

            ("dark_mode", "INTEGER DEFAULT 0"),
            ("language", "TEXT DEFAULT 'English'"),

            ("app_notification", "INTEGER DEFAULT 1"),
            ("email_notification", "INTEGER DEFAULT 1"),
            ("job_notification", "INTEGER DEFAULT 1"),
            ("withdraw_notification", "INTEGER DEFAULT 1"),
            ("promo_notification", "INTEGER DEFAULT 0"),

            ("is_deleted", "INTEGER DEFAULT 0")
        ]

        for column, column_type in partner_columns:

            add_column_if_not_exists(
                cur,
                "partners",
                column,
                column_type
            )


        # ==================================================
        # UPDATE OLD CUSTOMERS TABLE
        # ==================================================

        customer_columns = [

            ("email_verified", "INTEGER DEFAULT 0"),

            ("profile_photo", "TEXT"),

            ("address", "TEXT"),

            ("city", "TEXT"),

            ("state", "TEXT"),

            ("pincode", "TEXT"),

            ("latitude", "REAL"),

            ("longitude", "REAL")
        ]

        for column, column_type in customer_columns:

            add_column_if_not_exists(
                cur,
                "customers",
                column,
                column_type
            )

        # Customer preferences used by the customer settings pages.
        customer_preference_columns = [
            ("language", "TEXT DEFAULT 'English'"),
            ("app_notification", "INTEGER DEFAULT 1"),
            ("email_notification", "INTEGER DEFAULT 1"),
            ("promo_notification", "INTEGER DEFAULT 0")
        ]

        for column, column_type in customer_preference_columns:
            add_column_if_not_exists(
                cur,
                "customers",
                column,
                column_type
            )


        # ==================================================
        # UPDATE OLD SERVICES TABLE
        # IMPORTANT: BEFORE INSERTING DEFAULT SERVICES
        # ==================================================

        service_columns = [

            ("description", "TEXT"),

            ("active", "INTEGER DEFAULT 1"),

            ("created_at", "TIMESTAMP")
        ]

        for column, column_type in service_columns:

            add_column_if_not_exists(
                cur,
                "services",
                column,
                column_type
            )

        for column, column_type in [
            ("status", "TEXT DEFAULT 'Active'"),
            ("price", "REAL DEFAULT 0"),
            ("commission", "REAL DEFAULT 0")
        ]:
            add_column_if_not_exists(cur, "services", column, column_type)


        # ==================================================
        # UPDATE OLD BOOKINGS TABLE
        # ==================================================

        booking_columns = [

            # Basic booking information
            ("booking_id", "TEXT"),
            ("customer_id", "INTEGER"),
            ("service_id", "INTEGER"),
            ("category", "TEXT"),
            ("description", "TEXT"),

            # Customer location
            ("address", "TEXT"),
            ("city", "TEXT"),
            ("state", "TEXT"),
            ("pincode", "TEXT"),
            ("latitude", "REAL"),
            ("longitude", "REAL"),

            # Preferred service schedule
            ("preferred_date", "TEXT"),
            ("preferred_time", "TEXT"),

            # Charges
            ("service_charge", "REAL DEFAULT 0"),
            ("platform_fee", "REAL DEFAULT 0"),
            ("total_amount", "REAL DEFAULT 0"),

            # Payment
            ("payment_method", "TEXT"),
            ("payment_id", "TEXT"),
            ("payment_status", "TEXT DEFAULT 'pending'"),

            # Assignment and booking status
            ("worker_id", "INTEGER"),
            ("booking_status", "TEXT DEFAULT 'pending'"),
            ("cancellation_reason", "TEXT"),

            # Timestamps
            ("created_at", "TIMESTAMP"),
            ("updated_at", "TIMESTAMP"),
            ("service_type", "TEXT"),
            ("quantity", "INTEGER"),
            ("status", "TEXT DEFAULT 'pending'"),
            ("assigned_worker_id", "INTEGER"),
            ("assigned_partner_id", "TEXT"),
            ("priority", "TEXT DEFAULT 'normal'"),
            ("service", "TEXT"),
            ("notes", "TEXT"),
            ("amount", "REAL DEFAULT 0"),

            # Partner assignment compatibility. Older database versions
            # used only assigned_partner_id, while admin/assignment flows
            # also read partner_id. Keep both fields available.
            ("partner_id", "TEXT")
        ]

        for column, column_type in booking_columns:

            add_column_if_not_exists(
                cur,
                "bookings",
                column,
                column_type
            )


        # ==================================================
        # NORMALIZE LEGACY BOOKING IDENTIFIERS
        # ==================================================
        # Older builds stored public customer/worker identifiers
        # in fields that the current application treats as internal DB ids.
        # SQLite may silently compare these different representations, but
        # that creates inconsistent data and breaks joins on some records.
        # Normalize only values that can be mapped unambiguously; never
        # invent an id and never delete an unmatched legacy booking.
        try:
            cur.execute("""
                UPDATE bookings
                SET customer_id = (
                    SELECT c.id
                    FROM customers c
                    WHERE CAST(c.id AS TEXT) = TRIM(CAST(bookings.customer_id AS TEXT))
                       OR CAST(c.customer_id AS TEXT) = TRIM(CAST(bookings.customer_id AS TEXT))
                    LIMIT 1
                )
                WHERE customer_id IS NOT NULL
                  AND TRIM(CAST(customer_id AS TEXT)) != ''
                  AND EXISTS (
                    SELECT 1
                    FROM customers c
                    WHERE CAST(c.id AS TEXT) = TRIM(CAST(bookings.customer_id AS TEXT))
                       OR CAST(c.customer_id AS TEXT) = TRIM(CAST(bookings.customer_id AS TEXT))
                  )
            """)

            # payments used the same mixed representation in older builds.
            if table_exists(cur, "payments"):
                cur.execute("""
                    UPDATE payments
                    SET customer_id = (
                        SELECT c.id
                        FROM customers c
                        WHERE CAST(c.id AS TEXT) = TRIM(CAST(payments.customer_id AS TEXT))
                           OR CAST(c.customer_id AS TEXT) = TRIM(CAST(payments.customer_id AS TEXT))
                        LIMIT 1
                    )
                    WHERE customer_id IS NOT NULL
                      AND EXISTS (
                        SELECT 1
                        FROM customers c
                        WHERE CAST(c.id AS TEXT) = TRIM(CAST(payments.customer_id AS TEXT))
                           OR CAST(c.customer_id AS TEXT) = TRIM(CAST(payments.customer_id AS TEXT))
                      )
                """)

        except sqlite3.Error as error:
            print("CUSTOMER ID MIGRATION WARNING:", error)


        # ==================================================
        # NORMALIZE BOOKING ASSIGNMENTS
        # ==================================================
        # Some older records stored a public worker code in assigned_worker_id
        # while the worker-facing code
        # correctly uses workers.id in bookings.worker_id. Reconcile the
        # two fields during migration so existing assigned bookings do not
        # disappear from the worker dashboard/jobs page.
        try:
            cur.execute("""
                UPDATE bookings
                SET worker_id = (
                    SELECT w.id
                    FROM workers w
                    WHERE CAST(w.worker_id AS TEXT) =
                          CAST(bookings.assigned_worker_id AS TEXT)
                    LIMIT 1
                )
                WHERE worker_id IS NULL
                  AND assigned_worker_id IS NOT NULL
                  AND EXISTS (
                      SELECT 1
                      FROM workers w
                      WHERE CAST(w.worker_id AS TEXT) =
                            CAST(bookings.assigned_worker_id AS TEXT)
                  )
            """)

            # If an old row stored the numeric workers.id in
            # assigned_worker_id, support that representation as well.
            cur.execute("""
                UPDATE bookings
                SET worker_id = (
                    SELECT w.id
                    FROM workers w
                    WHERE CAST(w.id AS TEXT) =
                          CAST(bookings.assigned_worker_id AS TEXT)
                    LIMIT 1
                )
                WHERE worker_id IS NULL
                  AND assigned_worker_id IS NOT NULL
                  AND EXISTS (
                      SELECT 1
                      FROM workers w
                      WHERE CAST(w.id AS TEXT) =
                            CAST(bookings.assigned_worker_id AS TEXT)
                  )
            """)

            # Keep assigned_worker_id in the same canonical representation
            # as worker_id (the internal workers.id) whenever an assignment
            # exists. This does not change the public worker_id column.
            cur.execute("""
                UPDATE bookings
                SET assigned_worker_id = worker_id
                WHERE worker_id IS NOT NULL
                  AND (
                      assigned_worker_id IS NULL
                      OR NOT EXISTS (
                          SELECT 1
                          FROM workers w
                          WHERE CAST(w.id AS TEXT) =
                                CAST(bookings.assigned_worker_id AS TEXT)
                            AND w.id = bookings.worker_id
                      )
                  )
            """)

            # Partner assignment has the same compatibility requirement:
            # admin pages can use partner_id while older records may contain
            # only assigned_partner_id. Preserve both values.
            cur.execute("""
                UPDATE bookings
                SET partner_id = assigned_partner_id
                WHERE (partner_id IS NULL OR TRIM(CAST(partner_id AS TEXT)) = '')
                  AND assigned_partner_id IS NOT NULL
                  AND TRIM(CAST(assigned_partner_id AS TEXT)) != ''
            """)

            cur.execute("""
                UPDATE bookings
                SET assigned_partner_id = partner_id
                WHERE (assigned_partner_id IS NULL
                       OR TRIM(CAST(assigned_partner_id AS TEXT)) = '')
                  AND partner_id IS NOT NULL
                  AND TRIM(CAST(partner_id AS TEXT)) != ''
            """)

        except sqlite3.Error as error:
            # Migration compatibility must never prevent an otherwise valid
            # database from starting. The main schema changes above remain
            # transactional and the error is visible in the server log.
            print("BOOKING ASSIGNMENT MIGRATION WARNING:", error)


        # ==================================================
        # PAYMENT RECORDS
        # ==================================================

        cur.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id INTEGER NOT NULL,
                booking_id INTEGER,
                amount REAL NOT NULL DEFAULT 0,
                payment_method TEXT,
                transaction_id TEXT,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (customer_id) REFERENCES customers(id),
                FOREIGN KEY (booking_id) REFERENCES bookings(id)
            )
        """)

        # Admin/settings tables are created here as well so a fresh install
        # has the same schema expected by the admin and signup flows.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS admin_settings (
                setting_key TEXT PRIMARY KEY,
                setting_value TEXT
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS service_categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                price REAL NOT NULL DEFAULT 0,
                commission REAL NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS referral_offers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                partner_id TEXT NOT NULL,
                referral_code TEXT NOT NULL,
                starts_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS customer_app_ratings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id INTEGER NOT NULL,
                rating INTEGER NOT NULL,
                feedback TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (customer_id) REFERENCES customers(id)
            )
        """)

        # Global throttling schemas are provisioned before serving traffic so
        # first-login requests across multiple WSGI workers don't race DDL.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS login_attempts (
                attempt_key TEXT PRIMARY KEY,
                attempts INTEGER NOT NULL DEFAULT 0,
                window_started INTEGER NOT NULL,
                locked_until INTEGER NOT NULL DEFAULT 0
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_login_attempts_locked_until ON login_attempts(locked_until)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_login_attempts_window_started ON login_attempts(window_started)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS otp_requests (
                request_key TEXT PRIMARY KEY,
                request_count INTEGER NOT NULL DEFAULT 0,
                window_started INTEGER NOT NULL,
                last_sent INTEGER NOT NULL
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_otp_requests_window ON otp_requests(window_started)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS pending_security_changes (
                token_hash TEXT PRIMARY KEY,
                user_role TEXT NOT NULL,
                user_id TEXT NOT NULL,
                purpose TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                otp_hash TEXT NOT NULL,
                expires_at INTEGER NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                last_sent INTEGER NOT NULL,
                UNIQUE(user_role, user_id, purpose)
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_pending_security_expires ON pending_security_changes(expires_at)")
        # Prune expired throttling/pending-verification rows during startup so
        # stale one-off IPs and abandoned OTP sessions do not grow indefinitely.
        cur.execute("DELETE FROM login_attempts WHERE window_started < CAST(strftime('%s','now') AS INTEGER) - 900")
        cur.execute("DELETE FROM otp_requests WHERE window_started < CAST(strftime('%s','now') AS INTEGER) - 3600")
        cur.execute("DELETE FROM pending_security_changes WHERE expires_at <= CAST(strftime('%s','now') AS INTEGER)")

        # ==================================================
        # DEFAULT SERVICES
        # IMPORTANT: THIS MUST COME AFTER SERVICE UPDATE
        # ==================================================

        default_services = [

            (
                "Electrician",
                "⚡",
                "Electrical repair and installation"
            ),

            (
                "Plumber",
                "🔧",
                "Plumbing and water related services"
            ),

            (
                "Carpenter",
                "🪚",
                "Furniture and wood work services"
            ),

            (
                "Painter",
                "🎨",
                "Home and office painting services"
            ),

            (
                "AC Repair",
                "❄️",
                "Air conditioner repair and service"
            ),

            (
                "Appliance Repair",
                "🛠️",
                "Home appliance repair services"
            ),

            (
                "Cleaning",
                "🧹",
                "Home and office cleaning services"
            ),

            (
                "Gardening",
                "🌿",
                "Garden maintenance services"
            )
        ]


        for service in default_services:

            cur.execute(
                """
                INSERT OR IGNORE INTO services
                (
                    name,
                    icon,
                    description
                )
                VALUES (?, ?, ?)
                """,
                service
            )


        # ==================================================
        # PERFORMANCE INDEXES
        # ==================================================

        indexes = [
            ("idx_workers_location_status", "workers", "(status, is_deleted, latitude, longitude)"),
            ("idx_workers_public_id", "workers", "(worker_id)"),
            ("idx_partners_public_id", "partners", "(partner_id)"),
            ("idx_bookings_customer", "bookings", "(customer_id, id DESC)"),
            ("idx_bookings_worker_status", "bookings", "(worker_id, booking_status, id DESC)"),
            ("idx_bookings_assigned_worker", "bookings", "(assigned_worker_id, booking_status, id DESC)"),
            ("idx_bookings_partner_status", "bookings", "(partner_id, booking_status, id DESC)"),
            ("idx_bookings_assigned_partner", "bookings", "(assigned_partner_id, booking_status, id DESC)"),
            ("idx_bookings_status", "bookings", "(booking_status, id DESC)"),
            ("idx_bookings_payment", "bookings", "(payment_status, id DESC)"),
            ("idx_notifications_user", "notifications", "(user_type, user_id, is_read, id DESC)"),
            ("idx_wallet_user", "wallet_transactions", "(user_type, user_id, id DESC)"),
            ("idx_withdraw_partner_status", "withdraw_requests", "(partner_id, status, id DESC)"),
            ("idx_partner_workers_partner", "partner_workers", "(partner_id, id DESC)"),
            ("idx_partner_workers_worker", "partner_workers", "(worker_id, id DESC)"),
            ("idx_job_requests_worker", "job_requests", "(worker_id, request_status, id DESC)"),
            ("idx_job_requests_job", "job_requests", "(job_id, request_status, id DESC)"),
            ("idx_jobs_partner_status", "jobs", "(partner_id, job_status, id DESC)"),
            ("idx_jobs_worker_status", "jobs", "(worker_id, job_status, id DESC)")
        ]

        for index_name, table_name, columns_sql in indexes:
            try:
                cur.execute(
                    f"CREATE INDEX IF NOT EXISTS {index_name} "
                    f"ON {table_name} {columns_sql}"
                )
            except sqlite3.Error as error:
                print(f"Index creation skipped for {index_name}: {error}")


        # ==================================================
        # SIVDHI PAYMENT / COMMISSION MIGRATIONS
        # ==================================================
        # Additive only: preserve existing booking and wallet data.
        from utils.sivdhi_finance import ensure_finance_schema
        ensure_finance_schema(conn)

        # ==================================================
        # BRAND MIGRATION: RESERVED SIGNUP REFERRAL CODE
        # ==================================================
        # This is a reserved fallback code, not a real partner referral code.
        # Keep existing attribution while changing only the old fallback value.
        try:
            if table_exists(cur, "admin_settings"):
                cur.execute("""
                    UPDATE admin_settings SET setting_value='SV00000'
                    WHERE setting_key='default_referral_code'
                      AND UPPER(TRIM(setting_value))='WM00000'
                """)
            if table_exists(cur, "workers"):
                cur.execute("""
                    UPDATE workers SET referred_by='SV00000'
                    WHERE UPPER(TRIM(COALESCE(referred_by,'')))='WM00000'
                """)
        except sqlite3.Error as brand_migration_error:
            print(f"Reserved referral-code migration warning: {brand_migration_error}")

        # ==================================================
        # SAVE DATABASE
        # ==================================================

        conn.commit()

        print(
            "Database tables created/updated successfully."
        )


    except Exception as error:

        conn.rollback()

        print(
            "Database error:",
            error
        )

        raise


    finally:

        conn.close()


# ==========================================================
# RUN DATABASE SETUP
# ==========================================================

if __name__ == "__main__":

    create_tables()
