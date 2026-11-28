import sqlite3
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from werkzeug.datastructures import FileStorage

from routes import admin
from routes.customer import catalog_photo_class, customer_service_rows, ensure_customer_schema


class CatalogImageUploadTests(unittest.TestCase):
    def test_jpeg_upload_is_saved_with_random_safe_name(self):
        jpeg_bytes = b"\xff\xd8\xff" + b"catalog-image-test"
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(admin, "CATALOG_UPLOAD_DIR", Path(directory)):
                upload = FileStorage(
                    stream=BytesIO(jpeg_bytes),
                    filename="../../untrusted/photo.JPG",
                    content_type="image/jpeg",
                )
                saved_path = admin._save_catalog_image_upload(upload)

            self.assertTrue(saved_path.startswith("/static/uploads/catalog/"))
            saved_name = Path(saved_path).name
            self.assertNotEqual(saved_name, "photo.JPG")
            self.assertTrue(saved_name.endswith(".jpg"))
            self.assertEqual((Path(directory) / saved_name).read_bytes(), jpeg_bytes)

    def test_empty_upload_returns_none(self):
        upload = FileStorage(stream=BytesIO(b""), filename="")
        self.assertIsNone(admin._save_catalog_image_upload(upload))

    def test_disallowed_extension_and_mismatched_signature_are_rejected(self):
        with self.assertRaises(ValueError):
            admin._save_catalog_image_upload(
                FileStorage(stream=BytesIO(b"<svg/>"), filename="image.svg")
            )

        with tempfile.TemporaryDirectory() as directory:
            with patch.object(admin, "CATALOG_UPLOAD_DIR", Path(directory)):
                with self.assertRaises(ValueError):
                    admin._save_catalog_image_upload(
                        FileStorage(stream=BytesIO(b"not-a-jpeg"), filename="fake.jpg")
                    )
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_upload_larger_than_three_megabytes_is_rejected(self):
        too_large = b"\xff\xd8\xff" + (b"x" * (3 * 1024 * 1024))
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(admin, "CATALOG_UPLOAD_DIR", Path(directory)):
                with self.assertRaises(ValueError):
                    admin._save_catalog_image_upload(
                        FileStorage(stream=BytesIO(too_large), filename="large.jpg")
                    )
            self.assertEqual(list(Path(directory).iterdir()), [])


class CatalogPhotoMappingTests(unittest.TestCase):
    def test_default_photo_mapping_for_category_and_subcategory_names(self):
        self.assertEqual(
            catalog_photo_class("AC Repair & Service", "category"),
            "ac-category",
        )
        self.assertEqual(
            catalog_photo_class("Appliance Repair Service", "category"),
            "appliance-category",
        )
        self.assertEqual(
            catalog_photo_class("Home Cleaner", "category"),
            "home-cleaner",
        )
        cases = {
            "Microwave Oven": "microwave",
            "Chimney": "chimney",
            "Washing Machine": "washing-machine",
            "Refrigerator": "refrigerator",
            "1 BHK": "1bhk",
            "2 BHK": "2bhk",
            "3 BHK": "3bhk",
            "Window AC": "window-ac",
            "Split AC": "split-ac",
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(catalog_photo_class(name), expected)

    def test_customer_service_rows_exposes_only_catalog_image_metadata(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        try:
            db.execute("""
                CREATE TABLE services (
                    id INTEGER PRIMARY KEY,
                    name TEXT,
                    category TEXT,
                    icon TEXT,
                    image_path TEXT,
                    description TEXT,
                    price REAL,
                    full_day_price REAL,
                    active INTEGER
                )
            """)
            db.execute("""
                INSERT INTO services (
                    id, name, category, icon, image_path, description,
                    price, full_day_price, active
                ) VALUES (1, 'Microwave Oven', 'Appliance Repair Service',
                          '✦', '/static/uploads/catalog/microwave.jpg',
                          'Microwave service', 500, 0, 1)
            """)
            row = customer_service_rows(db, "Appliance Repair Service")[0]
            self.assertEqual(
                row["image_path"],
                "/static/uploads/catalog/microwave.jpg",
            )
            self.assertEqual(row["display_image_class"], "microwave")
            self.assertNotIn("partner_commission_percent", row)
        finally:
            db.close()

    def test_customer_schema_adds_image_path_to_existing_catalog_tables(self):
        db = sqlite3.connect(":memory:")
        try:
            db.execute("""
                CREATE TABLE services (
                    id INTEGER PRIMARY KEY,
                    name TEXT,
                    category TEXT,
                    icon TEXT,
                    description TEXT,
                    price REAL,
                    active INTEGER
                )
            """)
            db.execute("""
                CREATE TABLE service_categories (
                    id INTEGER PRIMARY KEY,
                    name TEXT,
                    icon TEXT,
                    description TEXT
                )
            """)
            ensure_customer_schema(db)
            service_columns = {
                row[1] for row in db.execute("PRAGMA table_info(services)")
            }
            category_columns = {
                row[1] for row in db.execute("PRAGMA table_info(service_categories)")
            }
            self.assertIn("image_path", service_columns)
            self.assertIn("image_path", category_columns)
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
