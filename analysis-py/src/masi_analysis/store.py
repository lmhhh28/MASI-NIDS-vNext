"""Bounded private task, artifact, and trace persistence."""

from __future__ import annotations

import os
import sqlite3
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from .canonical import compute_artifact_digest, compute_input_digest, file_digest, go_json_bytes
from .errors import AnalysisError
from .models import AnalysisArtifact, FrozenInput

_TERMINAL = {
    "TASK_STATE_COMPLETED",
    "TASK_STATE_FAILED",
    "TASK_STATE_CANCELED",
    "TASK_STATE_REJECTED",
    "TASK_STATE_INPUT_REQUIRED",
    "TASK_STATE_AUTH_REQUIRED",
}


@dataclass(frozen=True, slots=True)
class TaskRecord:
    task_id: str
    context_id: str
    requester_identity: str
    idempotency_key: str
    request_digest: str
    input_digest: str
    plugin_id: str
    binding_generation: int
    state: str
    reason_code: str
    input_bundle: FrozenInput
    artifact: AnalysisArtifact | None
    created_at_unix_ms: int
    updated_at_unix_ms: int


@dataclass(frozen=True, slots=True)
class TraceRecord:
    task_id: str
    sequence: int
    code: str
    occurred_at_unix_ms: int
    detail_digest: str


