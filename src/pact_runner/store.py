"""What the Runner remembers between runs and restarts: each task's Claude session, runs per day,
and the last quota reading. A small sqlite file in the state directory."""

import json
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Session:
    task_id: str
    session_id: str
    workdir: str
    role_hash: str


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(
            """CREATE TABLE IF NOT EXISTS sessions (
                 task_id text PRIMARY KEY, session_id text NOT NULL, workdir text NOT NULL, role_hash text NOT NULL,
                 updated_at text NOT NULL DEFAULT CURRENT_TIMESTAMP);
               CREATE TABLE IF NOT EXISTS runs (day text PRIMARY KEY, count integer NOT NULL);
               CREATE TABLE IF NOT EXISTS state (key text PRIMARY KEY, value text NOT NULL);"""
        )
        self.db.commit()

    def session(self, task_id: str) -> Session | None:
        row = self.db.execute(
            "SELECT task_id, session_id, workdir, role_hash FROM sessions WHERE task_id = ?", (task_id,)
        ).fetchone()
        return Session(*row) if row else None

    def save_session(self, s: Session) -> None:
        self.db.execute(
            """INSERT INTO sessions (task_id, session_id, workdir, role_hash) VALUES (?, ?, ?, ?)
               ON CONFLICT (task_id) DO UPDATE SET session_id = excluded.session_id, workdir = excluded.workdir,
                 role_hash = excluded.role_hash, updated_at = CURRENT_TIMESTAMP""",
            (s.task_id, s.session_id, s.workdir, s.role_hash),
        )
        self.db.commit()

    def forget_session(self, task_id: str) -> None:
        self.db.execute("DELETE FROM sessions WHERE task_id = ?", (task_id,))
        self.db.commit()

    def runs_today(self, today: date | None = None) -> int:
        row = self.db.execute("SELECT count FROM runs WHERE day = ?", ((today or date.today()).isoformat(),)).fetchone()
        return int(row[0]) if row else 0

    def count_run(self, today: date | None = None) -> None:
        self.db.execute(
            "INSERT INTO runs (day, count) VALUES (?, 1) ON CONFLICT (day) DO UPDATE SET count = count + 1",
            ((today or date.today()).isoformat(),),
        )
        self.db.commit()

    def get(self, key: str) -> Any:
        row = self.db.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key: str, value: Any) -> None:
        self.db.execute(
            "INSERT INTO state (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )
        self.db.commit()

    def close(self) -> None:
        self.db.close()
