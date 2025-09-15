import os
import logging
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, declarative_base

DB_URL = os.getenv("ADMIN_DB_URL", "sqlite:///./nutrios.db")
engine = create_engine(DB_URL, connect_args={"check_same_thread": False} if DB_URL.startswith("sqlite") else {})
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()

def init_db(BaseModel):
    BaseModel.metadata.create_all(bind=engine)

def ensure_meals_extras_column():
    """Ensure 'extras' column exists in 'meals' (SQLite-compatible)."""
    try:
        with engine.connect() as conn:
            # Only attempt for SQLite
            if DB_URL.startswith("sqlite"):
                res = conn.execute(text("PRAGMA table_info(meals)"))
                cols = [row[1] for row in res.fetchall()]
                if "extras" not in cols:
                    # SQLite: JSON affinity is TEXT; we use TEXT to be safe
                    conn.execute(text("ALTER TABLE meals ADD COLUMN extras TEXT"))
                    conn.commit()
    except Exception as e:
        # Best-effort; API can still run without extras column until next restart
        logging.getLogger(__name__).warning("ensure_meals_extras_column failed: %s", e)
