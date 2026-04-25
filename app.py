import os
import csv
import io
from datetime import datetime, timedelta
from functools import wraps

from dotenv import load_dotenv
load_dotenv()

from flask import (
    Flask, render_template, request, redirect, url_for,
    session, flash, jsonify, Response
)
from flask_login import (
    LoginManager, login_user, logout_user, login_required, current_user
)
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy import func as sa_func, desc

from database import engine, SessionLocal, Base
from models import FirstTimer, AdminUser, DayProgress, ActivityLog, Assessment


# ═══════════════════════════════════════════════════
# APP CONFIG
# ═══════════════════════════════════════════════════
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dca-dev-secret-key-change-me")
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=7)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"
login_manager.login_message = "Please log in to access your journey."
login_manager.login_message_category = "info"


# ═══════════════════════════════════════════════════
# CREATE TABLES + SEED ADMIN ON STARTUP
# ═══════════════════════════════════════════════════
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


# ═══════════════════════════════════════════════════
# USER LOADER (dual: first_timers + admins)
# ═══════════════════════════════════════════════════
@login_manager.user_loader
def load_user(user_id):
    db = SessionLocal()
    try:
        prefix, uid = user_id.split(":", 1)
        uid = int(uid)
        if prefix == "ft":
            return db.query(FirstTimer).get(uid)
        elif prefix == "admin":
            return db.query(AdminUser).get(uid)
    except Exception:
        return None
    finally:
        db.close()


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


def get_user_progress(db, user_id):
    """Returns dict {day_number: DayProgress} for a user."""
    rows = db.query(DayProgress).filter_by(user_id=user_id).all()
    return {dp.day_number: dp for dp in rows}


# ═══════════════════════════════════════════════════
# DAY CONTENT (static data from workbook)
# ═══════════════════════════════════════════════════
DAYS_DATA = [
    {
        "day": 1,
        "title": "Identity in Christ",
        "insight": "You are a new creation. Your past no longer defines you.",
        "verse": "2 Corinthians 5:17",
        "questions": ["Who am I in Christ?", "What must I stop believing about myself?"],
        "action": "Walk confidently as God\u2019s child.",
        "checklist": ["Prayer", "Word Study", "Application", "Reflection"],
    },
    {
        "day": 2,
        "title": "Dominion Over Sin",
        "insight": "Sin has no dominion over you.",
        "verse": "Romans 6:14",
        "questions": ["What habits must I overcome?", "What triggers must I avoid?"],
        "action": "Choose righteousness deliberately.",
        "checklist": ["Prayer", "Word Study", "Application", "Reflection"],
    },
    {
        "day": 3,
        "title": "The Word Life",
        "insight": "The Word is your guide and strength.",
        "verse": "Psalm 119:105",
        "questions": ["What did I learn today?", "How can I apply it?"],
        "action": "Speak the Word daily.",
        "checklist": ["Prayer", "Word Study", "Application", "Reflection"],
    },
    {
        "day": 4,
        "title": "Prayer & Fellowship",
        "insight": "Prayer builds intimacy with God.",
        "verse": "1 Thessalonians 5:17",
        "questions": ["How consistent was my prayer life?", "Did I sense God\u2019s presence?"],
        "action": "Pray throughout the day.",
        "checklist": ["Prayer", "Word Study", "Application", "Reflection"],
    },
    {
        "day": 5,
        "title": "Holiness & Character",
        "insight": "Your life must reflect Christ.",
        "verse": "1 Peter 1:15\u201316",
        "questions": ["Which character trait needs growth?", "How did I respond to people?"],
        "action": "Practice love and patience.",
        "checklist": ["Prayer", "Word Study", "Application", "Reflection"],
    },
    {
        "day": 6,
        "title": "Faith & Confession",
        "insight": "Speak and live by faith.",
        "verse": "Romans 10:17",
        "questions": ["What did I declare today?", "Did I speak fear or faith?"],
        "action": "Confess God\u2019s promises boldly.",
        "checklist": ["Prayer", "Word Study", "Application", "Reflection"],
    },
    {
        "day": 7,
        "title": "Purpose & Impact",
        "insight": "You are called to make impact.",
        "verse": "Matthew 5:16",
        "questions": ["Who did I help?", "Did I share Christ?"],
        "action": "Be intentional about impact.",
        "checklist": ["Prayer", "Word Study", "Application", "Reflection"],
    },
]

