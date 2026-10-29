"""Read-only usage accounting from Attempt evidence."""
from __future__ import annotations

import json
import math
from contextlib import closing
from pathlib import Path

from agent_taskflow.store import connect
from agent_taskflow.tasks import normalize_task_key

USAGE_FIELDS = (
    "input_tokens", "output_tokens", "cache_creation_input_tokens",
    "cache_read_input_tokens", "total_cost_usd", "num_turns", "duration_ms",
)


def summarize_ticket_usage(db_path: str | Path | None, task_key: str) -> dict:
    key = normalize_task_key(task_key)
    with closing(connect(db_path)) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "attempts" not in tables:
            rows = []
        else:
            root = "COALESCE(r.artifact_root, a.artifact_root)" if "attempt_resources" in tables else "a.artifact_root"
            join = "LEFT JOIN attempt_resources r ON r.attempt_id = a.attempt_id" if "attempt_resources" in tables else ""
            rows = conn.execute(
                f"SELECT a.attempt_id, a.attempt_number, {root} AS artifact_root "
                f"FROM attempts a JOIN tasks t ON t.task_id = a.task_id {join} "
                "WHERE t.task_key = ? ORDER BY a.attempt_number", (key,)
            ).fetchall()
    attempts = []
    known = []
    for row in rows:
        data = None
        try:
            if row['artifact_root']:
                candidate = json.loads((Path(row['artifact_root']) / 'claude-code-usage.json').read_text(encoding='utf-8'))
                if (isinstance(candidate, dict)
                    and candidate.get('schema_version') == 'claude_code_usage.v1'
                    and candidate.get('attempt_id') == row['attempt_id']
                    and candidate.get('task_key') == key
                    and all(isinstance(candidate.get(field), (int, float))
                            and not isinstance(candidate[field], bool)
                            and math.isfinite(candidate[field]) and candidate[field] >= 0
                            for field in USAGE_FIELDS)):
                    data = candidate
        except (OSError, UnicodeError, ValueError, TypeError, OverflowError):
            pass
        attempts.append({'attempt_id': row['attempt_id'], 'attempt_number': row['attempt_number'], 'usage': data})
        if data is not None:
            known.append(data)
    return {
        'attempts_total': len(rows), 'attempts_with_usage': len(known),
        'attempts_unknown': len(rows) - len(known),
        **{field: sum(item[field] for item in known) if known else None for field in USAGE_FIELDS},
        'per_attempt': attempts,
    }
