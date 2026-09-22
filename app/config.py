"""Application configuration. All secrets come from environment variables."""
import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
INSTANCE_DIR = BASE_DIR / "instance"


def _json_dumps(o):
    from .utils.jsonutil import dumps
    return dumps(o)


def _db_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        INSTANCE_DIR.mkdir(exist_ok=True)
        return f"sqlite:///{INSTANCE_DIR / 'ict.db'}"
    if url.startswith("postgres://"):  # Heroku/Render style
        url = url.replace("postgres://", "postgresql://", 1)
    if url.startswith("sqlite:///") and not url.startswith("sqlite:////") and ":memory:" not in url:
        # relative sqlite path -> anchor at project root so cwd never matters
        url = "sqlite:///" + str(BASE_DIR / url[len("sqlite:///"):])
    return url


class Config:
    ENVIRONMENT = os.environ.get("ENVIRONMENT", "development")
    SECRET_KEY = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
    SQLALCHEMY_DATABASE_URI = _db_url()
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True, "json_serializer": _json_dumps}
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    UPLOAD_LIMIT_MB = int(os.environ.get("UPLOAD_LIMIT_MB", "25"))
    MAX_CONTENT_LENGTH = UPLOAD_LIMIT_MB * 1024 * 1024
    UPLOAD_FOLDER = str(BASE_DIR / "data" / "uploads")
    ALLOWED_UPLOAD_EXT = {"csv", "xlsx", "json"}

    # Security
    CSRF_ENABLED = True
    API_KEY = os.environ.get("API_KEY")          # machine-to-machine (FIT, ERP push)
    AUTH_REQUIRED = os.environ.get("AUTH_REQUIRED", "0") == "1"
    DEFAULT_ROLE = os.environ.get("DEFAULT_ROLE", "Administrator")
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"

    # Behaviour
    JOBS_SYNC = os.environ.get("JOBS_SYNC", "1") == "1"   # synchronous fallback for heavy work
    JOB_WORKERS = int(os.environ.get("JOB_WORKERS", "2"))
    AUTO_INIT_DB = os.environ.get("AUTO_INIT_DB", "1") == "1"   # create tables + seed on start (local convenience); use `flask db upgrade` in production
    CURRENCY_CODE = os.environ.get("CURRENCY_CODE", "INR")
    CURRENCY_SYMBOL = os.environ.get("CURRENCY_SYMBOL", "₹")
    NUMBER_STYLE = os.environ.get("NUMBER_STYLE", "IN")   # IN = lakh/crore, INTL = K/M/B

    # Optional notification channels (all disabled unless configured)
    NOTIFY_WEBHOOK_URL = os.environ.get("NOTIFY_WEBHOOK_URL")
    NOTIFY_SLACK_URL = os.environ.get("NOTIFY_SLACK_URL")
    NOTIFY_TEAMS_URL = os.environ.get("NOTIFY_TEAMS_URL")
    SMTP_HOST = os.environ.get("SMTP_HOST")
    SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
    SMTP_USER = os.environ.get("SMTP_USER")
    SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")
    NOTIFY_EMAIL_TO = os.environ.get("NOTIFY_EMAIL_TO")


class DevelopmentConfig(Config):
    DEBUG = os.environ.get("FLASK_DEBUG", "0") == "1"


class TestingConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_ENGINE_OPTIONS = {"json_serializer": _json_dumps}
    WTF_CSRF = False
    AUTO_INIT_DB = False
    JOBS_SYNC = True


class ProductionConfig(Config):
    DEBUG = False
    SESSION_COOKIE_SECURE = True

    def __init__(self):  # pragma: no cover - guarded at app creation
        if not os.environ.get("SECRET_KEY"):
            raise RuntimeError("SECRET_KEY must be set in production")


CONFIGS = {
    "development": DevelopmentConfig,
    "testing": TestingConfig,
    "production": ProductionConfig,
}
