"""
database.py — SQLite setup, schema creation, and shared DB helpers for Enigma.
All tables are created here. Cogs import helpers from this module.
"""

import sqlite3
import os
from contextlib import contextmanager

DB_PATH = os.getenv("DB_PATH", "enigma.db")


@contextmanager
def get_db():
    """Context manager yielding a connected SQLite connection with row_factory."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Create all tables if they do not exist."""
    with get_db() as conn:
        conn.executescript("""
        -- ── Guild configuration ──────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS guild_games (
            guild_id    INTEGER NOT NULL,
            game_key    TEXT    NOT NULL,
            enabled     INTEGER NOT NULL DEFAULT 1,
            channel_id  INTEGER,               -- NULL = any channel
            PRIMARY KEY (guild_id, game_key)
        );

        -- ── Points & streaks ─────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS points (
            guild_id    INTEGER NOT NULL,
            user_id     INTEGER NOT NULL,
            game_key    TEXT    NOT NULL,
            points      INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, user_id, game_key)
        );

        CREATE TABLE IF NOT EXISTS solve_times (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id    INTEGER NOT NULL,
            user_id     INTEGER NOT NULL,
            game_key    TEXT    NOT NULL,
            solve_ms    INTEGER NOT NULL,       -- milliseconds
            solved_at   TEXT    NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS streaks (
            guild_id        INTEGER NOT NULL,
            user_id         INTEGER NOT NULL,
            current_streak  INTEGER NOT NULL DEFAULT 0,
            best_streak     INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, user_id)
        );

        -- ── Scheduler ────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS scheduled_posts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id    INTEGER NOT NULL,
            game_key    TEXT    NOT NULL,
            run_at      TEXT    NOT NULL,       -- ISO-8601 UTC
            fired       INTEGER NOT NULL DEFAULT 0,
            payload     TEXT                   -- JSON blob
        );

        -- ── Active rounds (generic) ──────────────────────────────────────
        CREATE TABLE IF NOT EXISTS active_rounds (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id    INTEGER NOT NULL,
            game_key    TEXT    NOT NULL,
            channel_id  INTEGER NOT NULL,
            message_id  INTEGER,
            thread_id   INTEGER,
            state       TEXT    NOT NULL DEFAULT '{}',  -- JSON
            started_at  TEXT    NOT NULL DEFAULT (datetime('now')),
            expires_at  TEXT                            -- NULL = no expiry
        );

        -- ── Submissions / answers ────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS submissions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            round_id    INTEGER NOT NULL REFERENCES active_rounds(id) ON DELETE CASCADE,
            user_id     INTEGER NOT NULL,
            content     TEXT    NOT NULL,
            is_correct  INTEGER,               -- NULL = ungraded
            submitted_at TEXT   NOT NULL DEFAULT (datetime('now'))
        );

        -- ── Votes ────────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS votes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            round_id    INTEGER NOT NULL REFERENCES active_rounds(id) ON DELETE CASCADE,
            user_id     INTEGER NOT NULL,
            target_id   INTEGER NOT NULL,      -- submission id or user id
            voted_at    TEXT    NOT NULL DEFAULT (datetime('now')),
            UNIQUE (round_id, user_id)
        );

        -- ── Member-submitted questions ───────────────────────────────────
        CREATE TABLE IF NOT EXISTS question_pool (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id    INTEGER NOT NULL,
            game_key    TEXT    NOT NULL,
            author_id   INTEGER NOT NULL,
            question    TEXT    NOT NULL,
            answer      TEXT    NOT NULL,
            metadata    TEXT    NOT NULL DEFAULT '{}',  -- JSON (hints, category…)
            upvotes     INTEGER NOT NULL DEFAULT 0,
            times_used  INTEGER NOT NULL DEFAULT 0,
            times_stumped INTEGER NOT NULL DEFAULT 0,
            approved    INTEGER NOT NULL DEFAULT 0,
            submitted_at TEXT   NOT NULL DEFAULT (datetime('now'))
        );

        -- ── Category badges ──────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS category_points (
            guild_id    INTEGER NOT NULL,
            user_id     INTEGER NOT NULL,
            category    TEXT    NOT NULL,
            points      INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, user_id, category)
        );

        -- ── Contributor tracking ─────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS contributions (
            guild_id        INTEGER NOT NULL,
            user_id         INTEGER NOT NULL,
            questions_submitted INTEGER NOT NULL DEFAULT 0,
            facts_submitted     INTEGER NOT NULL DEFAULT 0,
            clips_submitted     INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, user_id)
        );

        CREATE INDEX IF NOT EXISTS idx_points_guild    ON points(guild_id);
        CREATE INDEX IF NOT EXISTS idx_rounds_guild    ON active_rounds(guild_id, game_key);
        CREATE INDEX IF NOT EXISTS idx_sched_pending   ON scheduled_posts(fired, run_at);
        CREATE INDEX IF NOT EXISTS idx_solve_guild     ON solve_times(guild_id, game_key);
        """)


