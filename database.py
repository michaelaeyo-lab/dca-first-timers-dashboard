import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, scoped_session, DeclarativeBase
from dotenv import load_dotenv

load_dotenv()

# Railway provides postgres:// but SQLAlchemy 2.0 requires postgresql://
database_url = os.environ.get("DATABASE_URL") or os.environ.get(
    "LOCAL_DATABASE_URL", "sqlite:///local.db"
)
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)

engine = create_engine(database_url, pool_pre_ping=True, pool_recycle=300)
SessionLocal = sessionmaker(bind=engine)
# Scoped session for Flask-Login user loader (keeps objects attached)
ScopedSession = scoped_session(SessionLocal)


class Base(DeclarativeBase):
    pass
