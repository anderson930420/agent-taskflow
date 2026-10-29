import tempfile
import unittest
from pathlib import Path

from agent_taskflow.store import init_db
from agent_taskflow.ticket_feedback import add_ticket_feedback, list_ticket_feedback
from agent_taskflow.ticket_prompt import write_ticket_implementation_prompt
from agent_taskflow.models import utc_now_iso


class FeedbackTests(unittest.TestCase):
    def test_feedback_validation_order_and_prompt_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "db"
            init_db(db)
            now = utc_now_iso()
            import sqlite3
            with sqlite3.connect(db) as conn:
                conn.execute("INSERT INTO tasks(task_key, project, board, title, status, repo_path, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", ("AT-1", "p", "p", "t", "failed", str(root), now, now))
            for invalid in ("", "  ", "x" * 4001):
                with self.assertRaises(ValueError):
                    add_ticket_feedback(db, "AT-1", invalid, source="retry")
            for index in range(7):
                add_ticket_feedback(db, "AT-1", str(index) * 4000, source="retry")
            self.assertEqual(len(list_ticket_feedback(db, "AT-1")), 7)
            text = write_ticket_implementation_prompt("original", root / "attempt", db_path=db, task_key="AT-1").read_text()
            self.assertTrue(text.startswith("original\n\n## Feedback on earlier attempts"))
            self.assertLessEqual(len(text) - len("original"), 20000)
            self.assertNotIn("0" * 4000, text)
            self.assertIn("6" * 4000, text)
            self.assertLess(text.index('index="6"'), text.index('index="7"'))
            self.assertIn("plain text", text)
            add_ticket_feedback(db, "AT-1", "&" * 4000, source="retry")
            text = write_ticket_implementation_prompt("original", root / "attempt", db_path=db, task_key="AT-1").read_text()
            self.assertIn('index="8"', text)
            self.assertIn("[Note truncated]", text)
            self.assertLessEqual(len(text) - len("original"), 20000)
