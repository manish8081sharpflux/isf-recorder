"""
ISF Recorder — Database Connection
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SQLAlchemy engine and session factory.
Pool sized for 30-50 concurrent users.
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from backend.config import DATABASE_URL, DB_POOL_SIZE, DB_MAX_OVERFLOW

engine = create_engine(
    DATABASE_URL,
    pool_size=DB_POOL_SIZE,
    max_overflow=DB_MAX_OVERFLOW,
    pool_recycle=1800,
    pool_pre_ping=True,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    """FastAPI dependency — yields a DB session, auto-closes after request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
