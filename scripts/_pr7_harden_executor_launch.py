#!/usr/bin/env python3
"""One-shot PR-7 hardening patch; removes itself after applying."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "agent_taskflow/executor_launch.py"
text = TARGET.read_text(encoding="utf-8")

text = text.replace(
    '_LIVE_STATES = frozenset({"R", "S", "D", "T", "t", "W", "I", "P"})\n',
    '_DEAD_PROCESS_STATES = frozenset({"Z", "X", "x"})\n',
    1,
)
text = text.replace(
    "    def live(self) -> bool:\n        return self.state in _LIVE_STATES\n",
    "    def live(self) -> bool:\n"
    "        # Linux may add or expose less-common live states (for example K).\n"
    "        # Fail closed: only documented dead/zombie states count as exited.\n"
    "        return self.state not in _DEAD_PROCESS_STATES\n",
    1,
)

finalize_start = text.index("    def finalize(\n")
finalize_end = text.index("    def record_identity_mismatch(\n", finalize_start)
finalize_replacement = '''    def finalize(
        self,
        process_id: str,
        *,
        actor: str,
        exit_code: int | None,
        verified_exit: bool,
        termination_reason: str | None,
        metadata: Mapping[str, Any] | None = None,
    ) -> ExecutorProcessRecord:
        self.init_db()
        now = utc_now_iso()
        target = "exited" if verified_exit else "exit_unverified"
        reason = (
            "executor_process_exit_verified"
            if verified_exit
            else "executor_process_exit_unverified"
        )
        with closing(connect(self.db_path)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM executor_processes WHERE process_id = ?",
                (process_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Executor process not found: {process_id}")
            current = row["state"]
            event_metadata = {
                "exit_code": exit_code,
                "verified_exit": verified_exit,
                "termination_reason": termination_reason,
                **dict(metadata or {}),
            }
            if current == "exited":
                conn.execute(
                    """
                    UPDATE executor_processes
                    SET exit_code = COALESCE(?, exit_code),
                        termination_reason = COALESCE(?, termination_reason),
                        verified_exit = 1, updated_at = ?
                    WHERE process_id = ?
                    """,
                    (exit_code, termination_reason, now, process_id),
                )
            elif current == "exit_unverified" and verified_exit:
                cursor = conn.execute(
                    """
                    UPDATE executor_processes
                    SET state = 'exited', exited_at = COALESCE(exited_at, ?),
                        exit_code = COALESCE(?, exit_code),
                        termination_reason = COALESCE(?, termination_reason),
                        verified_exit = 1, updated_at = ?
                    WHERE process_id = ? AND state = 'exit_unverified'
                    """,
                    (now, exit_code, termination_reason, now, process_id),
                )
                if cursor.rowcount != 1:
                    raise ExecutorLaunchError(
                        f"Executor process state changed concurrently: {process_id}"
                    )
                self._insert_event(
                    conn,
                    process_id=process_id,
                    attempt_id=row["attempt_id"],
                    from_state="exit_unverified",
                    to_state="exited",
                    reason_code=reason,
                    actor=actor,
                    timestamp=now,
                    metadata=event_metadata,
                )
            elif current == "exit_unverified":
                conn.execute(
                    """
                    UPDATE executor_processes
                    SET exit_code = COALESCE(?, exit_code),
                        termination_reason = COALESCE(?, termination_reason),
                        updated_at = ?
                    WHERE process_id = ?
                    """,
                    (exit_code, termination_reason, now, process_id),
                )
            else:
                cursor = conn.execute(
                    """
                    UPDATE executor_processes
                    SET state = ?, exited_at = ?, exit_code = ?,
                        termination_reason = ?, verified_exit = ?, updated_at = ?
                    WHERE process_id = ? AND state = ?
                    """,
                    (
                        target,
                        now,
                        exit_code,
                        termination_reason,
                        int(verified_exit),
                        now,
                        process_id,
                        current,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ExecutorLaunchError(
                        f"Executor process state changed concurrently: {process_id}"
                    )
                self._insert_event(
                    conn,
                    process_id=process_id,
                    attempt_id=row["attempt_id"],
                    from_state=current,
                    to_state=target,
                    reason_code=reason,
                    actor=actor,
                    timestamp=now,
                    metadata=event_metadata,
                )
        record = self.get(process_id)
        assert record is not None
        return record

'''
text = text[:finalize_start] + finalize_replacement + text[finalize_end:]

terminate_start = text.index("def terminate_registered_process(\n")
terminate_end = text.index("def _write_preamble(\n", terminate_start)
terminate_replacement = '''def terminate_registered_process(
    store: ExecutorProcessStore,
    record: ExecutorProcessRecord,
    *,
    actor: str,
    termination_reason: str,
    terminate_grace_seconds: float = 2.0,
    kill_wait_seconds: float = 3.0,
) -> ExecutorProcessRecord:
    """Signal one proven process group, escalate, and persist verified exit.

    The operation is intentionally idempotent. The in-process launcher and an
    external operator may observe the same kill request; each transition reloads
    the current state and tolerates the other actor winning the compare-and-set.
    """
    current = store.get(record.process_id)
    if current is None:
        raise KeyError(f"Executor process not found: {record.process_id}")
    if current.state == "exited":
        return current
    if current.state == "exit_unverified":
        if current.pgid is None or current.session_id is None:
            return current
        verified = inspect_process_group(
            current.pgid, current.session_id
        ).verified_exited
        return store.finalize(
            current.process_id,
            actor=actor,
            exit_code=current.exit_code,
            verified_exit=verified,
            termination_reason=termination_reason,
            metadata={"reconciled_after_unverified_exit": True},
        )
    if current.state not in ACTIVE_PROCESS_STATES:
        return current

    snapshot = _verify_record_identity(store, current, actor=actor)
    if snapshot.verified_exited:
        return store.finalize(
            current.process_id,
            actor=actor,
            exit_code=current.exit_code,
            verified_exit=True,
            termination_reason=termination_reason,
            metadata={"already_exited": True},
        )
    assert current.pgid is not None and current.session_id is not None
    pgid = current.pgid
    session_id = current.session_id

    if current.state == "allocated":
        raise ProcessIdentityError(
            "executor process is allocated but has no proven running identity"
        )
    if current.state == "running":
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            current = store.mark_signal(
                current.process_id,
                signal_name="SIGTERM",
                actor=actor,
                termination_reason=termination_reason,
                metadata={
                    "live_member_pids": [item.pid for item in snapshot.live_members]
                },
            )
        except ExecutorLaunchError:
            current = store.get(current.process_id) or current

    if _wait_group_exit(pgid, session_id, terminate_grace_seconds):
        return store.finalize(
            current.process_id,
            actor=actor,
            exit_code=current.exit_code,
            verified_exit=True,
            termination_reason=termination_reason,
            metadata={"escalated_to_sigkill": False},
        )

    current = store.get(current.process_id) or current
    if current.state == "exited":
        return current
    if current.state == "exit_unverified":
        return store.finalize(
            current.process_id,
            actor=actor,
            exit_code=current.exit_code,
            verified_exit=inspect_process_group(pgid, session_id).verified_exited,
            termination_reason=termination_reason,
            metadata={"reconciled_during_escalation": True},
        )
    if current.state == "term_sent":
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            current = store.mark_signal(
                current.process_id,
                signal_name="SIGKILL",
                actor=actor,
                termination_reason=termination_reason,
                metadata={
                    "live_member_pids": [
                        item.pid
                        for item in inspect_process_group(pgid, session_id).live_members
                    ]
                },
            )
        except ExecutorLaunchError:
            current = store.get(current.process_id) or current

    verified = _wait_group_exit(pgid, session_id, kill_wait_seconds)
    current = store.get(current.process_id) or current
    return store.finalize(
        current.process_id,
        actor=actor,
        exit_code=current.exit_code,
        verified_exit=verified,
        termination_reason=termination_reason,
        metadata={"escalated_to_sigkill": True},
    )


'''
text = text[:terminate_start] + terminate_replacement + text[terminate_end:]

manifest_anchor = "    atomic_write_json(launch_spec_path, spec_payload, sort_keys=True)\n\n"
manifest_insert = '''    atomic_write_json(launch_spec_path, spec_payload, sort_keys=True)
    manifest_base = {
        "schema_version": "executor_process_pid.v1",
        "process_id": process_id,
        "attempt_id": binding.attempt_id,
        "task_key": binding.task_key,
        "lease_id": binding.lease_id,
        "owner_id": binding.owner_id,
        "executor_name": spec.executor_name,
        "pid": None,
        "pgid": None,
        "session_id": None,
        "leader_start_ticks": None,
        "state": "pending_preflight",
        "created_at": utc_now_iso(),
    }
    atomic_write_json(pid_manifest_path, manifest_base, sort_keys=True)

'''
if manifest_anchor not in text:
    raise RuntimeError("launch manifest anchor missing")
text = text.replace(manifest_anchor, manifest_insert, 1)

preflight_anchor = '''        store.create(
            process_id=process_id,
            binding=binding,
            executor_name=spec.executor_name,
            state="preflight_failed",
            launch_spec_path=launch_spec_path,
            pid_manifest_path=pid_manifest_path,
            reason_code="executor_launch_preflight_failed",
            metadata={
                "blocking_errors": list(preflight.blocking_errors),
                "warnings": list(preflight.warnings),
            },
        )
'''
preflight_replacement = preflight_anchor + '''        atomic_write_json(
            pid_manifest_path,
            {
                **manifest_base,
                "state": "preflight_failed",
                "blocking_errors": list(preflight.blocking_errors),
            },
            sort_keys=True,
        )
'''
if preflight_anchor not in text:
    raise RuntimeError("preflight manifest anchor missing")
text = text.replace(preflight_anchor, preflight_replacement, 1)

start_failure_anchor = '''            store.mark_start_failed(
                process_id,
                actor=binding.owner_id,
                error=f"{exc.__class__.__name__}: {exc}",
            )
'''
start_failure_replacement = start_failure_anchor + '''            atomic_write_json(
                pid_manifest_path,
                {
                    **manifest_base,
                    "state": "start_failed",
                    "error": f"{exc.__class__.__name__}: {exc}",
                },
                sort_keys=True,
            )
'''
if start_failure_anchor not in text:
    raise RuntimeError("start-failure manifest anchor missing")
text = text.replace(start_failure_anchor, start_failure_replacement, 1)

TARGET.write_text(text, encoding="utf-8")
(ROOT / ".github/workflows/ci.yml").write_text(
    '''name: CI\n\non:\n  pull_request:\n  push:\n    branches:\n      - main\n\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n\n      - uses: actions/setup-python@v5\n        with:\n          python-version: "3.12"\n\n      - name: Install package and dependencies\n        run: python -m pip install -e .\n\n      - name: Run unit tests\n        run: PYTHONPATH=. python -m unittest discover -s tests\n\n      - name: Compile sources\n        run: PYTHONPATH=. python -m compileall agent_taskflow scripts tests\n''',
    encoding="utf-8",
)
Path(__file__).unlink()
