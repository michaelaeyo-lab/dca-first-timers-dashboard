import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase
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


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        return db
    finally:
        pass  # caller is responsible for closing
