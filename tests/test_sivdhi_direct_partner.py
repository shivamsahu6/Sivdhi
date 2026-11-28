import sqlite3

from database import ensure_sivdhi_direct_partner
from utils.sivdhi_finance import find_partner_for_worker


def make_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE partners (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            partner_id TEXT UNIQUE NOT NULL,
            referral_code TEXT UNIQUE,
            fullname TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            mobile TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            company_name TEXT,
            wallet REAL DEFAULT 0,
            status TEXT DEFAULT 'Active',
            is_deleted INTEGER DEFAULT 0
        );
        CREATE TABLE workers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            worker_id TEXT UNIQUE NOT NULL,
            referred_by TEXT DEFAULT 'SV00000',
            is_deleted INTEGER DEFAULT 0,
            wallet REAL DEFAULT 0
        );
        CREATE TABLE partner_workers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            partner_id TEXT,
            worker_id TEXT,
            joined_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE admin_settings (
            setting_key TEXT PRIMARY KEY,
            setting_value TEXT
        );
        CREATE TABLE wallet_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_type TEXT,
            user_id TEXT,
            amount REAL,
            transaction_type TEXT,
            description TEXT
        );
        CREATE TABLE withdraw_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            partner_id TEXT,
            amount REAL
        );
    """)
    return conn


def test_direct_partner_is_seeded_idempotently_and_old_workers_are_linked():
    db = make_db()
    try:
        db.execute(
            "INSERT INTO workers(worker_id,referred_by) VALUES('W-OLD','SV00000')"
        )
        db.execute(
            "INSERT INTO workers(worker_id,referred_by) VALUES('W-OTHER','SV00000')"
        )
        db.execute(
            "INSERT INTO partner_workers(partner_id,worker_id) VALUES('SP-OTHER','W-OTHER')"
        )

        assert ensure_sivdhi_direct_partner(db) is True
        assert ensure_sivdhi_direct_partner(db) is True

        direct = db.execute(
            "SELECT * FROM partners WHERE partner_id='SP00000'"
        ).fetchone()
        assert direct is not None
        assert direct["fullname"] == "Sivdhi Direct"
        assert direct["company_name"] == "Sivdhi"
        assert direct["referral_code"] == "SV00000"
        assert direct["email"] == "shivamsahu.co@gmail.com"
        assert direct["mobile"] == "8954015683"
        assert direct["is_internal"] == 1
        assert direct["wallet"] == 0
        assert direct["password"]
        assert db.execute(
            "SELECT COUNT(*) FROM partners WHERE partner_id='SP00000'"
        ).fetchone()[0] == 1

        linked = db.execute(
            "SELECT worker_id FROM partner_workers WHERE partner_id='SP00000'"
        ).fetchall()
        assert [row["worker_id"] for row in linked] == ["W-OLD"]

        default_code = db.execute(
            "SELECT setting_value FROM admin_settings "
            "WHERE setting_key='default_referral_code'"
        ).fetchone()[0]
        assert default_code == "SV00000"
    finally:
        db.close()


def test_new_default_referral_worker_is_linked_automatically():
    db = make_db()
    try:
        assert ensure_sivdhi_direct_partner(db) is True
        db.execute(
            "INSERT INTO workers(worker_id,referred_by) VALUES('W-NEW','SV00000')"
        )
        mapping = db.execute(
            "SELECT partner_id FROM partner_workers WHERE worker_id='W-NEW'"
        ).fetchone()
        assert mapping["partner_id"] == "SP00000"
    finally:
        db.close()


def test_direct_partner_cannot_accumulate_wallet_or_withdrawal_ledger():
    db = make_db()
    try:
        assert ensure_sivdhi_direct_partner(db) is True
        db.execute(
            "UPDATE partners SET wallet=250 WHERE partner_id='SP00000'"
        )
        assert db.execute(
            "SELECT wallet FROM partners WHERE partner_id='SP00000'"
        ).fetchone()[0] == 0

        db.execute("""
            INSERT INTO wallet_transactions(
                user_type,user_id,amount,transaction_type,description
            ) VALUES('partner','SP00000',250,'referral_bonus','Referral bonus')
        """)
        assert db.execute(
            "SELECT COUNT(*) FROM wallet_transactions WHERE user_id='SP00000'"
        ).fetchone()[0] == 0

        db.execute(
            "INSERT INTO withdraw_requests(partner_id,amount) VALUES('SP00000',100)"
        )
        assert db.execute(
            "SELECT COUNT(*) FROM withdraw_requests WHERE partner_id='SP00000'"
        ).fetchone()[0] == 0

        try:
            db.execute(
                "UPDATE partners SET status='suspended' WHERE partner_id='SP00000'"
            )
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("Sivdhi Direct must not be suspendable")

        try:
            db.execute(
                "DELETE FROM partners WHERE partner_id='SP00000'"
            )
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("Sivdhi Direct must not be deletable")
    finally:
        db.close()


def test_referral_lookup_repairs_a_missing_direct_worker_mapping():
    db = make_db()
    try:
        db.execute(
            "INSERT INTO workers(worker_id,referred_by) VALUES('W-UNLINKED','SV00000')"
        )
        assert ensure_sivdhi_direct_partner(db) is True
        db.execute("DELETE FROM partner_workers WHERE worker_id='W-UNLINKED'")

        worker = db.execute(
            "SELECT id FROM workers WHERE worker_id='W-UNLINKED'"
        ).fetchone()
        partner_id = find_partner_for_worker(db, worker["id"])

        assert partner_id == "SP00000"
        mapping = db.execute(
            "SELECT partner_id FROM partner_workers WHERE worker_id='W-UNLINKED'"
        ).fetchone()
        assert mapping["partner_id"] == "SP00000"
    finally:
        db.close()


def test_direct_partner_bootstrap_does_not_take_over_an_existing_referral_owner():
    db = make_db()
    try:
        db.execute("""
            INSERT INTO partners(
                partner_id,referral_code,fullname,email,mobile,password
            ) VALUES(
                'SP-EXISTING','SV00000','Existing Partner',
                'other@example.com','9999999999','already-hashed'
            )
        """)
        assert ensure_sivdhi_direct_partner(db) is False
        assert db.execute(
            "SELECT COUNT(*) FROM partners WHERE partner_id='SP00000'"
        ).fetchone()[0] == 0
        assert db.execute(
            "SELECT referral_code FROM partners WHERE partner_id='SP-EXISTING'"
        ).fetchone()[0] == "SV00000"
    finally:
        db.close()
