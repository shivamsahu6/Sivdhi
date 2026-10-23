import os
import re
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _load_secret_key():
    """Load SECRET_KEY from the environment, with a local-only fallback.

    Production/deployed environments should always set SECRET_KEY explicitly.
    The local fallback keeps the existing development workflow working without
    committing a secret into source control.
    """
    value = os.getenv("SECRET_KEY", "").strip()
    if value and len(value) >= 32 and not value.lower().startswith("replace-with-"):
        return value

    key_file = BASE_DIR / ".secret_key"
    try:
        if key_file.is_file():
            value = key_file.read_text(encoding="utf-8").strip()
            if value:
                return value

        value = secrets.token_hex(32)
        # Restrict permissions where the operating system supports it.
        key_file.write_text(value, encoding="utf-8")
        try:
            os.chmod(key_file, 0o600)
        except OSError:
            pass
        return value
    except OSError as exc:
        # A random per-process fallback breaks sessions across workers and
        # restarts. Require an explicit stable key on read-only deployments.
        raise RuntimeError(
            "Cannot persist the local secret key. Configure SECRET_KEY in the environment."
        ) from exc


class Config:
    _app_env = os.getenv("APP_ENV", os.getenv("FLASK_ENV", "development")).strip().lower()
    _configured_secret = os.getenv("SECRET_KEY", "").strip()
    if _app_env == "production" and (
        len(_configured_secret) < 32
        or _configured_secret.lower().startswith("replace-with-")
    ):
        raise RuntimeError(
            "Production requires a strong SECRET_KEY (at least 32 characters) in the environment."
        )
    SECRET_KEY = _load_secret_key()
    _database_value = os.getenv("DATABASE_PATH", "sivdhi.db").strip() or "sivdhi.db"
    _database_path = Path(_database_value)
    DATABASE = str(_database_path if _database_path.is_absolute() else (BASE_DIR / _database_path).resolve())
    SQLITE_TIMEOUT_SECONDS = max(5, int(os.getenv("SQLITE_TIMEOUT_SECONDS", "30")))
    SQLITE_BUSY_TIMEOUT_MS = max(5000, int(os.getenv("SQLITE_BUSY_TIMEOUT_MS", "30000")))
    _support_email = os.getenv("SUPPORT_EMAIL", "").strip()
    SUPPORT_EMAIL = (
        _support_email
        if len(_support_email) <= 254
        and re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", _support_email)
        else ""
    )
    # Store only digits so this value cannot inject a URL scheme into tel:/wa.me links.
    _support_phone = re.sub(r"\D", "", os.getenv("SUPPORT_PHONE", ""))
    SUPPORT_PHONE = _support_phone if 8 <= len(_support_phone) <= 15 else ""
    MAX_CONTENT_LENGTH = 5 * 1024 * 1024
    ALLOWED_IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    # Secure cookies default on in production; local HTTP development remains usable.
    _production = os.getenv("APP_ENV", os.getenv("FLASK_ENV", "development")).strip().lower() == "production"
    _secure_cookie_env = os.getenv("SESSION_COOKIE_SECURE", "1" if _production else "0").strip().lower() in {"1", "true", "yes", "on"}
    # Never allow an explicit insecure-cookie override in production.
    SESSION_COOKIE_SECURE = _production or _secure_cookie_env
    SESSION_COOKIE_NAME = "sivdhi_session"
    PERMANENT_SESSION_LIFETIME = 60 * 60 * 8
    WTF_CSRF_TIME_LIMIT = 60 * 60
    WTF_CSRF_SSL_STRICT = _production
    PREFERRED_URL_SCHEME = "https" if _production else "http"
