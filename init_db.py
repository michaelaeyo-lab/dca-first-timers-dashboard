"""
Initialize database tables and seed default admin user.
Run manually: python init_db.py
Or called automatically on app startup.
"""
import os
from dotenv import load_dotenv
load_dotenv()

from database import engine, SessionLocal, Base
from models import AdminUser
from werkzeug.security import generate_password_hash


def init():
    # Create all tables
    Base.metadata.create_all(bind=engine)
    print("Tables created successfully.")

    # Seed default admin if none exists
    db = SessionLocal()
    try:
        existing = db.query(AdminUser).first()
        if not existing:
            admin = AdminUser(
                name="DCA Admin",
                email=os.environ.get("ADMIN_EMAIL", "admin@dca.church"),
                password_hash=generate_password_hash(
                    os.environ.get("ADMIN_PASSWORD", "changeme")
                ),
                role="super_admin",
            )
            db.add(admin)
            db.commit()
            print(f"Default admin created: {admin.email}")
        else:
            print("Admin user already exists, skipping seed.")
    finally:
        db.close()


if __name__ == "__main__":
    init()