ASSESSMENT_QUESTIONS = [
    "Am I more spiritually disciplined?",
    "Am I walking in dominion over sin?",
    "Is my relationship with God stronger?",
]


# ═══════════════════════════════════════════════════
# AUTH ROUTES
# ═══════════════════════════════════════════════════
@app.route("/login", methods=["GET", "POST"])
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
                login_user(user, remember=True)
                log_activity(db, user.id, "login")
                return redirect(request.args.get("next") or url_for("dashboard"))

            # Check admins
            admin = db.query(AdminUser).filter_by(email=email).first()
            if admin and admin.check_password(password):
                admin.last_login = datetime.utcnow()
                db.commit()
                login_user(admin, remember=True)
                return redirect(url_for("admin_dashboard"))

            flash("Invalid email or password.", "error")
        finally:
            db.close()

    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
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

        if len(password) < 6:
            flash("Password must be at least 6 characters.", "error")
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
            )
            user.set_password(password)
            db.add(user)
            db.commit()
            db.refresh(user)

            log_activity(db, user.id, "signup")
            login_user(user, remember=True)
            flash(f"Welcome, {first_name}! Your 7-day journey begins now.", "success")
            return redirect(url_for("dashboard"))
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
        progress = get_user_progress(db, current_user.id)
        completed_count = sum(1 for dp in progress.values() if dp.is_completed)
        pct = round((completed_count / 7) * 100)

        # Check if assessment is done
        assessment = db.query(Assessment).filter_by(user_id=current_user.id).first()

        return render_template(
            "dashboard.html",
            days_data=DAYS_DATA,
            progress=progress,
            completed_count=completed_count,
            pct=pct,
            assessment_done=assessment is not None,
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

    db = SessionLocal()
    try:
        progress = get_user_progress(db, current_user.id)

        # Enforce sequential unlock
        if n > 1:
            prev = progress.get(n - 1)
            if not prev or not prev.is_completed:
                flash(f"Please complete Day {n - 1} first.", "error")
                return redirect(url_for("dashboard"))

        # Get or create progress row
        dp = progress.get(n)
        if not dp:
            dp = DayProgress(user_id=current_user.id, day_number=n)
            db.add(dp)
            db.commit()
            db.refresh(dp)
            log_activity(db, current_user.id, "day_started", day_number=n)

        day_data = DAYS_DATA[n - 1]

        return render_template(
            "day_flow.html",
            day_data=day_data,
            progress=dp,
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

    db = SessionLocal()
    try:
        dp = db.query(DayProgress).filter_by(
            user_id=current_user.id, day_number=n
        ).first()

        if not dp:
            dp = DayProgress(user_id=current_user.id, day_number=n)
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
        log_activity(db, current_user.id, "card_answered", day_number=n, card_name=card_name)

        return jsonify({"status": "ok"})
    finally:
        db.close()


@app.route("/day/<int:n>/complete", methods=["POST"])
@first_timer_required
def day_complete(n):
    if n < 1 or n > 7:
        return jsonify({"error": "Invalid day"}), 400

    db = SessionLocal()
    try:
        dp = db.query(DayProgress).filter_by(
            user_id=current_user.id, day_number=n
        ).first()

        if not dp:
            return jsonify({"error": "Day not started"}), 400

        dp.is_completed = True
        dp.completed_at = datetime.utcnow()
        db.commit()

        log_activity(db, current_user.id, "day_completed", day_number=n)

        next_day = n + 1 if n < 7 else None
        return jsonify({"status": "ok", "next_day": next_day})
    finally:
        db.close()


@app.route("/assessment")
@first_timer_required
def assessment():
    db = SessionLocal()
    try:
        progress = get_user_progress(db, current_user.id)
        completed_count = sum(1 for dp in progress.values() if dp.is_completed)

        if completed_count < 7:
            flash("Please complete all 7 days before the final assessment.", "error")
            return redirect(url_for("dashboard"))

        existing = db.query(Assessment).filter_by(user_id=current_user.id).first()
        log_activity(db, current_user.id, "assessment_started")

        return render_template(
            "assessment.html",
            questions=ASSESSMENT_QUESTIONS,
            existing=existing,
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

    db = SessionLocal()
    try:
        existing = db.query(Assessment).filter_by(user_id=current_user.id).first()
        if existing:
            existing.responses = data["responses"]
            existing.completed_at = datetime.utcnow()
        else:
            a = Assessment(user_id=current_user.id, responses=data["responses"])
            db.add(a)

        db.commit()
        log_activity(db, current_user.id, "assessment_completed")

        return jsonify({"status": "ok"})
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

        # Users who completed all 7 days
        from sqlalchemy import and_
        completed_users = 0
        all_users = db.query(FirstTimer).all()
        for u in all_users:
            dp_count = db.query(DayProgress).filter(
                and_(DayProgress.user_id == u.id, DayProgress.is_completed == True)
            ).count()
            if dp_count == 7:
                completed_users += 1

        completion_rate = round((completed_users / total_users * 100)) if total_users > 0 else 0

        # Day-by-day completion counts
        day_stats = []
        for d in range(1, 8):
            count = db.query(DayProgress).filter(
                and_(DayProgress.day_number == d, DayProgress.is_completed == True)
            ).count()
            day_stats.append({"day": d, "count": count})

        # Average days completed
        if total_users > 0:
            total_completed = db.query(DayProgress).filter(
                DayProgress.is_completed == True
            ).count()
            avg_days = round(total_completed / total_users, 1)
        else:
            avg_days = 0

        return render_template(
            "admin_dashboard.html",
            total_users=total_users,
            active_today=active_today,
            completion_rate=completion_rate,
            completed_users=completed_users,
            day_stats=day_stats,
            avg_days=avg_days,
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
                "last_active": last_activity.timestamp if last_activity else u.created_at,
            })

        return render_template(
            "admin_users.html",
            user_data=user_data,
            search=search,
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

        progress = get_user_progress(db, user.id)
        activities = db.query(ActivityLog).filter_by(
            user_id=user.id
        ).order_by(desc(ActivityLog.timestamp)).limit(100).all()

        assessment = db.query(Assessment).filter_by(user_id=user.id).first()

        return render_template(
            "admin_user_detail.html",
            user=user,
            progress=progress,
            activities=activities,
            assessment=assessment,
            days_data=DAYS_DATA,
            assessment_questions=ASSESSMENT_QUESTIONS,
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
            "First Name", "Last Name", "Email", "Phone", "Registered",
        ]
        for d in range(1, 8):
            header.extend([
                f"Day {d} Started", f"Day {d} Completed",
                f"Day {d} Q1", f"Day {d} Q2", f"Day {d} Notes",
                f"Day {d} Checklist",
            ])
        header.extend([
            "Assessment Q1", "Assessment Q2", "Assessment Q3", "Assessment Completed"
        ])
        writer.writerow(header)

        users = db.query(FirstTimer).order_by(FirstTimer.created_at).all()
        for u in users:
            row = [u.first_name, u.last_name, u.email, u.phone or "", str(u.created_at)]

            for d in range(1, 8):
                dp = db.query(DayProgress).filter_by(user_id=u.id, day_number=d).first()
                if dp:
                    answers = dp.answers or {}
                    checklist = dp.checklist or {}
                    checklist_str = ", ".join(
                        DAYS_DATA[d - 1]["checklist"][int(k)]
                        for k, v in checklist.items() if v
                    )
                    row.extend([
                        str(dp.started_at) if dp.started_at else "",
                        str(dp.completed_at) if dp.completed_at else "",
                        answers.get("0", ""),
                        answers.get("1", ""),
                        dp.notes or "",
                        checklist_str,
                    ])
                else:
                    row.extend(["", "", "", "", "", ""])

            assess = db.query(Assessment).filter_by(user_id=u.id).first()
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
