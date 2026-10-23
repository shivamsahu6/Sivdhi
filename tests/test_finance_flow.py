"""SQLite-only smoke tests for Sivdhi's internal demo settlement ledger."""
import sqlite3
import unittest

from utils.sivdhi_finance import ensure_finance_schema, settle_completed_booking


class FinanceSettlementTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE bookings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                booking_id TEXT UNIQUE NOT NULL,
                customer_id INTEGER NOT NULL DEFAULT 1,
                booking_status TEXT DEFAULT 'completed',
                status TEXT DEFAULT 'completed',
                total_amount REAL NOT NULL DEFAULT 0,
                amount REAL NOT NULL DEFAULT 0,
                worker_id INTEGER,
                assigned_worker_id INTEGER,
                partner_id TEXT,
                assigned_partner_id TEXT,
                advance_amount REAL DEFAULT 0,
                advance_paid_amount REAL DEFAULT 0,
                advance_payment_status TEXT DEFAULT 'DEMO_PAID',
                balance_payment_status TEXT DEFAULT 'DEMO_PAID',
                assigned_at TEXT,
                commission_settled_at TEXT
            );
            CREATE TABLE workers (
                id INTEGER PRIMARY KEY,
                worker_id TEXT UNIQUE,
                fullname TEXT,
                wallet REAL DEFAULT 0,
                status TEXT DEFAULT 'Active',
                is_deleted INTEGER DEFAULT 0
            );
            CREATE TABLE partners (
                id INTEGER PRIMARY KEY,
                partner_id TEXT UNIQUE,
                fullname TEXT,
                wallet REAL DEFAULT 0,
                is_deleted INTEGER DEFAULT 0
            );
            CREATE TABLE partner_workers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                partner_id TEXT,
                worker_id TEXT
            );
            CREATE TABLE wallet_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_type TEXT,
                user_id TEXT,
                amount REAL,
                transaction_type TEXT,
                description TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE admin_settings (
                setting_key TEXT PRIMARY KEY,
                setting_value TEXT
            );
        """)
        self.db.execute("INSERT INTO workers(id,worker_id,fullname,wallet) VALUES(1,'W-001','Worker One',0)")
        self.db.execute("INSERT INTO partners(id,partner_id,fullname,wallet) VALUES(1,'P-001','Partner One',0)")
        self.db.execute("INSERT INTO partner_workers(partner_id,worker_id) VALUES('P-001','W-001')")
        self.db.execute("INSERT INTO admin_settings VALUES('partner_commission_percent','10')")
        self.db.execute("INSERT INTO admin_settings VALUES('platform_commission_percent','10')")
        ensure_finance_schema(self.db)

    def tearDown(self):
        self.db.close()

    def add_booking(self, booking_id, partner_id='P-001', total=500):
        cur = self.db.execute("""
            INSERT INTO bookings(
                booking_id, booking_status, status, total_amount, amount,
                worker_id, assigned_worker_id, partner_id, assigned_partner_id,
                advance_amount, advance_paid_amount, advance_payment_status,
                balance_payment_status, assigned_at,
                partner_commission_percent, platform_commission_percent
            ) VALUES(?, 'completed', 'completed', ?, ?, 1, 1, ?, ?, 50, 50,
                     'DEMO_PAID', 'DEMO_PAID', '2026-10-12T10:00:00', 10, 10)
        """, (booking_id, total, total, partner_id, partner_id or None))
        return cur.lastrowid

    def test_split_is_correct_and_idempotent(self):
        booking_id = self.add_booking('SIV-TEST-1')
        ok, message = settle_completed_booking(self.db, booking_id)
        self.assertTrue(ok, message)
        self.assertAlmostEqual(self.db.execute("SELECT wallet FROM workers WHERE id=1").fetchone()[0], 400)
        self.assertAlmostEqual(self.db.execute("SELECT wallet FROM partners WHERE id=1").fetchone()[0], 50)
        platform = self.db.execute("""
            SELECT SUM(amount) FROM wallet_transactions
            WHERE user_type='platform' AND transaction_type='platform_commission'
        """).fetchone()[0]
        self.assertAlmostEqual(platform, 50)
        count_before = self.db.execute("SELECT COUNT(*) FROM wallet_transactions").fetchone()[0]
        ok, message = settle_completed_booking(self.db, booking_id)
        self.assertTrue(ok, message)
        count_after = self.db.execute("SELECT COUNT(*) FROM wallet_transactions").fetchone()[0]
        self.assertEqual(count_before, count_after)

    def test_no_partner_means_no_partner_commission(self):
        booking_id = self.add_booking('SIV-TEST-2', partner_id='')
        self.db.execute("DELETE FROM partner_workers")
        ok, message = settle_completed_booking(self.db, booking_id)
        self.assertTrue(ok, message)
        self.assertAlmostEqual(self.db.execute("SELECT wallet FROM workers WHERE id=1").fetchone()[0], 450)
        self.assertAlmostEqual(self.db.execute("SELECT wallet FROM partners WHERE id=1").fetchone()[0], 0)
        row = self.db.execute("SELECT partner_commission_amount, platform_commission_amount FROM bookings WHERE id=?", (booking_id,)).fetchone()
        self.assertAlmostEqual(row[0], 0)
        self.assertAlmostEqual(row[1], 50)

    def test_unpaid_booking_cannot_settle(self):
        booking_id = self.add_booking('SIV-TEST-3')
        self.db.execute("UPDATE bookings SET balance_payment_status='DUE' WHERE id=?", (booking_id,))
        ok, message = settle_completed_booking(self.db, booking_id)
        self.assertFalse(ok)
        self.assertIn('paid', message.lower())
        self.assertAlmostEqual(self.db.execute("SELECT wallet FROM workers WHERE id=1").fetchone()[0], 0)

    def test_booking_keeps_commission_snapshot_if_settings_change(self):
        booking_id = self.add_booking('SIV-TEST-SNAPSHOT')
        self.db.execute("UPDATE admin_settings SET setting_value='30' WHERE setting_key='partner_commission_percent'")
        self.db.execute("UPDATE admin_settings SET setting_value='30' WHERE setting_key='platform_commission_percent'")
        ok, message = settle_completed_booking(self.db, booking_id)
        self.assertTrue(ok, message)
        self.assertAlmostEqual(self.db.execute("SELECT wallet FROM workers WHERE id=1").fetchone()[0], 400)
        self.assertAlmostEqual(self.db.execute("SELECT wallet FROM partners WHERE id=1").fetchone()[0], 50)
        platform = self.db.execute("SELECT SUM(amount) FROM wallet_transactions WHERE user_type='platform' AND transaction_type='platform_commission'").fetchone()[0]
        self.assertAlmostEqual(platform, 50)


if __name__ == '__main__':
    unittest.main()
