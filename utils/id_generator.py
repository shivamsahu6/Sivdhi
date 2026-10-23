"""Helpers for generating Sivdhi public identifiers.

The functions in this module only calculate the next identifier.
They do not insert records, so the caller remains responsible for
the transaction.

Existing callers may pass an open DB connection; when they do,
this module must not commit, rollback, or close that connection.
"""

from database import get_db


def _next_numeric_suffix(conn, table, column, prefix, start=10001):
    """Return the next numeric suffix used by public IDs in a table.

    The actual public-ID column is checked to avoid duplicate IDs
    after imports, deletes, or manually inserted records.
    """
    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT COALESCE(
            MAX(CAST(SUBSTR({column}, ?) AS INTEGER)),
            ?
        ) AS max_suffix
        FROM {table}
        WHERE {column} LIKE ?
          AND LENGTH({column}) > ?
          AND SUBSTR({column}, ?) GLOB '[0-9]*'
        """,
        (
            len(prefix) + 1,
            start - 1,
            f"{prefix}%",
            len(prefix),
            len(prefix) + 1,
        ),
    )

    row = cur.fetchone()
    maximum = int(row[0] or (start - 1))
    return maximum + 1


def _generate(conn, table, column, prefix, start=10001):
    return (
        f"{prefix}"
        f"{_next_numeric_suffix(conn, table, column, prefix, start)}"
    )


# ======================================
# Worker ID
# Format: SV10001
# ======================================

def generate_worker_id(conn=None):
    own_connection = conn is None
    db = conn or get_db()

    try:
        return _generate(
            db,
            "workers",
            "worker_id",
            "SV",
        )
    finally:
        if own_connection:
            db.close()


# ======================================
# Partner ID
# Format: SP10001
# ======================================

def generate_partner_id(conn=None):
    own_connection = conn is None
    db = conn or get_db()

    try:
        return _generate(
            db,
            "partners",
            "partner_id",
            "SP",
        )
    finally:
        if own_connection:
            db.close()


# ======================================
# Admin ID
# Format: AD10001
# ======================================

def generate_admin_id(conn=None):
    own_connection = conn is None
    db = conn or get_db()

    try:
        return _generate(
            db,
            "admin",
            "admin_id",
            "AD",
        )
    finally:
        if own_connection:
            db.close()


# ======================================
# Job ID (legacy jobs table compatibility)
# Format: JB10001
# ======================================

def generate_job_id(conn=None):
    own_connection = conn is None
    db = conn or get_db()

    try:
        return _generate(
            db,
            "jobs",
            "job_id",
            "JB",
        )
    finally:
        if own_connection:
            db.close()


# ======================================
# Partner Referral Code
# Format: RP10001
# ======================================

def generate_referral_code(conn=None):
    """Generate the next partner referral code from actual referral data.

    Referral codes use the RP prefix and are independent of partner IDs.
    """
    own_connection = conn is None
    db = conn or get_db()

    try:
        cur = db.cursor()
        cur.execute(
            """
            SELECT COALESCE(
                MAX(CAST(SUBSTR(referral_code, 3) AS INTEGER)),
                10000
            )
            FROM partners
            WHERE referral_code LIKE 'RP%'
              AND LENGTH(referral_code) > 2
              AND SUBSTR(referral_code, 3) GLOB '[0-9]*'
            """
        )

        row = cur.fetchone()
        return f"RP{int(row[0] or 10000) + 1}"

    finally:
        if own_connection:
            db.close()