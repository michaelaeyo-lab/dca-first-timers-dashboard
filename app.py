import os
import re
import csv
import io
import sys
from datetime import datetime, timedelta, date
from functools import wraps
from urllib.parse import urlparse

from dotenv import load_dotenv
load_dotenv()

from flask import (
    Flask, render_template, request, redirect, url_for,
    session, flash, jsonify, Response, abort
)
from flask_login import (
    LoginManager, login_user, logout_user, login_required, current_user
)
from flask_wtf.csrf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy import func as sa_func, desc, text

from database import engine, SessionLocal, ScopedSession, Base
from models import FirstTimer, AdminUser, DayProgress, ActivityLog, Assessment


# ═══════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════
TOTAL_WEEKS = 4


# ═══════════════════════════════════════════════════
# APP CONFIG
# ═══════════════════════════════════════════════════
app = Flask(__name__)

# Secret key — refuse to start with the default in production
_secret = os.environ.get("SECRET_KEY", "")
if not _secret:
    if os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("DATABASE_URL"):
        print("FATAL: SECRET_KEY environment variable is not set. Refusing to start in production.", file=sys.stderr)
        sys.exit(1)
    _secret = "dca-dev-secret-key-LOCAL-ONLY"
app.secret_key = _secret

app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = not app.debug  # HTTPS-only cookies in production
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=7)
app.config["WTF_CSRF_TIME_LIMIT"] = 3600  # 1-hour CSRF token validity

# ── CSRF Protection ──
csrf = CSRFProtect(app)

# ── Rate Limiting ──
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["200 per hour"],
    storage_uri="memory://",
)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"
login_manager.login_message = "Please log in to access your journey."
login_manager.login_message_category = "info"


def _is_safe_redirect(target):
    """Validate that redirect target is a safe internal URL."""
    if not target:
        return False
    parsed = urlparse(target)
    # Only allow relative paths (no scheme, no external host)
    return parsed.scheme == "" and parsed.netloc == ""


# ═══════════════════════════════════════════════════
# CREATE TABLES + SEED ADMIN ON STARTUP
# ═══════════════════════════════════════════════════
try:
    with app.app_context():
        Base.metadata.create_all(bind=engine)
        db = SessionLocal()
        try:
            if not db.query(AdminUser).first():
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
        finally:
            db.close()
except Exception as e:
    print(f"DB init warning (will retry on first request): {e}")

_db_initialized = False

@app.before_request
def ensure_db():
    global _db_initialized
    if not _db_initialized:
        try:
            Base.metadata.create_all(bind=engine)
            db = SessionLocal()
            try:
                if not db.query(AdminUser).first():
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
            finally:
                db.close()
            _db_initialized = True
        except Exception:
            pass


# ═══════════════════════════════════════════════════
# USER LOADER (dual: first_timers + admins)
# ═══════════════════════════════════════════════════
@login_manager.user_loader
def load_user(user_id):
    try:
        db = ScopedSession()
        prefix, uid = user_id.split(":", 1)
        uid = int(uid)
        if prefix == "ft":
            return db.query(FirstTimer).get(uid)
        elif prefix == "admin":
            return db.query(AdminUser).get(uid)
    except Exception:
        return None


@app.teardown_appcontext
def shutdown_session(exception=None):
    ScopedSession.remove()


# ═══════════════════════════════════════════════════
# DECORATORS
# ═══════════════════════════════════════════════════
def admin_required(f):
    @wraps(f)
    @login_required
    def decorated(*args, **kwargs):
        if not isinstance(current_user, AdminUser):
            flash("Admin access required.", "error")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


def first_timer_required(f):
    @wraps(f)
    @login_required
    def decorated(*args, **kwargs):
        if not isinstance(current_user, FirstTimer):
            flash("This page is for first timers only.", "error")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


# ═══════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════
def log_activity(db, user_id, action_type, day_number=None, card_name=None, extra=None):
    log = ActivityLog(
        user_id=user_id,
        action_type=action_type,
        day_number=day_number,
        card_name=card_name,
        extra=extra or {},
    )
    db.add(log)
    db.commit()


def get_user_progress(db, user_id, week_number=None):
    """Returns dict {day_number: DayProgress} for a user in a specific week."""
    query = db.query(DayProgress).filter_by(user_id=user_id)
    if week_number is not None:
        query = query.filter_by(week_number=week_number)
    rows = query.all()
    return {dp.day_number: dp for dp in rows}


