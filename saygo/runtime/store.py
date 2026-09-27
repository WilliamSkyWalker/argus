"""SQLite checkpoints, atomic event history and cooperative resource ownership."""

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time
import uuid


class BusyError(RuntimeError):
    pass


class Store:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "runtime.sqlite3"
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, snapshot TEXT NOT NULL, control TEXT
                );
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                    at REAL NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_run ON events(run_id, seq);
                CREATE TABLE IF NOT EXISTS resources (
                    key TEXT PRIMARY KEY, run_id TEXT NOT NULL
                );
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA busy_timeout=10000")
        try:
            with db:
                yield db
        finally:
            db.close()

    def create(self, workflow, *, mode=None):
        run_id = uuid.uuid4().hex
        state = dict(id=run_id, workflow=workflow, cursor=0, status="queued",
                     outputs={}, observations={}, pending=None, human=None,
                     error=None, created_at=time.time())
        if mode == "interactive":
            state.update(mode=mode, status="idle", requests={})
        with self.connect() as db:
            db.execute("INSERT INTO runs(id,snapshot) VALUES (?,?)", (run_id, json.dumps(state)))
            self._event(db, run_id, "created", {})
        return state

    def get(self, run_id):
        with self.connect() as db:
            row = db.execute("SELECT snapshot FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise ValueError(f"unknown run: {run_id}")
        return json.loads(row[0])

    def save(self, state, kind, data=None, release=False, clear_control=None):
        state["updated_at"] = time.time()
        with self.connect() as db:
            db.execute("UPDATE runs SET snapshot=? WHERE id=?", (json.dumps(state), state["id"]))
            self._event(db, state["id"], kind, data or {})
            if clear_control:
                db.execute("UPDATE runs SET control=NULL WHERE id=? AND control=?", (state["id"], clear_control))
            if release:
                db.execute("DELETE FROM resources WHERE run_id=?", (state["id"],))

    def _event(self, db, run_id, kind, data):
        db.execute("INSERT INTO events(run_id,at,kind,data) VALUES (?,?,?,?)",
                   (run_id, time.time(), kind, json.dumps(data)))

    def events(self, run_id):
        self.get(run_id)
        with self.connect() as db:
            rows = db.execute("SELECT seq,at,kind,data FROM events WHERE run_id=? ORDER BY seq", (run_id,)).fetchall()
        return [dict(seq=s, at=t, kind=k, data=json.loads(d)) for s, t, k, d in rows]

    def reserve(self, run_id, keys):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for key in sorted(set(keys)):
                row = db.execute("SELECT run_id FROM resources WHERE key=?", (key,)).fetchone()
                if row and row[0] != run_id:
                    raise BusyError(f"resource {key} is owned by {row[0]}")
                db.execute("INSERT OR IGNORE INTO resources VALUES (?,?)", (key, run_id))

    def request(self, run_id, command):
        if command not in {"pause", "cancel"}:
            raise ValueError("invalid control request")
        self.get(run_id)
        with self.connect() as db:
            db.execute("UPDATE runs SET control=CASE WHEN control='cancel' THEN control ELSE ? END WHERE id=?",
                       (command, run_id))
            self._event(db, run_id, "control_requested", {"command": command})

    def control(self, run_id):
        with self.connect() as db:
            row = db.execute("SELECT control FROM runs WHERE id=?", (run_id,)).fetchone()
        return row[0]

    @contextmanager
    def guard(self, run_id):
        # OS lock is released on crash. DB status alone cannot identify a dead runner.
        if not isinstance(run_id, str) or len(run_id) != 32 or any(c not in "0123456789abcdef" for c in run_id):
            raise ValueError("invalid run id")
        path = self.root / f"{run_id}.lock"
        with path.open("a+b") as f:
            f.seek(0)
            if not f.read(1):
                f.write(b"0")
                f.flush()
            f.seek(0)
            import os
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise BusyError("run already has an active executor") from exc
            try:
                yield
            finally:
                if os.name == "nt":
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
