"""Durable HTTP task identities and observable late worker completion.

This journal records execution, not money. The injected runtime still reserves
and settles every provider request in its existing append-only budget ledger.
No recorded task is resumed or automatically retried after a process restart.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from arknights_agent import services
from arknights_agent.errors import Conflict, NotFound
from arknights_agent.runtime.security import contains_secret, redact

_ACTIVE = {"queued", "running", "waiting"}
_TERMINAL = {"completed", "failed", "unknown"}
_OWNER = uuid.uuid4().hex
_EXECUTORS = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ark-http-tasks")


def _now():
    return datetime.now(UTC).isoformat()


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class ChatTaskStore:
    """Serialize claims across threads/processes; never re-enqueue old rows."""

    def __init__(self, path, *, isolate_failures=False):
        self.path = Path(path).resolve()
        self.isolate_failures = isolate_failures
        self._initialize_lock = threading.Lock()
        self._initialized = False

    @contextmanager
    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        with self._initialize_lock:
            if not self._initialized:
                db.execute("""CREATE TABLE IF NOT EXISTS http_chat_task (
                    task_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
                    request_hash TEXT NOT NULL, payload TEXT NOT NULL,
                    state TEXT NOT NULL, reason TEXT, response TEXT,
                    owner TEXT NOT NULL, owner_pid INTEGER NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                )""")
                db.execute("""CREATE INDEX IF NOT EXISTS http_chat_task_conversation
                    ON http_chat_task(conversation_id, state)""")
                db.execute("""CREATE TABLE IF NOT EXISTS http_chat_task_progress (
                    task_id TEXT PRIMARY KEY, snapshot TEXT NOT NULL
                )""")
                self._initialized = True
        try:
            yield db
        finally:
            db.close()

    def _recover(self, db):
        rows = db.execute(
            "SELECT task_id, owner_pid FROM http_chat_task "
            "WHERE state IN ('queued','running','waiting') AND owner != ?", (_OWNER,)
        ).fetchall()
        for row in rows:
            if not _alive(row["owner_pid"]):
                db.execute(
                    "UPDATE http_chat_task SET state='unknown', "
                    "reason='worker_interrupted', updated_at=? WHERE task_id=? "
                    "AND state IN ('queued','running','waiting')",
                    (_now(), row["task_id"]),
                )

    @staticmethod
    def _decode(row):
        if row is None:
            raise NotFound("chat task was not found")
        value = dict(row)
        value["payload"] = json.loads(value["payload"])
        value["response"] = json.loads(value["response"]) if value["response"] else None
        if value.get("progress"):
            value["progress"] = json.loads(value["progress"])
        return value

    def find(self, task_id):
        if not self.path.exists():
            return None
        with self._connect() as db:
            self._recover(db)
            row = db.execute(
                "SELECT t.*, p.snapshot AS progress FROM http_chat_task t "
                "LEFT JOIN http_chat_task_progress p USING(task_id) WHERE t.task_id=?", (task_id,)
            ).fetchone()
            return self._decode(row) if row else None

    def get(self, task_id):
        row = self.find(task_id)
        if row is None:
            raise NotFound("chat task was not found")
        return row

    def claim(self, task_id, conversation_id, request_hash, payload):
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                self._recover(db)
                row = db.execute(
                    "SELECT * FROM http_chat_task WHERE task_id=?", (task_id,)
                ).fetchone()
                if row:
                    if row["request_hash"] != request_hash:
                        raise Conflict("task_id_conflict")
                    db.commit()
                    return self._decode(row), False
                pending = db.execute(
                    "SELECT task_id FROM http_chat_task WHERE conversation_id=? "
                    "AND (state IN ('queued','running','waiting') OR (state='unknown' AND ?)) LIMIT 1",
                    (conversation_id, not self.isolate_failures),
                ).fetchone()
                if pending:
                    raise Conflict("task_pending:" + pending["task_id"])
                timestamp = _now()
                db.execute(
                    "INSERT INTO http_chat_task VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (task_id, conversation_id, request_hash, services.canonical_json(payload),
                     "queued", None, None, _OWNER, os.getpid(), timestamp, timestamp),
                )
                db.commit()
            except BaseException:
                db.rollback()
                raise
        return self.get(task_id), True

    def update(self, task_id, state, *, reason=None, response=None):
        if state not in _ACTIVE | _TERMINAL:
            raise ValueError("invalid task state")
        with self._connect() as db:
            db.execute(
                "UPDATE http_chat_task SET state=?, reason=?, response=?, updated_at=? "
                "WHERE task_id=? AND state IN ('queued','running','waiting')",
                (state, reason, services.canonical_json(response) if response is not None else None,
                 _now(), task_id),
            )
        return self.get(task_id)

    def publish_progress(self, task_id, snapshot):
        phase = snapshot.get("phase")
        if phase not in {"planning", "searching", "writing", "reviewing", "revising", "saving"}:
            return
        # Progress is best effort and cannot change the task's execution or billing.
        try:
            with self._connect() as db:
                db.execute(
                    "INSERT INTO http_chat_task_progress(task_id,snapshot) "
                    "SELECT task_id, ? FROM http_chat_task WHERE task_id=? AND owner=? "
                    "AND state IN ('running','waiting') "
                    "ON CONFLICT(task_id) DO UPDATE SET snapshot=excluded.snapshot",
                    (services.canonical_json(snapshot), task_id, _OWNER),
                )
        except sqlite3.Error:
            pass

    def public(self, task_id):
        row = self.get(task_id)
        payload = row["payload"]
        terminal = row["state"] in _TERMINAL
        return {
            "task_id": row["task_id"], "conversation_id": row["conversation_id"],
            "state": row["state"], "terminal": terminal,
            "blocked": row["state"] == "unknown", "reason": row["reason"],
            "continuation_allowed": terminal and self.isolate_failures,
            "error_code": row["reason"] if row["state"] in {"failed", "unknown"} else None,
            "response": row["response"], "created_at": row["created_at"],
            "updated_at": row["updated_at"], "release_id": payload["release_id"],
            "previous_run_id": payload.get("previous_run_id"),
            "question": payload["question"], "module": payload["module"],
            "backend": payload["backend"],
            "progress": row.get("progress") if not terminal else None,
            "poll_after_ms": None if terminal else 1000,
        }


class ChatTaskService:
    """Observe the whole worker and audit its returned answer exactly once."""

    def __init__(self, store, execute, audit):
        self.store, self.execute, self.audit = store, execute, audit
        self._finish_lock = threading.Lock()

    def submit(self, task_id, conversation_id, request_hash, payload, runtime):
        row, created = self.store.claim(task_id, conversation_id, request_hash, payload)
        if created:
            try:
                _EXECUTORS.submit(self._dispatch, task_id, payload, runtime)
            except Exception:
                # A failed enqueue is recorded, never silently recreated by
                # the next POST using a new process or request identity.
                self._fail(task_id, "worker_submission_failed")
        return self.store.public(row["task_id"])

    def _dispatch(self, task_id, payload, runtime):
        self.store.update(task_id, "running")
        observed = False

        def observe(worker):
            nonlocal observed
            observed = True
            worker.add_done_callback(lambda future: self._collect(task_id, payload, future))

        try:
            result = self.execute(payload, {
                **runtime, "publish_progress": lambda snapshot: self.store.publish_progress(task_id, snapshot),
            }, observe)
            if result.get("status") == "timeout":
                self.store.update(task_id, "waiting", reason="worker_still_running")
            elif not observed:
                self._finish(task_id, payload, result)
        except Exception:
            self._fail(task_id, "worker_error")

    def _collect(self, task_id, payload, future):
        try:
            result = future.result()
        except Exception:
            self._fail(task_id, "worker_error")
        else:
            try:
                self._finish(task_id, payload, result)
            except Exception:
                self._fail(task_id, "worker_result_unverified")

    def _fail(self, task_id, code):
        with self._finish_lock:
            row = self.store.get(task_id)
            if row["state"] in _TERMINAL:
                return
            response = None
            if self.store.isolate_failures:
                payload = row["payload"]
                # Preserve the question and authentic parent without turning
                # an interrupted execution into a factual answer.
                failure = {**{k: payload[k] for k in ("question", "module", "release_id", "manifest_hash")},
                    "status": "needs_clarification", "answer": "本次查询未完成。", "claims": [], "citations": [],
                    "agent_runtime": {"requested": payload["backend"], "used": "local",
                        "server_committed": False, "reason": code,
                        "http_task": {"task_id": task_id, "conversation_id": row["conversation_id"]}}}
                try:
                    response = self.audit(payload, failure)
                except Exception:
                    pass
            self.store.update(task_id, "unknown", reason=code, response=response)

    def _finish(self, task_id, payload, result):
        with self._finish_lock:
            row = self.store.get(task_id)
            if row["state"] in _TERMINAL:
                return
            if (
                not isinstance(result, dict)
                or result.get("status") == "timeout"
                or result.get("question") != payload["question"]
                or result.get("release_id") != payload["release_id"]
                or result.get("manifest_hash") != payload["manifest_hash"]
                or contains_secret(result) or redact(result) != result
            ):
                self.store.update(task_id, "unknown", reason="worker_result_unverified")
                return
            trace = result.get("agent_runtime", {})
            committed = bool(trace.get("server_committed"))
            if committed and trace.get("submission_mode") in {
                "verified_query_direct", "verified_query_semantic_review",
            }:
                body = {key: value for key, value in result.items()
                        if key not in {"agent_runtime", "run_id"}}
                if trace.get("committed_answer_hash") != services.content_digest(body):
                    self.store.update(task_id, "unknown", reason="worker_commit_hash_mismatch")
                    return
            reason = trace.get("reason") or "worker_completed"
            state = "completed"
            if payload["backend"] == "deepseek" and not committed:
                budget = trace.get("budget", {})
                state = "unknown" if budget.get("frozen") or budget.get("reservation_state") == "unknown" else "failed"
            result.setdefault("agent_runtime", {})["http_task"] = {
                "task_id": task_id, "conversation_id": row["conversation_id"],
                "late_completion": row["state"] == "waiting",
            }
            progress = row.get("progress") or {}
            if state == "completed":
                if progress.get("sources"):
                    result["agent_runtime"]["public_source_candidates"] = progress["sources"]
                if progress.get("steps"):
                    result["agent_runtime"]["public_verification_steps"] = progress["steps"]
            try:
                audited = self.audit(payload, result)
            except Exception:
                self.store.update(task_id, "unknown", reason="answer_audit_failed")
                return
            self.store.update(task_id, state, reason=reason, response=audited)
