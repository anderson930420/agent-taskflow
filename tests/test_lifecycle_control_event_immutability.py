from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from agent_taskflow.lifecycle_control import RuntimeControlStore
from agent_taskflow.store import connect


class RuntimeControlEventImmutabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "state.db"
        RuntimeControlStore(self.db_path).pause(actor="test-operator")

    def test_control_events_cannot_be_updated_or_deleted(self) -> None:
        with closing(connect(self.db_path)) as conn:
            event_id = conn.execute(
                "SELECT event_id FROM runtime_control_events ORDER BY event_id LIMIT 1"
            ).fetchone()[0]

            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "runtime control events are append-only",
            ):
                with conn:
                    conn.execute(
                        "UPDATE runtime_control_events SET actor = 'rewritten' WHERE event_id = ?",
                        (event_id,),
                    )

            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "runtime control events are append-only",
            ):
                with conn:
                    conn.execute(
                        "DELETE FROM runtime_control_events WHERE event_id = ?",
                        (event_id,),
                    )


if __name__ == "__main__":
    unittest.main()
