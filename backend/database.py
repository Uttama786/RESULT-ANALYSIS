import logging
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from config import SQLALCHEMY_DATABASE_URL

logger = logging.getLogger(__name__)

# Configure connect_args based on DB dialect (SQLite vs PostgreSQL)
connect_args = {}
if SQLALCHEMY_DATABASE_URL.startswith("sqlite"):
    connect_args = {"check_same_thread": False}

logger.info(f"🗄️ Initializing database engine with dialect: {'SQLite' if SQLALCHEMY_DATABASE_URL.startswith('sqlite') else 'PostgreSQL'}")

engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args=connect_args,
    pool_pre_ping=True  # Automatically reconnects if DB connection drops
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

def get_db():
    """Dependency / Helper to get a SQLAlchemy database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def upgrade_db_schema():
    """Dynamically migrates existing database tables to add missing persistent storage columns."""
    try:
        from sqlalchemy import inspect, text
        inspector = inspect(engine)
        if "history" in inspector.get_table_names():
            columns = [c["name"] for c in inspector.get_columns("history")]
            is_sqlite = SQLALCHEMY_DATABASE_URL.startswith("sqlite")
            blob_type = "BLOB" if is_sqlite else "BYTEA"

            with engine.begin() as conn:
                if "excel_data" not in columns:
                    logger.info(f"🛠️ Migrating database schema: Adding 'excel_data' ({blob_type}) column to 'history' table...")
                    conn.execute(text(f"ALTER TABLE history ADD COLUMN excel_data {blob_type}"))
                if "excel_url" not in columns:
                    logger.info("🛠️ Migrating database schema: Adding 'excel_url' (VARCHAR) column to 'history' table...")
                    conn.execute(text("ALTER TABLE history ADD COLUMN excel_url VARCHAR(500) DEFAULT ''"))
                if "storage_provider" not in columns:
                    logger.info("🛠️ Migrating database schema: Adding 'storage_provider' (VARCHAR) column to 'history' table...")
                    conn.execute(text("ALTER TABLE history ADD COLUMN storage_provider VARCHAR(50) DEFAULT 'db'"))
    except Exception as e:
        logger.warning(f"Note on schema migration: {e}")
