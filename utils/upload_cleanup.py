"""Safe cleanup for replaced, application-managed image uploads.

Only files created by Sivdhi's current upload naming schemes are eligible.
Shared files, built-in/static assets, unknown legacy filenames and paths
outside the managed upload folders are deliberately left untouched.
"""
import logging
import re
from pathlib import Path
from urllib.parse import urlsplit


_PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROFILE_UPLOAD_DIR = _PROJECT_ROOT / "static" / "uploads"
CATALOG_UPLOAD_DIR = PROFILE_UPLOAD_DIR / "catalog"

_PROFILE_FILENAME_RE = re.compile(
    r"^(?:customer|worker|partner)_\d+_[0-9a-f]{16}\.(?:jpg|jpeg|png|webp)$",
    re.IGNORECASE,
)
_CATALOG_FILENAME_RE = re.compile(
    r"^[0-9a-f]{32}\.(?:jpg|jpeg|png|webp)$",
    re.IGNORECASE,
)

_PROFILE_REFERENCES = (
    ("customers", "profile_photo"),
    ("workers", "profile_photo"),
    ("partners", "profile_photo"),
)
_CATALOG_REFERENCES = (
    ("service_categories", "image_path"),
    ("services", "image_path"),
)

_logger = logging.getLogger(__name__)


def _has_database_reference(db, value, references):
    """Return True when referenced, and fail closed when DB checks fail."""
    try:
        for table, column in references:
            table_exists = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            if not table_exists:
                continue

            columns = {
                row[1]
                for row in db.execute(f'PRAGMA table_info("{table}")').fetchall()
            }
            if column not in columns:
                continue

            referenced = db.execute(
                f'SELECT 1 FROM "{table}" WHERE "{column}"=? LIMIT 1',
                (value,),
            ).fetchone()
            if referenced:
                return True
    except Exception:
        # If the reference scan cannot be trusted, preserving the file is safer.
        _logger.exception("Could not verify whether an old upload is still referenced")
        return True

    return False


def _unlink_managed_file(directory, filename):
    """Unlink one direct child of a managed upload directory; never follow symlinks."""
    try:
        root = Path(directory).resolve()
        target = Path(directory) / filename

        if target.is_symlink():
            _logger.warning("Skipping symlink while cleaning an old upload")
            return False
        if target.parent.resolve() != root:
            _logger.warning("Skipping upload path outside its managed directory")
            return False
        if not target.is_file():
            return False

        target.unlink()
        return True
    except OSError:
        _logger.exception("Could not remove an unreferenced old upload")
        return False


def delete_replaced_profile_photo(db, old_filename, new_filename):
    """Delete a replaced profile photo only after no account references it."""
    old_filename = str(old_filename or "").strip()
    new_filename = str(new_filename or "").strip()

    if not old_filename or old_filename == new_filename:
        return False
    if not _PROFILE_FILENAME_RE.fullmatch(old_filename):
        return False
    if _has_database_reference(db, old_filename, _PROFILE_REFERENCES):
        return False

    return _unlink_managed_file(PROFILE_UPLOAD_DIR, old_filename)


def delete_replaced_catalog_photo(db, old_image_path, new_image_path):
    """Delete a replaced catalog upload only after no category/service references it."""
    old_image_path = str(old_image_path or "").strip()
    new_image_path = str(new_image_path or "").strip()

    if not old_image_path or old_image_path == new_image_path:
        return False

    parsed = urlsplit(old_image_path)
    prefix = "/static/uploads/catalog/"
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        return False
    if not parsed.path.startswith(prefix):
        return False

    filename = parsed.path[len(prefix):]
    if not _CATALOG_FILENAME_RE.fullmatch(filename):
        return False
    if _has_database_reference(db, old_image_path, _CATALOG_REFERENCES):
        return False

    return _unlink_managed_file(CATALOG_UPLOAD_DIR, filename)
