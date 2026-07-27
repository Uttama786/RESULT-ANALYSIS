import os

# Base directory setup
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.getenv("DATA_DIR", BASE_DIR)
os.makedirs(DATA_DIR, exist_ok=True)

EXPORTS_DIR = os.path.join(DATA_DIR, "exports")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
os.makedirs(EXPORTS_DIR, exist_ok=True)
os.makedirs(TEMPLATES_DIR, exist_ok=True)

# Database Configuration
# Fallback to local SQLite if DATABASE_URL is not provided (Localhost environment)
DEFAULT_SQLITE_URL = f"sqlite:///{os.path.join(DATA_DIR, 'app.db')}"
RAW_DB_URL = os.getenv("DATABASE_URL", DEFAULT_SQLITE_URL)

# SQLAlchemy require "postgresql://" instead of legacy "postgres://" provided by Render/Heroku
if RAW_DB_URL.startswith("postgres://"):
    SQLALCHEMY_DATABASE_URL = RAW_DB_URL.replace("postgres://", "postgresql://", 1)
else:
    SQLALCHEMY_DATABASE_URL = RAW_DB_URL
