"""Transactional job state, result, event and idempotency persistence."""

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone

TERMINAL = {"succeeded", "failed", "cancelled", "timed_out", "interrupted"}


def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class Conflict(Exception):
    pass


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, key TEXT UNIQUE NOT NULL, digest TEXT NOT NULL, state TEXT NOT NULL)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS events (job TEXT, seq INTEGER, body TEXT, PRIMARY KEY(job,seq))"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS artifacts (id TEXT PRIMARY KEY, body TEXT)"
        )
        self.db.commit()

    def get(self, job_id):
        row = self.db.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def by_key(self, key):
        row = self.db.execute("SELECT state FROM jobs WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def event(self, state):
        seq = self.db.execute(
            "SELECT COALESCE(MAX(seq),0)+1 FROM events WHERE job=?", (state["job_id"],)
        ).fetchone()[0]
        body = {
            "seq": seq,
            "time": now(),
            "level": "info",
            "type": "status_changed",
            "message": state["status"],
            "data": {"status": state["status"]},
        }
        self.db.execute(
            "INSERT INTO events VALUES (?,?,?)",
            (state["job_id"], seq, json.dumps(body)),
        )

    def create(self, request):
        encoded = json.dumps(
            request, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        with self.db:
            row = self.db.execute(
                "SELECT digest,state FROM jobs WHERE key=?",
                (request["idempotency_key"],),
            ).fetchone()
            if row:
                if row[0] != digest:
                    raise Conflict()
                return json.loads(row[1]), False
            state = {
                **request,
                "job_id": uuid.uuid4().hex,
                "status": "queued",
                "created_at": now(),
                "started_at": None,
                "finished_at": None,
                "error": None,
                "result": None,
            }
            self.db.execute(
                "INSERT INTO jobs VALUES (?,?,?,?)",
                (
                    state["job_id"],
                    request["idempotency_key"],
                    digest,
                    json.dumps(state),
                ),
            )
            self.event(state)
        return state, True

    def transition(self, job_id, status, *, result=None, error=None):
        with self.db:
            state = self.get(job_id)
            if state["status"] in TERMINAL:
                return state
            state.update(status=status, error=error)
            if status == "running":
                state["started_at"] = now()
            if status in TERMINAL:
                state["finished_at"] = now()
                state["result"] = result
            self.db.execute(
                "UPDATE jobs SET state=? WHERE id=?", (json.dumps(state), job_id)
            )
            self.event(state)
        return state

    def recover(self):
        ids = [row[0] for row in self.db.execute("SELECT id FROM jobs")]
        for job_id in ids:
            if self.get(job_id)["status"] not in TERMINAL:
                self.transition(
                    job_id,
                    "interrupted",
                    error={
                        "code": "JOB_INTERRUPTED",
                        "message": "Bridge stopped before completion; execution was not replayed",
                        "retryable": False,
                    },
                )

    def events(self, job_id, after, limit):
        return [
            json.loads(row[0])
            for row in self.db.execute(
                "SELECT body FROM events WHERE job=? AND seq>? ORDER BY seq LIMIT ?",
                (job_id, after, limit),
            )
        ]

    def progress(self, job_id, value):
        with self.db:
            state = self.get(job_id)
            if state["status"] in TERMINAL:
                return
            state["progress"] = value
            self.db.execute(
                "UPDATE jobs SET state=? WHERE id=?", (json.dumps(state), job_id)
            )
            seq = self.db.execute(
                "SELECT COALESCE(MAX(seq),0)+1 FROM events WHERE job=?", (job_id,)
            ).fetchone()[0]
            event = {
                "seq": seq,
                "time": now(),
                "level": "info",
                "type": "progress",
                "message": value.get("message", value.get("stage", "")),
                "data": value,
            }
            self.db.execute(
                "INSERT INTO events VALUES (?,?,?)", (job_id, seq, json.dumps(event))
            )

    def clean(self, cutoff):
        removed_artifacts = []
        with self.db:
            for job_id, state_json in self.db.execute(
                "SELECT id,state FROM jobs"
            ).fetchall():
                state = json.loads(state_json)
                if state["status"] in TERMINAL and state["finished_at"] < cutoff:
                    self.db.execute("DELETE FROM events WHERE job=?", (job_id,))
                    self.db.execute("DELETE FROM jobs WHERE id=?", (job_id,))
            for artifact_id, body in self.db.execute(
                "SELECT id,body FROM artifacts"
            ).fetchall():
                created = json.loads(body).get("created_at")
                if created and datetime.fromisoformat(created) < datetime.fromisoformat(
                    cutoff.replace("Z", "+00:00")
                ):
                    self.db.execute("DELETE FROM artifacts WHERE id=?", (artifact_id,))
                    removed_artifacts.append(artifact_id)
        return removed_artifacts
