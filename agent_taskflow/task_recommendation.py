"""Deprecated compatibility wrapper for :mod:`agent_taskflow.task_recommendations`.

Use ``agent_taskflow.task_recommendations`` for the canonical recommendation
APIs. This singular module is kept so older scripts and tests can continue to
import the historical path.
"""

from __future__ import annotations

from agent_taskflow.task_recommendations import *  # noqa: F401,F403
