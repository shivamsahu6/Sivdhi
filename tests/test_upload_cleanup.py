import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from utils import upload_cleanup


class UploadCleanupTests(unittest.TestCase):
    @staticmethod
    def profile_db():
        db = sqlite3.connect(":memory:")
        for table in ("customers", "workers", "partners"):
            db.execute(f"CREATE TABLE {table} (profile_photo TEXT)")
        return db

    @staticmethod
    def catalog_db():
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE service_categories (image_path TEXT)")
        db.execute("CREATE TABLE services (image_path TEXT)")
        return db

    def test_replaced_managed_profile_photo_is_deleted_after_reference_is_gone(self):
        old = "worker_17_0123456789abcdef.jpg"
        new = "worker_17_fedcba9876543210.webp"
        db = self.profile_db()
        try:
            with tempfile.TemporaryDirectory() as directory:
                old_file = Path(directory) / old
                old_file.write_bytes(b"old")
                with patch.object(upload_cleanup, "PROFILE_UPLOAD_DIR", Path(directory)):
                    deleted = upload_cleanup.delete_replaced_profile_photo(db, old, new)
                self.assertTrue(deleted)
                self.assertFalse(old_file.exists())
        finally:
            db.close()

    def test_profile_photo_still_used_by_another_account_is_preserved(self):
        old = "customer_9_0123456789abcdef.png"
        db = self.profile_db()
        try:
            db.execute("INSERT INTO partners(profile_photo) VALUES (?)", (old,))
            db.commit()
            with tempfile.TemporaryDirectory() as directory:
                old_file = Path(directory) / old
                old_file.write_bytes(b"shared")
                with patch.object(upload_cleanup, "PROFILE_UPLOAD_DIR", Path(directory)):
                    deleted = upload_cleanup.delete_replaced_profile_photo(
                        db, old, "customer_9_fedcba9876543210.png"
                    )
                self.assertFalse(deleted)
                self.assertTrue(old_file.exists())
        finally:
            db.close()

    def test_unknown_or_builtin_profile_filename_is_never_deleted(self):
        db = self.profile_db()
        try:
            with tempfile.TemporaryDirectory() as directory:
                for name in ("default.png", "avatar.png", "../worker_7_0123456789abcdef.jpg"):
                    candidate = Path(directory) / Path(name).name
                    candidate.write_bytes(b"keep")
                    with patch.object(upload_cleanup, "PROFILE_UPLOAD_DIR", Path(directory)):
                        deleted = upload_cleanup.delete_replaced_profile_photo(
                            db, name, "worker_7_fedcba9876543210.jpg"
                        )
                    self.assertFalse(deleted)
                    self.assertTrue(candidate.exists())
        finally:
            db.close()

    def test_same_profile_photo_is_not_deleted(self):
        old = "partner_2_0123456789abcdef.webp"
        db = self.profile_db()
        try:
            with tempfile.TemporaryDirectory() as directory:
                old_file = Path(directory) / old
                old_file.write_bytes(b"keep")
                with patch.object(upload_cleanup, "PROFILE_UPLOAD_DIR", Path(directory)):
                    deleted = upload_cleanup.delete_replaced_profile_photo(db, old, old)
                self.assertFalse(deleted)
                self.assertTrue(old_file.exists())
        finally:
            db.close()

    def test_database_reference_scan_failure_fails_closed(self):
        class BrokenDatabase:
            def execute(self, *_args, **_kwargs):
                raise sqlite3.OperationalError("database unavailable")

        old = "worker_17_0123456789abcdef.jpg"
        with tempfile.TemporaryDirectory() as directory:
            old_file = Path(directory) / old
            old_file.write_bytes(b"keep")
            with patch.object(upload_cleanup, "PROFILE_UPLOAD_DIR", Path(directory)):
                deleted = upload_cleanup.delete_replaced_profile_photo(
                    BrokenDatabase(), old, "worker_17_fedcba9876543210.jpg"
                )
            self.assertFalse(deleted)
            self.assertTrue(old_file.exists())

    def test_replaced_catalog_photo_is_deleted_when_no_record_references_it(self):
        old = "/static/uploads/catalog/0123456789abcdef0123456789abcdef.jpg"
        new = "/static/uploads/catalog/fedcba9876543210fedcba9876543210.png"
        db = self.catalog_db()
        try:
            with tempfile.TemporaryDirectory() as directory:
                old_file = Path(directory) / old.rsplit("/", 1)[1]
                old_file.write_bytes(b"old")
                with patch.object(upload_cleanup, "CATALOG_UPLOAD_DIR", Path(directory)):
                    deleted = upload_cleanup.delete_replaced_catalog_photo(db, old, new)
                self.assertTrue(deleted)
                self.assertFalse(old_file.exists())
        finally:
            db.close()

    def test_catalog_photo_shared_by_another_service_is_preserved(self):
        old = "/static/uploads/catalog/0123456789abcdef0123456789abcdef.webp"
        db = self.catalog_db()
        try:
            db.execute("INSERT INTO services(image_path) VALUES (?)", (old,))
            db.commit()
            with tempfile.TemporaryDirectory() as directory:
                old_file = Path(directory) / old.rsplit("/", 1)[1]
                old_file.write_bytes(b"shared")
                with patch.object(upload_cleanup, "CATALOG_UPLOAD_DIR", Path(directory)):
                    deleted = upload_cleanup.delete_replaced_catalog_photo(
                        db, old, "/static/uploads/catalog/fedcba9876543210fedcba9876543210.jpg"
                    )
                self.assertFalse(deleted)
                self.assertTrue(old_file.exists())
        finally:
            db.close()

    def test_default_or_external_catalog_asset_is_never_deleted(self):
        db = self.catalog_db()
        try:
            with tempfile.TemporaryDirectory() as directory:
                for old in (
                    "/static/catalog/catalog-defaults.webp",
                    "/static/catalog/home-cleaner.svg",
                    "/static/uploads/catalog/not-a-uuid.jpg",
                    "https://cdn.example.com/static/uploads/catalog/0123456789abcdef0123456789abcdef.jpg",
                ):
                    with patch.object(upload_cleanup, "CATALOG_UPLOAD_DIR", Path(directory)):
                        deleted = upload_cleanup.delete_replaced_catalog_photo(
                            db, old, ""
                        )
                    self.assertFalse(deleted)
                self.assertEqual(list(Path(directory).iterdir()), [])
        finally:
            db.close()

    def test_same_catalog_photo_is_not_deleted(self):
        old = "/static/uploads/catalog/0123456789abcdef0123456789abcdef.jpg"
        db = self.catalog_db()
        try:
            with tempfile.TemporaryDirectory() as directory:
                old_file = Path(directory) / old.rsplit("/", 1)[1]
                old_file.write_bytes(b"keep")
                with patch.object(upload_cleanup, "CATALOG_UPLOAD_DIR", Path(directory)):
                    deleted = upload_cleanup.delete_replaced_catalog_photo(db, old, old)
                self.assertFalse(deleted)
                self.assertTrue(old_file.exists())
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
