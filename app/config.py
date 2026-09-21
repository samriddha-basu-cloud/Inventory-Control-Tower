import os
from datetime import timedelta

basedir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-key-change-me")
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", f"sqlite:///{os.path.join(basedir, 'instance', 'ict.db')}"
    )
    # Render/Heroku style Postgres URLs sometimes use postgres:// which SQLAlchemy 1.4+/2.x rejects
    if SQLALCHEMY_DATABASE_URI.startswith("postgres://"):
        SQLALCHEMY_DATABASE_URI = SQLALCHEMY_DATABASE_URI.replace(
            "postgres://", "postgresql://", 1
        )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}

    ENVIRONMENT = os.environ.get("ENVIRONMENT", "development")
    UPLOAD_LIMIT_MB = int(os.environ.get("UPLOAD_LIMIT_MB", 25))
    MAX_CONTENT_LENGTH = UPLOAD_LIMIT_MB * 1024 * 1024

    PERMANENT_SESSION_LIFETIME = timedelta(hours=12)

    # Default organizational configuration (overridable via Admin > Settings in future phases)
    DEFAULT_SERVICE_LEVEL = 0.95
    DEFAULT_HOLDING_COST_PCT = 0.22          # annual carrying cost as % of unit cost
    DEFAULT_ORDERING_COST = 750.0            # currency units per PO line
    WORKING_DAYS_PER_YEAR = 365

    INVENTORY_POSITION_FORMULA = "on_hand + on_order + in_transit - allocated - backorders"


class DevelopmentConfig(Config):
    DEBUG = True


class ProductionConfig(Config):
    DEBUG = False


class TestingConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False


config_map = {
    "development": DevelopmentConfig,
    "production": ProductionConfig,
    "testing": TestingConfig,
}
