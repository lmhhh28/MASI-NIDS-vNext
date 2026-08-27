from __future__ import annotations

import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path

from masi_analysis.errors import AnalysisError
from masi_analysis.store import TaskStore

from .support import frozen_input


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "analysis.sqlite3"
        self.store = TaskStore(self.path, max_tasks=16, retention_seconds=60, max_trace_events=8)

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def test_idempotent_submit_and_conflict(self) -> None:
        value = frozen_input()
        record, created = self.store.submit(value, "sha256:" + "c" * 64, "context-1", "masi-control")
        self.assertTrue(created)
        same, created_again = self.store.submit(value, "sha256:" + "c" * 64, "context-1", "masi-control")
        self.assertFalse(created_again)
        self.assertEqual(record.task_id, same.task_id)
        with self.assertRaises(AnalysisError) as caught:
            self.store.submit(value, "sha256:" + "d" * 64, "context-1", "masi-control")
        self.assertEqual(caught.exception.code, "IDEMPOTENCY_CONFLICT")

    def test_restart_marks_incomplete_task_failed(self) -> None:
        value = frozen_input(task_id="task-restart")
        self.store.submit(value, "sha256:" + "c" * 64, "context-restart", "masi-control")
        self.store.close()
        self.store = TaskStore(self.path, max_tasks=16, retention_seconds=60, max_trace_events=8)
        self.assertEqual(self.store.get(value.task_id).reason_code, "RECOVERED_INCOMPLETE")

    def test_requester_isolation_and_private_mode(self) -> None:
        value = frozen_input(task_id="task-private")
        self.store.submit(value, "sha256:" + "c" * 64, "context-private", "masi-control")
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(self.store.get_for_requester(value.task_id, "masi-control").task_id, value.task_id)
        with self.assertRaises(AnalysisError) as caught:
            self.store.get_for_requester(value.task_id, "other-authorized-client")
        self.assertEqual(caught.exception.code, "TASK_NOT_FOUND")

    def test_tampered_private_input_digest_fails_closed(self) -> None:
        value = frozen_input(task_id="task-tamper")
        self.store.submit(value, "sha256:" + "c" * 64, "context-tamper", "masi-control")
        self.store.close()
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "UPDATE analysis_tasks SET input_digest=? WHERE task_id=?",
                ("sha256:" + "d" * 64, value.task_id),
            )
        self.store = TaskStore(self.path, max_tasks=16, retention_seconds=60, max_trace_events=8)
        with self.assertRaises(AnalysisError) as caught:
            self.store.get(value.task_id)
        self.assertEqual(caught.exception.code, "PRIVATE_STATE_INVALID")


if __name__ == "__main__":
    unittest.main()
