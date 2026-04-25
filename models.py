from sqlalchemy import (
    Integer, String, Boolean, DateTime, Text, ForeignKey, UniqueConstraint, JSON
)
from sqlalchemy.orm import mapped_column, relationship
from sqlalchemy.sql import func
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from database import Base


# ═══════════════════════════════════════════════════
# FIRST TIMER (user accounts)
# ═══════════════════════════════════════════════════
class FirstTimer(UserMixin, Base):
    __tablename__ = "first_timers"

    id = mapped_column(Integer, primary_key=True)
    first_name = mapped_column(String(100), nullable=False)
    last_name = mapped_column(String(100), nullable=False)
    email = mapped_column(String(255), unique=True, nullable=False, index=True)
    phone = mapped_column(String(30), nullable=True)
    password_hash = mapped_column(String(256), nullable=False)
    created_at = mapped_column(DateTime, server_default=func.now())
    last_login = mapped_column(DateTime, nullable=True)
    is_active = mapped_column(Boolean, default=True)

    # Relationships
    day_progress = relationship("DayProgress", back_populates="user", cascade="all, delete-orphan")
    activity_logs = relationship("ActivityLog", back_populates="user", cascade="all, delete-orphan")
    assessment = relationship("Assessment", back_populates="user", uselist=False)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def get_id(self):
        return f"ft:{self.id}"

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}"

    @property
    def completed_days(self):
        return sum(1 for dp in self.day_progress if dp.is_completed)


# ═══════════════════════════════════════════════════
# ADMIN USER
# ═══════════════════════════════════════════════════
class AdminUser(UserMixin, Base):
    __tablename__ = "admin_users"

    id = mapped_column(Integer, primary_key=True)
    name = mapped_column(String(200), nullable=False)
    email = mapped_column(String(255), unique=True, nullable=False, index=True)
    password_hash = mapped_column(String(256), nullable=False)
    role = mapped_column(String(50), default="admin")
    created_at = mapped_column(DateTime, server_default=func.now())
    last_login = mapped_column(DateTime, nullable=True)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def get_id(self):
        return f"admin:{self.id}"


# ═══════════════════════════════════════════════════
# DAY PROGRESS (per-user, per-day tracking)
# ═══════════════════════════════════════════════════
class DayProgress(Base):
    __tablename__ = "day_progress"

    id = mapped_column(Integer, primary_key=True)
    user_id = mapped_column(Integer, ForeignKey("first_timers.id"), nullable=False)
    day_number = mapped_column(Integer, nullable=False)  # 1-7
    started_at = mapped_column(DateTime, server_default=func.now())
    completed_at = mapped_column(DateTime, nullable=True)
    answers = mapped_column(JSON, default=dict)       # {"0": "text", "1": "text"}
    notes = mapped_column(Text, default="")
    action_acknowledged = mapped_column(Boolean, default=False)
    checklist = mapped_column(JSON, default=dict)      # {"0": true, "1": false, ...}
    is_completed = mapped_column(Boolean, default=False)

    user = relationship("FirstTimer", back_populates="day_progress")

    __table_args__ = (
        UniqueConstraint("user_id", "day_number", name="uq_user_day"),
    )


# ═══════════════════════════════════════════════════
# ACTIVITY LOG (timestamped audit trail)
# ═══════════════════════════════════════════════════
class ActivityLog(Base):
    __tablename__ = "activity_logs"

    id = mapped_column(Integer, primary_key=True)
    user_id = mapped_column(Integer, ForeignKey("first_timers.id"), nullable=False)
    action_type = mapped_column(String(50), nullable=False)
    # action_types: signup, login, day_started, card_answered,
    #               day_completed, assessment_started, assessment_completed
    day_number = mapped_column(Integer, nullable=True)
    card_name = mapped_column(String(100), nullable=True)
    timestamp = mapped_column(DateTime, server_default=func.now(), index=True)
    extra = mapped_column(JSON, default=dict)

    user = relationship("FirstTimer", back_populates="activity_logs")


# ═══════════════════════════════════════════════════
# ASSESSMENT (final 3-question reflection)
# ═══════════════════════════════════════════════════
class Assessment(Base):
    __tablename__ = "assessments"

    id = mapped_column(Integer, primary_key=True)
    user_id = mapped_column(Integer, ForeignKey("first_timers.id"), unique=True, nullable=False)
    responses = mapped_column(JSON, default=dict)  # {"0": "response", "1": "...", "2": "..."}
    completed_at = mapped_column(DateTime, server_default=func.now())

    user = relationship("FirstTimer", back_populates="assessment")
