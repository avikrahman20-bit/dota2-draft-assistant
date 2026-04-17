"""SQLite database for user accounts and profiles."""

import json
import sqlite3
import os
from contextlib import contextmanager

DB_PATH = os.path.join(os.path.dirname(__file__), "users.db")


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def get_db():
    conn = _get_conn()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    """Create tables if they don't exist, and migrate schema."""
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                username    TEXT    UNIQUE NOT NULL,
                password_hash TEXT  NOT NULL,
                created_at  TEXT    DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS user_profiles (
                user_id         INTEGER PRIMARY KEY REFERENCES users(id),
                preferred_roles TEXT    DEFAULT '[]',
                hero_pool       TEXT    DEFAULT '[]',
                playstyle_tags  TEXT    DEFAULT '[]',
                playstyle_notes TEXT    DEFAULT '',
                mmr_bracket     TEXT    DEFAULT '',
                custom_weights  TEXT    DEFAULT '{}',
                dota_account_id TEXT    DEFAULT '',
                player_stats    TEXT    DEFAULT '{}',
                updated_at      TEXT    DEFAULT (datetime('now'))
            );

        """)
        # Migrate: add columns that may not exist in older databases
        existing = {row[1] for row in conn.execute("PRAGMA table_info(user_profiles)").fetchall()}
        migrations = {
            "playstyle_tags":  "TEXT DEFAULT '[]'",
            "dota_account_id": "TEXT DEFAULT ''",
            "player_stats":    "TEXT DEFAULT '{}'",
        }
        for col, typedef in migrations.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE user_profiles ADD COLUMN {col} {typedef}")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS chat_feedback (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER REFERENCES users(id),
                hero_id     INTEGER,
                feedback    TEXT    NOT NULL,
                draft_context TEXT   DEFAULT '',
                created_at  TEXT    DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS chat_usage (
                user_id  INTEGER REFERENCES users(id),
                date     TEXT,
                count    INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, date)
            );
        """)


# ── User CRUD ────────────────────────────────────────────────

def create_user(username: str, password_hash: str) -> int:
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO users (username, password_hash) VALUES (?, ?)",
            (username, password_hash),
        )
        user_id = cur.lastrowid
        conn.execute(
            "INSERT INTO user_profiles (user_id) VALUES (?)", (user_id,)
        )
        return user_id


def get_user_by_username(username: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()
        return dict(row) if row else None


# ── Profile CRUD ─────────────────────────────────────────────

def get_profile(user_id: int) -> dict:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM user_profiles WHERE user_id = ?", (user_id,)
        ).fetchone()
        if not row:
            return {}
        profile = dict(row)
        def _loads(val, fallback):
            try:
                return json.loads(val) if val else fallback
            except (json.JSONDecodeError, TypeError):
                return fallback
        profile["preferred_roles"] = _loads(profile["preferred_roles"], [])
        profile["hero_pool"]        = _loads(profile["hero_pool"], [])
        profile["playstyle_tags"]   = _loads(profile.get("playstyle_tags"), [])
        profile["custom_weights"]   = _loads(profile["custom_weights"], {})
        profile["player_stats"]     = _loads(profile.get("player_stats"), {})
        return profile


def update_profile(user_id: int, **fields) -> dict:
    allowed = {
        "preferred_roles", "hero_pool", "playstyle_tags", "playstyle_notes",
        "mmr_bracket", "custom_weights", "dota_account_id", "player_stats",
    }
    updates = {}
    for k, v in fields.items():
        if k not in allowed:
            continue
        if isinstance(v, (list, dict)):
            updates[k] = json.dumps(v)
        else:
            updates[k] = v

    if not updates:
        return get_profile(user_id)

    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [user_id]
    with get_db() as conn:
        conn.execute(
            f"UPDATE user_profiles SET {set_clause}, updated_at = datetime('now') WHERE user_id = ?",
            values,
        )
    return get_profile(user_id)


# ── Feedback CRUD ────────────────────────────────────────────

def add_feedback(user_id: int, hero_id: int | None, feedback: str, draft_context: str = ""):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO chat_feedback (user_id, hero_id, feedback, draft_context) VALUES (?, ?, ?, ?)",
            (user_id, hero_id, feedback, draft_context),
        )


def get_chat_count_today(user_id: int) -> int:
    with get_db() as conn:
        row = conn.execute(
            "SELECT count FROM chat_usage WHERE user_id = ? AND date = date('now')",
            (user_id,)
        ).fetchone()
        return row["count"] if row else 0


def increment_chat_count(user_id: int) -> int:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO chat_usage (user_id, date, count) VALUES (?, date('now'), 1) "
            "ON CONFLICT(user_id, date) DO UPDATE SET count = count + 1",
            (user_id,)
        )
        row = conn.execute(
            "SELECT count FROM chat_usage WHERE user_id = ? AND date = date('now')",
            (user_id,)
        ).fetchone()
        return row["count"]


def get_recent_feedback(user_id: int, limit: int = 20) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM chat_feedback WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]
