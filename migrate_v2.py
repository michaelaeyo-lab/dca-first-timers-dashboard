"""
Migration script: v1 (single-week) → v2 (4-week program)

Run ONCE on Railway Postgres BEFORE deploying the new code.
Preserves all existing user data — tags everything as week 1.

Usage:
    DATABASE_URL=postgresql://... python migrate_v2.py

Or run on Railway:
    railway run python migrate_v2.py
"""

import os
import sys
from dotenv import load_dotenv

load_dotenv()

database_url = (
    os.environ.get("DATABASE_URL") or
    os.environ.get("LOCAL_DATABASE_URL", "")
).strip()

if not database_url:
    print("ERROR: No DATABASE_URL or LOCAL_DATABASE_URL set.")
    sys.exit(1)

if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)

from sqlalchemy import create_engine, text

engine = create_engine(database_url)

MIGRATION_STEPS = [
    # ── 1. Add current_week to first_timers ──
    {
        "desc": "Add current_week column to first_timers",
        "check": """
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'first_timers' AND column_name = 'current_week'
        """,
        "sql": """
            ALTER TABLE first_timers
            ADD COLUMN current_week INTEGER NOT NULL DEFAULT 1
        """,
    },
    # ── 2. Add week_number to day_progress ──
    {
        "desc": "Add week_number column to day_progress",
        "check": """
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'day_progress' AND column_name = 'week_number'
        """,
        "sql": """
            ALTER TABLE day_progress
            ADD COLUMN week_number INTEGER NOT NULL DEFAULT 1
        """,
    },
    # ── 3. Drop old unique constraint on day_progress (user_id, day_number) ──
    {
        "desc": "Drop old uq_user_day constraint from day_progress",
        "check": """
            SELECT constraint_name FROM information_schema.table_constraints
            WHERE table_name = 'day_progress' AND constraint_name = 'uq_user_day'
        """,
        "sql": """
            ALTER TABLE day_progress DROP CONSTRAINT uq_user_day
        """,
        "skip_if_missing": True,
    },
    # ── 4. Add new composite unique constraint (user_id, week_number, day_number) ──
    {
        "desc": "Add uq_user_week_day constraint to day_progress",
        "check": """
            SELECT constraint_name FROM information_schema.table_constraints
            WHERE table_name = 'day_progress' AND constraint_name = 'uq_user_week_day'
        """,
        "sql": """
            ALTER TABLE day_progress
            ADD CONSTRAINT uq_user_week_day UNIQUE (user_id, week_number, day_number)
        """,
        "skip_if_exists": True,
    },
    # ── 5. Add week_number to assessments ──
    {
        "desc": "Add week_number column to assessments",
        "check": """
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'assessments' AND column_name = 'week_number'
        """,
        "sql": """
            ALTER TABLE assessments
            ADD COLUMN week_number INTEGER NOT NULL DEFAULT 1
        """,
    },
    # ── 6. Drop old unique constraint on assessments (user_id only) ──
    # The old model used unique=True on user_id, Postgres auto-names this
    {
        "desc": "Drop old unique constraint on assessments.user_id",
        "check": """
            SELECT con.conname
            FROM pg_constraint con
            JOIN pg_class rel ON rel.oid = con.conrelid
            WHERE rel.relname = 'assessments'
              AND con.contype = 'u'
              AND con.conname != 'uq_user_week_assessment'
        """,
        "sql_dynamic": True,
    },
    # ── 7. Add new composite unique constraint (user_id, week_number) ──
    {
        "desc": "Add uq_user_week_assessment constraint to assessments",
        "check": """
            SELECT constraint_name FROM information_schema.table_constraints
            WHERE table_name = 'assessments' AND constraint_name = 'uq_user_week_assessment'
        """,
        "sql": """
            ALTER TABLE assessments
            ADD CONSTRAINT uq_user_week_assessment UNIQUE (user_id, week_number)
        """,
        "skip_if_exists": True,
    },
]


def run_migration():
    print(f"Connecting to database...")
    print(f"URL: {database_url[:30]}...\n")

    with engine.begin() as conn:
        # Verify tables exist
        result = conn.execute(text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name IN ('first_timers', 'day_progress', 'assessments')"
        ))
        tables = [r[0] for r in result]
        print(f"Found tables: {tables}")

        if not tables:
            print("\nNo tables found. If this is a fresh deploy, just run the app — create_all will handle it.")
            return

        # Count existing data
        for tbl in ["first_timers", "day_progress", "assessments"]:
            if tbl in tables:
                count = conn.execute(text(f"SELECT COUNT(*) FROM {tbl}")).scalar()
                print(f"  {tbl}: {count} rows")
        print()

        # Run each step
        for i, step in enumerate(MIGRATION_STEPS, 1):
            desc = step["desc"]
            print(f"[{i}/{len(MIGRATION_STEPS)}] {desc}...")

            # Check if already done
            check_result = conn.execute(text(step["check"])).fetchall()

            if step.get("skip_if_exists") and check_result:
                print(f"  SKIP — already exists\n")
                continue

            if step.get("skip_if_missing") and not check_result:
                print(f"  SKIP — constraint not found (may already be dropped)\n")
                continue

            # For adding columns: skip if column already exists
            if "ADD COLUMN" in step.get("sql", "") and check_result:
                print(f"  SKIP — column already exists\n")
                continue

            # Dynamic SQL for dropping unknown constraint names
            if step.get("sql_dynamic"):
                if not check_result:
                    print(f"  SKIP — no old constraints to drop\n")
                    continue
                for row in check_result:
                    constraint_name = row[0]
                    print(f"  Dropping constraint: {constraint_name}")
                    conn.execute(text(f"ALTER TABLE assessments DROP CONSTRAINT {constraint_name}"))
                print(f"  DONE\n")
                continue

            # Standard SQL execution
            conn.execute(text(step["sql"]))
            print(f"  DONE\n")

    print("=" * 50)
    print("Migration complete! All existing data preserved.")
    print("Existing users are tagged as Week 1.")
    print("Deploy the new code now.")


if __name__ == "__main__":
    run_migration()