class TaskStore:
    def __init__(self, path: Path, *, max_tasks: int, retention_seconds: int, max_trace_events: int) -> None:
        self._path = path
        self._max_tasks = max_tasks
        self._retention_ms = retention_seconds * 1000
        self._max_trace_events = max_trace_events
        self._lock = threading.RLock()
        self._prepare_private_file(path)
        self._connection = sqlite3.connect(path, timeout=2.0, check_same_thread=False, isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS analysis_tasks (
              task_id TEXT PRIMARY KEY,
              context_id TEXT NOT NULL,
              requester_identity TEXT NOT NULL,
              idempotency_key TEXT NOT NULL UNIQUE,
              request_digest TEXT NOT NULL,
              input_digest TEXT NOT NULL,
              plugin_id TEXT NOT NULL,
              binding_generation INTEGER NOT NULL CHECK(binding_generation >= 1),
              state TEXT NOT NULL,
              reason_code TEXT NOT NULL,
              input_json BLOB NOT NULL,
              created_at_unix_ms INTEGER NOT NULL,
              updated_at_unix_ms INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS analysis_artifacts (
              artifact_id TEXT PRIMARY KEY,
              task_id TEXT NOT NULL UNIQUE REFERENCES analysis_tasks(task_id),
              artifact_digest TEXT NOT NULL UNIQUE,
              body BLOB NOT NULL,
              created_at_unix_ms INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS analysis_trace_events (
              task_id TEXT NOT NULL REFERENCES analysis_tasks(task_id),
              sequence INTEGER NOT NULL,
              code TEXT NOT NULL,
              occurred_at_unix_ms INTEGER NOT NULL,
              detail_digest TEXT NOT NULL,
              PRIMARY KEY(task_id, sequence)
            );
            CREATE INDEX IF NOT EXISTS analysis_tasks_terminal_age_idx
              ON analysis_tasks(state, updated_at_unix_ms);
            """
        )
        columns = {str(row[1]) for row in self._connection.execute("PRAGMA table_info(analysis_tasks)").fetchall()}
        if "requester_identity" not in columns:
            self._connection.close()
            raise AnalysisError("PRIVATE_STATE_VERSION_UNSUPPORTED", "private task schema predates requester isolation", 503)
        self._verify_private_file(path)
        self._recover_incomplete()

    @staticmethod
    def _prepare_private_file(path: Path) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags, 0o600)
        except FileExistsError:
            TaskStore._verify_private_file(path)
        except OSError as exc:
            raise AnalysisError("STORE_PATH_REJECTED", "private task store could not be created safely", 503) from exc
        else:
            os.close(descriptor)
            TaskStore._verify_private_file(path)

    @staticmethod
    def _verify_private_file(path: Path) -> None:
        try:
            info = path.lstat()
        except OSError as exc:
            raise AnalysisError("STORE_PATH_REJECTED", "private task store unavailable", 503) from exc
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise AnalysisError("STORE_PATH_REJECTED", "private task store owner, mode, or file type rejected", 503)

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _recover_incomplete(self) -> None:
        now_ms = time.time_ns() // 1_000_000
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._connection.execute(
                    """UPDATE analysis_tasks SET state='TASK_STATE_FAILED', reason_code='RECOVERED_INCOMPLETE',
                       updated_at_unix_ms=? WHERE state NOT IN
                       ('TASK_STATE_COMPLETED','TASK_STATE_FAILED','TASK_STATE_CANCELED','TASK_STATE_REJECTED',
                        'TASK_STATE_INPUT_REQUIRED','TASK_STATE_AUTH_REQUIRED')""",
                    (now_ms,),
                )
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

    def _purge_expired(self, now_ms: int) -> None:
        cutoff = now_ms - self._retention_ms
        rows = self._connection.execute(
            "SELECT task_id FROM analysis_tasks WHERE updated_at_unix_ms < ? AND state IN "
            "('TASK_STATE_COMPLETED','TASK_STATE_FAILED','TASK_STATE_CANCELED','TASK_STATE_REJECTED',"
            "'TASK_STATE_INPUT_REQUIRED','TASK_STATE_AUTH_REQUIRED') ORDER BY updated_at_unix_ms LIMIT 512",
            (cutoff,),
        ).fetchall()
        for row in rows:
            task_id = str(row["task_id"])
            self._connection.execute("DELETE FROM analysis_trace_events WHERE task_id=?", (task_id,))
            self._connection.execute("DELETE FROM analysis_artifacts WHERE task_id=?", (task_id,))
            self._connection.execute("DELETE FROM analysis_tasks WHERE task_id=?", (task_id,))

    def submit(
        self,
        input_bundle: FrozenInput,
        request_digest: str,
        context_id: str,
        requester_identity: str,
    ) -> tuple[TaskRecord, bool]:
        if not requester_identity or len(requester_identity) > 253:
            raise AnalysisError("IDENTITY_MISMATCH", "requester identity rejected", 401)
        now_ms = time.time_ns() // 1_000_000
        input_raw = go_json_bytes(input_bundle.model_dump(mode="json"))
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._purge_expired(now_ms)
                existing = self._connection.execute(
                    "SELECT task_id FROM analysis_tasks WHERE idempotency_key=? OR task_id=?",
                    (input_bundle.idempotency_key, input_bundle.task_id),
                ).fetchone()
                if existing is not None:
                    record = self._get_locked(str(existing["task_id"]))
                    if record.requester_identity != requester_identity:
                        raise AnalysisError("TASK_NOT_FOUND", "task not found", 404)
                    if (
                        record.task_id != input_bundle.task_id
                        or record.idempotency_key != input_bundle.idempotency_key
                        or record.request_digest != request_digest
                        or record.input_digest != input_bundle.input_digest
                    ):
                        raise AnalysisError("IDEMPOTENCY_CONFLICT", "task identity was reused with different input", 409)
                    self._connection.execute("COMMIT")
                    return record, False
                count = int(self._connection.execute("SELECT count(*) FROM analysis_tasks").fetchone()[0])
                if count >= self._max_tasks:
                    raise AnalysisError("RESOURCE_EXHAUSTED", "private task store reached its bound", 429, True)
                self._connection.execute(
                    """INSERT INTO analysis_tasks(task_id,context_id,requester_identity,idempotency_key,request_digest,input_digest,
                       plugin_id,binding_generation,state,reason_code,input_json,created_at_unix_ms,updated_at_unix_ms)
                       VALUES(?,?,?,?,?,?,?,?,'TASK_STATE_SUBMITTED','A2A_SUBMITTED',?,?,?)""",
                    (
                        input_bundle.task_id,
                        context_id,
                        requester_identity,
                        input_bundle.idempotency_key,
                        request_digest,
                        input_bundle.input_digest,
                        input_bundle.plugin_id,
                        input_bundle.binding_generation,
                        input_raw,
                        now_ms,
                        now_ms,
                    ),
                )
                self._connection.execute("COMMIT")
                return self.get(input_bundle.task_id), True
            except Exception:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise

    def set_state(self, task_id: str, state: str, reason_code: str) -> TaskRecord:
        if state not in _TERMINAL | {"TASK_STATE_SUBMITTED", "TASK_STATE_WORKING"}:
            raise AnalysisError("INTERNAL_UNAVAILABLE", "invalid private task transition", 500)
        now_ms = time.time_ns() // 1_000_000
        with self._lock:
            tag = self._connection.execute(
                "UPDATE analysis_tasks SET state=?,reason_code=?,updated_at_unix_ms=? WHERE task_id=?",
                (state, reason_code, now_ms, task_id),
            )
            if tag.rowcount != 1:
                raise AnalysisError("TASK_NOT_FOUND", "task not found", 404)
            return self._get_locked(task_id)

    def complete(self, task_id: str, artifact: AnalysisArtifact) -> TaskRecord:
        body = go_json_bytes(artifact.model_dump(mode="json"))
        now_ms = time.time_ns() // 1_000_000
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._connection.execute("SELECT state FROM analysis_tasks WHERE task_id=?", (task_id,)).fetchone()
                if row is None:
                    raise AnalysisError("TASK_NOT_FOUND", "task not found", 404)
                if str(row["state"]) in _TERMINAL:
                    existing = self._connection.execute("SELECT artifact_digest FROM analysis_artifacts WHERE task_id=?", (task_id,)).fetchone()
                    if existing is None or str(existing["artifact_digest"]) != artifact.artifact_digest:
                        raise AnalysisError("IDEMPOTENCY_CONFLICT", "terminal task cannot be overwritten", 409)
                    self._connection.execute("COMMIT")
                    return self.get(task_id)
                self._connection.execute(
                    "INSERT INTO analysis_artifacts(artifact_id,task_id,artifact_digest,body,created_at_unix_ms) VALUES(?,?,?,?,?)",
                    (artifact.artifact_id, task_id, artifact.artifact_digest, body, now_ms),
                )
                self._connection.execute(
                    "UPDATE analysis_tasks SET state='TASK_STATE_COMPLETED',reason_code=?,updated_at_unix_ms=? WHERE task_id=?",
                    ("ANALYSIS_" + artifact.analysis_outcome.upper(), now_ms, task_id),
                )
                self._connection.execute("COMMIT")
                return self.get(task_id)
            except Exception:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise

    def append_trace(self, task_id: str, code: str, detail: bytes = b"") -> TraceRecord:
        now_ms = time.time_ns() // 1_000_000
        with self._lock:
            sequence = int(self._connection.execute("SELECT COALESCE(max(sequence),0)+1 FROM analysis_trace_events WHERE task_id=?", (task_id,)).fetchone()[0])
            if sequence > self._max_trace_events:
                raise AnalysisError("RESOURCE_EXHAUSTED", "task trace event bound reached", 429)
            digest = file_digest(detail)
            self._connection.execute(
                "INSERT INTO analysis_trace_events(task_id,sequence,code,occurred_at_unix_ms,detail_digest) VALUES(?,?,?,?,?)",
                (task_id, sequence, code, now_ms, digest),
            )
            return TraceRecord(task_id, sequence, code, now_ms, digest)

    def traces(self, task_id: str) -> list[TraceRecord]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT task_id,sequence,code,occurred_at_unix_ms,detail_digest FROM analysis_trace_events WHERE task_id=? ORDER BY sequence LIMIT ?",
                (task_id, self._max_trace_events),
            ).fetchall()
            return [
                TraceRecord(str(row["task_id"]), int(row["sequence"]), str(row["code"]), int(row["occurred_at_unix_ms"]), str(row["detail_digest"]))
                for row in rows
            ]

    def traces_for_requester(self, task_id: str, requester_identity: str) -> list[TraceRecord]:
        with self._lock:
            self._authorize_locked(task_id, requester_identity)
            return self.traces(task_id)

    def get(self, task_id: str) -> TaskRecord:
        with self._lock:
            return self._get_locked(task_id)

    def get_for_requester(self, task_id: str, requester_identity: str) -> TaskRecord:
        with self._lock:
            self._authorize_locked(task_id, requester_identity)
            return self._get_locked(task_id)

    def _authorize_locked(self, task_id: str, requester_identity: str) -> None:
        row = self._connection.execute(
            "SELECT requester_identity FROM analysis_tasks WHERE task_id=?",
            (task_id,),
        ).fetchone()
        if row is None or str(row["requester_identity"]) != requester_identity:
            raise AnalysisError("TASK_NOT_FOUND", "task not found", 404)

    def _get_locked(self, task_id: str) -> TaskRecord:
        row = self._connection.execute(
            "SELECT task_id,context_id,requester_identity,idempotency_key,request_digest,input_digest,plugin_id,binding_generation,"
            "state,reason_code,input_json,created_at_unix_ms,updated_at_unix_ms FROM analysis_tasks WHERE task_id=?",
            (task_id,),
        ).fetchone()
        if row is None:
            raise AnalysisError("TASK_NOT_FOUND", "task not found", 404)
        artifact_row = self._connection.execute(
            "SELECT artifact_digest,body FROM analysis_artifacts WHERE task_id=?",
            (task_id,),
        ).fetchone()
        try:
            input_bundle = FrozenInput.model_validate_json(bytes(row["input_json"]))
            artifact = AnalysisArtifact.model_validate_json(bytes(artifact_row["body"])) if artifact_row else None
        except ValidationError as exc:
            raise AnalysisError("PRIVATE_STATE_INVALID", "private task state failed contract validation", 503) from exc
        if row["input_digest"] != input_bundle.input_digest or compute_input_digest(input_bundle) != input_bundle.input_digest:
            raise AnalysisError("PRIVATE_STATE_INVALID", "private frozen input digest mismatch", 503)
        if artifact is not None and (
            artifact_row is None
            or str(artifact_row["artifact_digest"]) != artifact.artifact_digest
            or compute_artifact_digest(artifact) != artifact.artifact_digest
        ):
            raise AnalysisError("PRIVATE_STATE_INVALID", "private Artifact digest mismatch", 503)
        return TaskRecord(
            task_id=str(row["task_id"]),
            context_id=str(row["context_id"]),
            requester_identity=str(row["requester_identity"]),
            idempotency_key=str(row["idempotency_key"]),
            request_digest=str(row["request_digest"]),
            input_digest=str(row["input_digest"]),
            plugin_id=str(row["plugin_id"]),
            binding_generation=int(row["binding_generation"]),
            state=str(row["state"]),
            reason_code=str(row["reason_code"]),
            input_bundle=input_bundle,
            artifact=artifact,
            created_at_unix_ms=int(row["created_at_unix_ms"]),
            updated_at_unix_ms=int(row["updated_at_unix_ms"]),
        )

    def counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._connection.execute("SELECT state,count(*) AS count FROM analysis_tasks GROUP BY state").fetchall()
            return {str(row["state"]): int(row["count"]) for row in rows}
