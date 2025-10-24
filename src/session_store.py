#session_store.py

import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

DB_PATH = Path(os.getenv("SESSION_DB_PATH", "data/sessions.sqlite3"))

_DB_INITIALIZED = False


def _ensure_schema() -> None:
    global _DB_INITIALIZED
    if _DB_INITIALIZED:
        return
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS sessions (waid TEXT PRIMARY KEY, payload TEXT NOT NULL)"
        )
        conn.commit()
    _DB_INITIALIZED = True


def load_session(waid: str) -> Optional[dict]:
    _ensure_schema()
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute("SELECT payload FROM sessions WHERE waid = ?", (waid,))
        row = cur.fetchone()
        if not row:
            return None
        try:
            return json.loads(row[0])
        except json.JSONDecodeError:
            log.warning("Invalid session payload for %s; clearing entry", waid)
            conn.execute("DELETE FROM sessions WHERE waid = ?", (waid,))
            conn.commit()
            return None


def save_session(waid: str, session: dict) -> None:
    _ensure_schema()
    payload = json.dumps(session, ensure_ascii=False)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO sessions (waid, payload)
            VALUES (?, ?)
            ON CONFLICT(waid) DO UPDATE SET payload=excluded.payload
            """,
            (waid, payload),
        )
        conn.commit()


def clear_session(waid: str) -> None:
    _ensure_schema()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM sessions WHERE waid = ?", (waid,))
        conn.commit()