def get_all_user_progress(db, user_id):
    """Returns total completed days across all weeks."""
    return db.query(DayProgress).filter_by(
        user_id=user_id, is_completed=True
    ).count()


def is_day_time_locked(db, user_id, week_number, day_number):
    """Check if a day is time-locked (previous day completed today or later).
    Returns (is_locked, unlock_date) tuple.
    Day 1 of each week: not locked if previous week assessment is done (or week 1).
    Day 2-7: locked until the calendar day after previous day's completion.
    """
    today = datetime.utcnow().date()

    if day_number == 1:
        # Day 1 of week 1 is always unlocked
        if week_number == 1:
            return False, None
        # Day 1 of later weeks: unlocked once previous week assessment is done
        prev_assessment = db.query(Assessment).filter_by(
            user_id=user_id, week_number=week_number - 1
        ).first()
        if not prev_assessment:
            return True, None
        # Time-lock: must be at least the next day after assessment
        assess_date = prev_assessment.completed_at.date()
        if today <= assess_date:
            unlock = assess_date + timedelta(days=1)
            return True, unlock
        return False, None

    # Day 2-7: check previous day's completion
    prev_dp = db.query(DayProgress).filter_by(
        user_id=user_id, week_number=week_number, day_number=day_number - 1
    ).first()

    if not prev_dp or not prev_dp.is_completed:
        return True, None  # Previous day not done at all

    completed_date = prev_dp.completed_at.date()
    if today <= completed_date:
        unlock = completed_date + timedelta(days=1)
        return True, unlock
    return False, None


# ═══════════════════════════════════════════════════
# DAY CONTENT (static data from workbook)
# ═══════════════════════════════════════════════════
DAYS_DATA = [
    {
        "day": 1,
        "title": "Identity in Christ",
        "insight": "You are a new creation. Your past no longer defines you.",
        "verse": "2 Corinthians 5:17",
        "verse_text": "Therefore, if anyone is in Christ, the new creation has come: The old has gone, the new is here!",
        "supporting_verse": "Galatians 2:20",
        "supporting_verse_text": "I have been crucified with Christ and I no longer live, but Christ lives in me. The life I now live in the body, I live by faith in the Son of God, who loved me and gave himself for me.",
        "questions": ["Who am I in Christ?", "What must I stop believing about myself?"],
        "action": "Walk confidently as God\u2019s child.",
        "checklist": ["Prayer", "Word Study", "Application", "Reflection"],
    },
    {
        "day": 2,
        "title": "Dominion Over Sin",
        "insight": "Sin has no dominion over you.",
        "verse": "Romans 6:14",
        "verse_text": "For sin shall no longer be your master, because you are not under the law, but under grace.",
        "supporting_verse": "1 John 1:9",
        "supporting_verse_text": "If we confess our sins, he is faithful and just and will forgive us our sins and purify us from all unrighteousness.",
        "questions": ["What habits must I overcome?", "What triggers must I avoid?"],
        "action": "Choose righteousness deliberately.",
        "checklist": ["Prayer", "Word Study", "Application", "Reflection"],
    },
    {
        "day": 3,
        "title": "The Word Life",
        "insight": "The Word is your guide and strength.",
        "verse": "Psalm 119:105",
        "verse_text": "Your word is a lamp for my feet, a light on my path.",
        "supporting_verse": "Joshua 1:8",
        "supporting_verse_text": "Keep this Book of the Law always on your lips; meditate on it day and night, so that you may be careful to do everything written in it. Then you will be prosperous and successful.",
        "questions": ["What did I learn today?", "How can I apply it?"],
        "action": "Speak the Word daily.",
        "checklist": ["Prayer", "Word Study", "Application", "Reflection"],
    },
    {
        "day": 4,
        "title": "Prayer & Fellowship",
        "insight": "Prayer builds intimacy with God.",
        "verse": "1 Thessalonians 5:17",
        "verse_text": "Pray without ceasing.",
        "supporting_verse": "Philippians 4:6\u20137",
        "supporting_verse_text": "Do not be anxious about anything, but in every situation, by prayer and petition, with thanksgiving, present your requests to God. And the peace of God, which transcends all understanding, will guard your hearts and your minds in Christ Jesus.",
        "questions": ["How consistent was my prayer life?", "Did I sense God\u2019s presence?"],
        "action": "Pray throughout the day.",
        "checklist": ["Prayer", "Word Study", "Application", "Reflection"],
    },
    {
        "day": 5,
        "title": "Holiness & Character",
        "insight": "Your life must reflect Christ.",
        "verse": "1 Peter 1:15\u201316",
        "verse_text": "But just as he who called you is holy, so be holy in all you do; for it is written: \u2018Be holy, because I am holy.\u2019",
        "supporting_verse": "Galatians 5:22\u201323",
        "supporting_verse_text": "But the fruit of the Spirit is love, joy, peace, forbearance, kindness, goodness, faithfulness, gentleness and self-control. Against such things there is no law.",
        "questions": ["Which character trait needs growth?", "How did I respond to people?"],
        "action": "Practice love and patience.",
        "checklist": ["Prayer", "Word Study", "Application", "Reflection"],
    },
    {
        "day": 6,
        "title": "Faith & Confession",
        "insight": "Speak and live by faith.",
        "verse": "Romans 10:17",
        "verse_text": "So then faith comes by hearing, and hearing by the word of God.",
        "supporting_verse": "Hebrews 11:1",
        "supporting_verse_text": "Now faith is confidence in what we hope for and assurance about what we do not see.",
        "questions": ["What did I declare today?", "Did I speak fear or faith?"],
        "action": "Confess God\u2019s promises boldly.",
        "checklist": ["Prayer", "Word Study", "Application", "Reflection"],
    },
    {
        "day": 7,
        "title": "Purpose & Impact",
        "insight": "You are called to make impact.",
        "verse": "Matthew 5:16",
        "verse_text": "In the same way, let your light shine before others, that they may see your good deeds and glorify your Father in heaven.",
        "supporting_verse": "Jeremiah 29:11",
        "supporting_verse_text": "For I know the plans I have for you, declares the Lord, plans to prosper you and not to harm you, plans to give you hope and a future.",
        "questions": ["Who did I help?", "Did I share Christ?"],
        "action": "Be intentional about impact.",
        "checklist": ["Prayer", "Word Study", "Application", "Reflection"],
    },
]