# ── Generic helpers ──────────────────────────────────────────────────────────

def add_points(guild_id: int, user_id: int, game_key: str, amount: int):
    with get_db() as conn:
        conn.execute("""
            INSERT INTO points (guild_id, user_id, game_key, points)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id, game_key)
            DO UPDATE SET points = points + excluded.points
        """, (guild_id, user_id, game_key, amount))


def add_category_points(guild_id: int, user_id: int, category: str, amount: int):
    with get_db() as conn:
        conn.execute("""
            INSERT INTO category_points (guild_id, user_id, category, points)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id, category)
            DO UPDATE SET points = points + excluded.points
        """, (guild_id, user_id, category, amount))


def record_solve_time(guild_id: int, user_id: int, game_key: str, ms: int):
    with get_db() as conn:
        conn.execute("""
            INSERT INTO solve_times (guild_id, user_id, game_key, solve_ms)
            VALUES (?, ?, ?, ?)
        """, (guild_id, user_id, game_key, ms))


def update_streak(guild_id: int, user_id: int, correct: bool) -> int:
    """Update streak; return new current streak value."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT current_streak, best_streak FROM streaks WHERE guild_id=? AND user_id=?",
            (guild_id, user_id)
        ).fetchone()
        cur = (row["current_streak"] if row else 0)
        best = (row["best_streak"] if row else 0)
        cur = cur + 1 if correct else 0
        best = max(best, cur)
        conn.execute("""
            INSERT INTO streaks (guild_id, user_id, current_streak, best_streak)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id)
            DO UPDATE SET current_streak=excluded.current_streak, best_streak=excluded.best_streak
        """, (guild_id, user_id, cur, best))
        return cur


def get_game_config(guild_id: int, game_key: str) -> sqlite3.Row | None:
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM guild_games WHERE guild_id=? AND game_key=?",
            (guild_id, game_key)
        ).fetchone()


def is_game_enabled(guild_id: int, game_key: str) -> bool:
    row = get_game_config(guild_id, game_key)
    return bool(row and row["enabled"])


def get_active_round(guild_id: int, game_key: str) -> sqlite3.Row | None:
    with get_db() as conn:
        return conn.execute("""
            SELECT * FROM active_rounds
            WHERE guild_id=? AND game_key=?
            ORDER BY started_at DESC LIMIT 1
        """, (guild_id, game_key)).fetchone()


def create_active_round(guild_id: int, game_key: str, channel_id: int,
                        state: str = "{}", expires_at: str | None = None) -> int:
    with get_db() as conn:
        cur = conn.execute("""
            INSERT INTO active_rounds (guild_id, game_key, channel_id, state, expires_at)
            VALUES (?, ?, ?, ?, ?)
        """, (guild_id, game_key, channel_id, state, expires_at))
        return cur.lastrowid


def close_active_round(round_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM active_rounds WHERE id=?", (round_id,))


def schedule_post(guild_id: int, game_key: str, run_at: str, payload: str = "{}"):
    with get_db() as conn:
        conn.execute("""
            INSERT INTO scheduled_posts (guild_id, game_key, run_at, payload)
            VALUES (?, ?, ?, ?)
        """, (guild_id, game_key, run_at, payload))


def get_pending_schedules(now_iso: str) -> list:
    with get_db() as conn:
        return conn.execute("""
            SELECT * FROM scheduled_posts
            WHERE fired=0 AND run_at <= ?
            ORDER BY run_at
        """, (now_iso,)).fetchall()


def mark_schedule_fired(schedule_id: int):
    with get_db() as conn:
        conn.execute("UPDATE scheduled_posts SET fired=1 WHERE id=?", (schedule_id,))
