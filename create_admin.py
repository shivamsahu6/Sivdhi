"""Create a Sivdhi admin account interactively without echoing passwords."""

import getpass
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

# Use this script's own .env even when invoked from another working directory.
load_dotenv(Path(__file__).resolve().parent / ".env")

from werkzeug.security import generate_password_hash

from database import create_tables, get_db


def main():
    create_tables()

    admin_id = input("Admin ID: ").strip()
    fullname = input("Full Name: ").strip()
    email = input("Email: ").strip().lower()
    password = getpass.getpass("Password (hidden): ")
    confirmation = getpass.getpass("Confirm password: ")

    if not admin_id or len(admin_id) > 64:
        print("Admin ID is required and must be 64 characters or fewer.")
        return 2
    if not fullname or len(fullname) > 100:
        print("Full name is required and must be 100 characters or fewer.")
        return 2
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email) or len(email) > 254:
        print("Please enter a valid email address.")
        return 2
    if len(password) < 12 or not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        print("Password must be at least 12 characters and include a letter and a number.")
        return 2
    if password != confirmation:
        print("Passwords do not match.")
        return 2

    conn = get_db()
    try:
        conn.execute(
            """INSERT INTO admin (admin_id, fullname, email, password, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (admin_id, fullname, email, generate_password_hash(password), datetime.now().isoformat()),
        )
        conn.commit()
        print("Admin account successfully created.")
        print("Admin ID:", admin_id)
        print("Email:", email)
        return 0
    except sqlite3.IntegrityError:
        conn.rollback()
        print("Admin account was not created: the ID or email may already be in use.")
        return 1
    except sqlite3.Error:
        conn.rollback()
        print("Admin account was not created because the database returned an error.")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