ASSESSMENT_QUESTIONS = [
    "Am I more spiritually disciplined this week compared to last?",
    "Am I walking in dominion over sin?",
    "Is my relationship with God stronger?",
]

WEEK_LABELS = {
    1: "Foundation",
    2: "Strengthening",
    3: "Deepening",
    4: "Mastery",
}


# ═══════════════════════════════════════════════════
# ADMIN SEED/RESET (one-time setup URL)
# ═══════════════════════════════════════════════════
@app.route("/setup-admin")
@limiter.limit("3 per hour")
def setup_admin():
    """Reset or create the admin user using current env vars.
    Visit this URL once after deployment to ensure admin exists.
    """
    email = os.environ.get("ADMIN_EMAIL", "admin@dca.church")
    password = os.environ.get("ADMIN_PASSWORD", "changeme")

    db = SessionLocal()
    try:
        admin = db.query(AdminUser).filter_by(email=email).first()
        if admin:
            admin.set_password(password)
            db.commit()
            return jsonify({"status": "Admin password reset", "email": email})
        else:
            admin = AdminUser(
                name="DCA Admin",
                email=email,
                password_hash=generate_password_hash(password),
                role="super_admin",
            )
            db.add(admin)
            db.commit()
            return jsonify({"status": "Admin created", "email": email})
    finally:
        db.close()


# ═══════════════════════════════════════════════════
# AUTH ROUTES
# ═══════════════════════════════════════════════════
@app.route("/login", methods=["GET", "POST"])
@limiter.limit("10 per minute")
def login():
    if current_user.is_authenticated:
        if isinstance(current_user, AdminUser):
            return redirect(url_for("admin_dashboard"))
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        db = SessionLocal()
        try:
            # Check first timers first
            user = db.query(FirstTimer).filter_by(email=email).first()
            if user and user.check_password(password):
                user.last_login = datetime.utcnow()
                db.commit()
                user_id = user.id
                log_activity(db, user_id, "login")
                login_user(user, remember=True)
                next_page = request.args.get("next")
                if not next_page or not _is_safe_redirect(next_page):
                    next_page = url_for("dashboard")
                db.close()
                return redirect(next_page)

            # Check admins
            admin = db.query(AdminUser).filter_by(email=email).first()
            if admin and admin.check_password(password):
                admin.last_login = datetime.utcnow()
                db.commit()
                login_user(admin, remember=True)
                db.close()
                return redirect(url_for("admin_dashboard"))

            flash("Invalid email or password.", "error")
        except Exception:
            flash("Something went wrong. Please try again.", "error")
        finally:
            try:
                db.close()
            except Exception:
                pass

    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
