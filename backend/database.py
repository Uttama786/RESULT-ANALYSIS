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