@limiter.limit("5 per minute")
def register():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        first_name = request.form.get("first_name", "").strip()
        last_name = request.form.get("last_name", "").strip()
        email = request.form.get("email", "").strip().lower()
        phone = request.form.get("phone", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")

        if not all([first_name, last_name, email, password]):
            flash("Please fill in all required fields.", "error")
            return render_template("register.html")

        if password != confirm:
            flash("Passwords do not match.", "error")
            return render_template("register.html")

        if len(password) < 8:
            flash("Password must be at least 8 characters.", "error")
            return render_template("register.html")

        if not re.search(r"[A-Z]", password) or not re.search(r"[0-9]", password):
            flash("Password must contain at least one uppercase letter and one number.", "error")
            return render_template("register.html")

        # Basic email format check
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
            flash("Please enter a valid email address.", "error")
            return render_template("register.html")

        db = SessionLocal()
        try:
            existing = db.query(FirstTimer).filter_by(email=email).first()
            if existing:
                flash("An account with this email already exists.", "error")
                return render_template("register.html")

            user = FirstTimer(
                first_name=first_name,
                last_name=last_name,
                email=email,
                phone=phone,
                current_week=1,
            )
            user.set_password(password)
            db.add(user)
            db.commit()

            # Re-fetch to get the ID safely
            user = db.query(FirstTimer).filter_by(email=email).first()
            log_activity(db, user.id, "signup")

            # Store user ID before closing db
            user_id = user.id
            user_name = user.first_name
            db.close()

            # Re-fetch for login_user (needs attached object)
            db = SessionLocal()
            user = db.query(FirstTimer).get(user_id)
            login_user(user, remember=True)
            flash(f"Welcome, {user_name}! Your 4-week spiritual growth journey begins now.", "success")
            return redirect(url_for("dashboard"))
        except Exception as e:
            db.rollback()
            flash(f"Registration error: {str(e)}", "error")
            return render_template("register.html")
        finally:
            db.close()

    return render_template("register.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("You have been signed out.", "info")
    return redirect(url_for("login"))


# ═══════════════════════════════════════════════════
# FIRST TIMER ROUTES
# ═══════════════════════════════════════════════════
@app.route("/")
def index():
    if current_user.is_authenticated:
        if isinstance(current_user, AdminUser):
            return redirect(url_for("admin_dashboard"))
        return redirect(url_for("dashboard"))
    return redirect(url_for("register"))


@app.route("/dashboard")
@first_timer_required
def dashboard():
    db = SessionLocal()
    try:
        week = current_user.current_week or 1
        progress = get_user_progress(db, current_user.id, week_number=week)
        completed_count = sum(1 for dp in progress.values() if dp.is_completed)
        week_pct = round((completed_count / 7) * 100)

        # Overall progress (across all weeks)
        total_completed = get_all_user_progress(db, current_user.id)
        total_assessments = db.query(Assessment).filter_by(user_id=current_user.id).count()
        # Each week = 7 days + 1 assessment = progress unit
        overall_pct = round((total_completed / (7 * TOTAL_WEEKS)) * 100)

        # Check if current week assessment is done
        week_assessment = db.query(Assessment).filter_by(
            user_id=current_user.id, week_number=week
        ).first()

        # Check time locks for each day
        day_locks = {}
        for d in range(1, 8):
            locked, unlock_date = is_day_time_locked(db, current_user.id, week, d)
            day_locks[d] = {
                "locked": locked,
                "unlock_date": unlock_date.isoformat() if unlock_date else None,
            }

        # Program complete?
        program_complete = (week > TOTAL_WEEKS) or (
            week == TOTAL_WEEKS and week_assessment is not None
        )

        return render_template(
            "dashboard.html",
            days_data=DAYS_DATA,
            progress=progress,
            completed_count=completed_count,
            week_pct=week_pct,
            overall_pct=overall_pct,
            total_completed=total_completed,
            current_week=week,
            total_weeks=TOTAL_WEEKS,
            week_label=WEEK_LABELS.get(week, ""),
            assessment_done=week_assessment is not None,
            program_complete=program_complete,
            day_locks=day_locks,
            user=current_user,
        )
    finally:
        db.close()


@app.route("/day/<int:n>")
@first_timer_required
def day_flow(n):
    if n < 1 or n > 7:
        flash("Invalid day.", "error")
        return redirect(url_for("dashboard"))

    week = current_user.current_week or 1

    db = SessionLocal()
    try:
        progress = get_user_progress(db, current_user.id, week_number=week)

        # Enforce sequential unlock
        if n > 1:
            prev = progress.get(n - 1)
            if not prev or not prev.is_completed:
                flash(f"Please complete Day {n - 1} first.", "error")
                return redirect(url_for("dashboard"))

        # Enforce time lock
        locked, unlock_date = is_day_time_locked(db, current_user.id, week, n)
        if locked:
            if unlock_date:
                flash(f"Day {n} unlocks on {unlock_date.strftime('%A, %B %d')}. Come back tomorrow!", "info")
            else:
                flash(f"Please complete the previous step first.", "error")
            return redirect(url_for("dashboard"))

        # Get or create progress row
        dp = progress.get(n)
        if not dp:
            dp = DayProgress(
                user_id=current_user.id,
                week_number=week,
                day_number=n,
            )
            db.add(dp)
            db.commit()
            db.refresh(dp)
            log_activity(db, current_user.id, "day_started", day_number=n,
                         extra={"week": week})

        day_data = DAYS_DATA[n - 1]

        return render_template(
            "day_flow.html",
            day_data=day_data,
            progress=dp,
            current_week=week,
            total_weeks=TOTAL_WEEKS,
            week_label=WEEK_LABELS.get(week, ""),
            user=current_user,
        )
    finally:
        db.close()


@app.route("/day/<int:n>/save", methods=["POST"])
@first_timer_required
def day_save(n):
    if n < 1 or n > 7:
        return jsonify({"error": "Invalid day"}), 400

    data = request.get_json()
    if not data:
        return jsonify({"error": "No data"}), 400

    week = current_user.current_week or 1

    db = SessionLocal()
    try:
        dp = db.query(DayProgress).filter_by(
            user_id=current_user.id, week_number=week, day_number=n
        ).first()

        if not dp:
            dp = DayProgress(
                user_id=current_user.id,
                week_number=week,
                day_number=n,
            )
            db.add(dp)

        if "answers" in data:
            dp.answers = data["answers"]
        if "notes" in data:
            dp.notes = data["notes"]
        if "checklist" in data:
            dp.checklist = data["checklist"]
        if "action_acknowledged" in data:
            dp.action_acknowledged = data["action_acknowledged"]

        db.commit()

        # Log card interaction
        card_name = data.get("current_card", "unknown")
        log_activity(db, current_user.id, "card_answered", day_number=n,
                     card_name=card_name, extra={"week": week})

        return jsonify({"status": "ok"})
    finally:
        db.close()


@app.route("/day/<int:n>/complete", methods=["POST"])
@first_timer_required
def day_complete(n):
    if n < 1 or n > 7:
        return jsonify({"error": "Invalid day"}), 400

    week = current_user.current_week or 1

    db = SessionLocal()
    try:
        dp = db.query(DayProgress).filter_by(
            user_id=current_user.id, week_number=week, day_number=n
        ).first()

        if not dp:
            return jsonify({"error": "Day not started"}), 400

        dp.is_completed = True
        dp.completed_at = datetime.utcnow()
        db.commit()

        log_activity(db, current_user.id, "day_completed", day_number=n,
                     extra={"week": week})

        next_day = n + 1 if n < 7 else None
        return jsonify({"status": "ok", "next_day": next_day, "week": week})
    finally:
        db.close()


@app.route("/assessment")
@first_timer_required
def assessment():
    week = current_user.current_week or 1

    db = SessionLocal()
    try:
        progress = get_user_progress(db, current_user.id, week_number=week)
        completed_count = sum(1 for dp in progress.values() if dp.is_completed)

        if completed_count < 7:
            flash("Please complete all 7 days before the weekly assessment.", "error")
            return redirect(url_for("dashboard"))

        existing = db.query(Assessment).filter_by(
            user_id=current_user.id, week_number=week
        ).first()
        log_activity(db, current_user.id, "assessment_started",
                     extra={"week": week})

        is_final_week = (week == TOTAL_WEEKS)

        return render_template(
            "assessment.html",
            questions=ASSESSMENT_QUESTIONS,
            existing=existing,
            current_week=week,
            total_weeks=TOTAL_WEEKS,
            week_label=WEEK_LABELS.get(week, ""),
            is_final_week=is_final_week,
            user=current_user,
        )
    finally:
        db.close()


@app.route("/assessment/complete", methods=["POST"])
@first_timer_required
def assessment_complete():
    data = request.get_json()
    if not data or "responses" not in data:
        return jsonify({"error": "No data"}), 400

    week = current_user.current_week or 1

    db = SessionLocal()
    try:
        existing = db.query(Assessment).filter_by(
            user_id=current_user.id, week_number=week
        ).first()
        if existing:
            existing.responses = data["responses"]
            existing.completed_at = datetime.utcnow()
        else:
            a = Assessment(
                user_id=current_user.id,
                week_number=week,
                responses=data["responses"],
            )
            db.add(a)

        # Advance to next week if not final
        is_final = (week == TOTAL_WEEKS)
        if not is_final:
            # Re-fetch user in this session
            user = db.query(FirstTimer).get(current_user.id)
            user.current_week = week + 1

        db.commit()
        log_activity(db, current_user.id, "assessment_completed",
                     extra={"week": week, "advanced_to_week": week + 1 if not is_final else None})

        return jsonify({
            "status": "ok",
            "is_final": is_final,
            "next_week": week + 1 if not is_final else None,
        })
    finally:
        db.close()


# ═══════════════════════════════════════════════════
# ADMIN ROUTES
# ═══════════════════════════════════════════════════
@app.route("/admin")
@admin_required
def admin_dashboard():
    db = SessionLocal()
    try:
        total_users = db.query(FirstTimer).count()
        now = datetime.utcnow()
        active_today = db.query(ActivityLog.user_id).filter(
            ActivityLog.timestamp >= now - timedelta(hours=24)
        ).distinct().count()

        # Users who completed all 4 weeks (28 days)
        from sqlalchemy import and_
        completed_users = 0
        all_users = db.query(FirstTimer).all()
        for u in all_users:
            dp_count = db.query(DayProgress).filter(
                and_(DayProgress.user_id == u.id, DayProgress.is_completed == True)
            ).count()
            if dp_count >= 7 * TOTAL_WEEKS:
                completed_users += 1

        completion_rate = round((completed_users / total_users * 100)) if total_users > 0 else 0

        # Day-by-day completion counts (across all weeks)
        day_stats = []
        for d in range(1, 8):
            count = db.query(DayProgress).filter(
                and_(DayProgress.day_number == d, DayProgress.is_completed == True)
            ).count()
            day_stats.append({"day": d, "count": count})

        # Average days completed per user
        if total_users > 0:
            total_completed = db.query(DayProgress).filter(
                DayProgress.is_completed == True
            ).count()
            avg_days = round(total_completed / total_users, 1)
        else:
            avg_days = 0

        # Week distribution
        week_stats = []
        for w in range(1, TOTAL_WEEKS + 1):
            users_in_week = db.query(FirstTimer).filter(
                FirstTimer.current_week == w
            ).count()
            week_stats.append({"week": w, "label": WEEK_LABELS.get(w, ""), "count": users_in_week})

        return render_template(
            "admin_dashboard.html",
            total_users=total_users,
            active_today=active_today,
            completion_rate=completion_rate,
            completed_users=completed_users,
            day_stats=day_stats,
            avg_days=avg_days,
            week_stats=week_stats,
            total_weeks=TOTAL_WEEKS,
            admin=current_user,
        )
    finally:
        db.close()


@app.route("/admin/users")
@admin_required
def admin_users():
    db = SessionLocal()
    try:
        search = request.args.get("search", "").strip()
        query = db.query(FirstTimer)

        if search:
            like = f"%{search}%"
            query = query.filter(
                (FirstTimer.first_name.ilike(like)) |
                (FirstTimer.last_name.ilike(like)) |
                (FirstTimer.email.ilike(like))
            )

        users = query.order_by(desc(FirstTimer.created_at)).all()

        # Attach progress info to each user
        user_data = []
        for u in users:
            completed = db.query(DayProgress).filter(
                DayProgress.user_id == u.id,
                DayProgress.is_completed == True
            ).count()

            last_activity = db.query(ActivityLog).filter_by(
                user_id=u.id
            ).order_by(desc(ActivityLog.timestamp)).first()

            user_data.append({
                "user": u,
                "completed_days": completed,
                "current_week": u.current_week or 1,
                "last_active": last_activity.timestamp if last_activity else u.created_at,
            })

        return render_template(
            "admin_users.html",
            user_data=user_data,
            search=search,
            total_weeks=TOTAL_WEEKS,
            admin=current_user,
        )
    finally:
        db.close()


@app.route("/admin/users/<int:user_id>")
@admin_required
def admin_user_detail(user_id):
    db = SessionLocal()
    try:
        user = db.query(FirstTimer).get(user_id)
        if not user:
            flash("User not found.", "error")
            return redirect(url_for("admin_users"))

        # Get progress for all weeks
        all_progress = {}
        for w in range(1, TOTAL_WEEKS + 1):
            all_progress[w] = get_user_progress(db, user.id, week_number=w)

        activities = db.query(ActivityLog).filter_by(
            user_id=user.id
        ).order_by(desc(ActivityLog.timestamp)).limit(100).all()

        assessments = db.query(Assessment).filter_by(user_id=user.id).all()
        assessments_by_week = {a.week_number: a for a in assessments}

        return render_template(
            "admin_user_detail.html",
            user=user,
            all_progress=all_progress,
            activities=activities,
            assessments_by_week=assessments_by_week,
            days_data=DAYS_DATA,
            assessment_questions=ASSESSMENT_QUESTIONS,
            total_weeks=TOTAL_WEEKS,
            week_labels=WEEK_LABELS,
            admin=current_user,
        )
    finally:
        db.close()


@app.route("/admin/export")
@admin_required
def admin_export():
    db = SessionLocal()
    try:
        output = io.StringIO()
        writer = csv.writer(output)

        # Header
        header = [
            "First Name", "Last Name", "Email", "Phone", "Registered", "Current Week",
        ]
        for w in range(1, TOTAL_WEEKS + 1):
            for d in range(1, 8):
                header.extend([
                    f"W{w}D{d} Started", f"W{w}D{d} Completed",
                    f"W{w}D{d} Q1", f"W{w}D{d} Q2", f"W{w}D{d} Notes",
                ])
            header.extend([
                f"W{w} Assessment Q1", f"W{w} Assessment Q2", f"W{w} Assessment Q3",
                f"W{w} Assessment Completed",
            ])
        writer.writerow(header)

        users = db.query(FirstTimer).order_by(FirstTimer.created_at).all()
        for u in users:
            row = [u.first_name, u.last_name, u.email, u.phone or "",
                   str(u.created_at), u.current_week or 1]

            for w in range(1, TOTAL_WEEKS + 1):
                for d in range(1, 8):
                    dp = db.query(DayProgress).filter_by(
                        user_id=u.id, week_number=w, day_number=d
                    ).first()
                    if dp:
                        answers = dp.answers or {}
                        row.extend([
                            str(dp.started_at) if dp.started_at else "",
                            str(dp.completed_at) if dp.completed_at else "",
                            answers.get("0", ""),
                            answers.get("1", ""),
                            dp.notes or "",
                        ])
                    else:
                        row.extend(["", "", "", "", ""])

                assess = db.query(Assessment).filter_by(
                    user_id=u.id, week_number=w
                ).first()
                if assess:
                    responses = assess.responses or {}
                    row.extend([
                        responses.get("0", ""),
                        responses.get("1", ""),
                        responses.get("2", ""),
                        str(assess.completed_at) if assess.completed_at else "",
                    ])
                else:
                    row.extend(["", "", "", ""])

            writer.writerow(row)

        output.seek(0)
        return Response(
            output.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": "attachment; filename=dca_first_timers_export.csv"},
        )
    finally:
        db.close()


# ═══════════════════════════════════════════════════
# ERROR HANDLERS
# ═══════════════════════════════════════════════════
@app.errorhandler(404)
def not_found(e):
    return render_template("404.html"), 404


@app.errorhandler(500)
def server_error(e):
    return render_template("500.html"), 500


# ═══════════════════════════════════════════════════
# RUN
# ═══════════════════════════════════════════════════
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "False").lower() in ("true", "1")
    app.run(host="0.0.0.0", port=port, debug=debug)
