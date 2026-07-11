#!/usr/bin/env python3
"""One-shot PR-9 integration patch; removed by the workflow that executes it."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replace_once(path: str, old: str, new: str) -> None:
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    if old not in text:
        raise RuntimeError(f"expected pattern missing in {path}: {old[:120]!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


# Shared process schema: existing PR-7 rows remain executor-role rows.
replace_once(
    "agent_taskflow/executor_process_schema.py",
    '"""Additive PR-7 schema for managed executor process groups."""',
    '"""Additive schema for managed executor and validator process groups."""',
)
replace_once(
    "agent_taskflow/executor_process_schema.py",
    """                executor_name TEXT NOT NULL,\n                pid INTEGER,""",
    """                executor_name TEXT NOT NULL,\n                process_role TEXT NOT NULL DEFAULT 'executor'\n                    CHECK(process_role IN ('executor', 'validator')),\n                pid INTEGER,""",
)
replace_once(
    "agent_taskflow/executor_process_schema.py",
    '''        conn.execute(\n            """\n            CREATE UNIQUE INDEX IF NOT EXISTS ux_executor_process_one_active_per_attempt\n''',
    '''        columns = {\n            row["name"]\n            for row in conn.execute("PRAGMA table_info(executor_processes)")\n        }\n        if "process_role" not in columns:\n            conn.execute(\n                """\n                ALTER TABLE executor_processes\n                ADD COLUMN process_role TEXT NOT NULL DEFAULT 'executor'\n                CHECK(process_role IN ('executor', 'validator'))\n                """\n            )\n        conn.execute(\n            """\n            CREATE UNIQUE INDEX IF NOT EXISTS ux_executor_process_one_active_per_attempt\n''',
)

# Validator contexts receive the same immutable Attempt launch binding.
replace_once(
    "agent_taskflow/validators/base.py",
    "from agent_taskflow.models import require_absolute_path\n",
    "from agent_taskflow.executor_launch import ExecutorLaunchBinding\nfrom agent_taskflow.models import require_absolute_path\n",
)
replace_once(
    "agent_taskflow/validators/base.py",
    """    timeout_seconds: int | None = None\n    env: dict[str, str] | None = None\n""",
    """    timeout_seconds: int | None = None\n    env: dict[str, str] | None = None\n    launch_binding: ExecutorLaunchBinding | None = None\n""",
)
replace_once(
    "agent_taskflow/validators/base.py",
    '''        object.__setattr__(self, "env", _validate_env(self.env))\n\n\n@dataclass(frozen=True)\nclass ValidatorResult:\n''',
    '''        object.__setattr__(self, "env", _validate_env(self.env))\n        if self.launch_binding is not None:\n            binding = self.launch_binding\n            if binding.task_key != self.task_key:\n                raise ValueError("launch_binding task_key does not match ValidatorContext")\n            if binding.worktree_path.resolve() != self.worktree_path.resolve():\n                raise ValueError(\n                    "launch_binding worktree_path does not match ValidatorContext"\n                )\n            if binding.artifact_root.resolve() != self.artifact_dir.resolve():\n                raise ValueError(\n                    "launch_binding artifact_root does not match ValidatorContext"\n                )\n\n\n@dataclass(frozen=True)\nclass ValidatorResult:\n''',
)

# Make PR-7's launcher role-aware while preserving executor compatibility.
replace_once(
    "agent_taskflow/executor_launch.py",
    '''PROCESS_REASON_CODES = frozenset(\n    {\n        "executor_launch_allocated",\n        "executor_launch_preflight_failed",\n        "executor_process_start_failed",\n        "executor_process_started",\n        "executor_process_exited",\n        "executor_timeout",\n        "operator_kill_requested",\n        "executor_descendant_cleanup",\n        "executor_process_sigterm_sent",\n        "executor_process_sigkill_sent",\n        "executor_process_exit_verified",\n        "executor_process_exit_unverified",\n        "executor_process_identity_mismatch",\n    }\n)\n''',
    '''_ROLE_REASON_SUFFIXES = frozenset(\n    {\n        "launch_allocated",\n        "launch_preflight_failed",\n        "process_start_failed",\n        "process_started",\n        "process_exited",\n        "timeout",\n        "descendant_cleanup",\n        "process_sigterm_sent",\n        "process_sigkill_sent",\n        "process_exit_verified",\n        "process_exit_unverified",\n        "process_identity_mismatch",\n    }\n)\nPROCESS_REASON_CODES = frozenset(\n    {f"{role}_{suffix}" for role in ("executor", "validator") for suffix in _ROLE_REASON_SUFFIXES}\n    | {"operator_kill_requested"}\n)\n\n\ndef _validate_process_role(process_role: str) -> str:\n    normalized = str(process_role).strip().lower()\n    if normalized not in {"executor", "validator"}:\n        raise ValueError(f"Invalid process_role: {process_role!r}")\n    return normalized\n\n\ndef _role_reason(process_role: str, suffix: str) -> str:\n    role = _validate_process_role(process_role)\n    reason = f"{role}_{suffix}"\n    if reason not in PROCESS_REASON_CODES:\n        raise ValueError(f"Unknown runtime process reason_code: {reason!r}")\n    return reason\n\n\ndef _role_label(process_role: str) -> str:\n    return "validator" if _validate_process_role(process_role) == "validator" else "executor"\n''',
)
replace_once(
    "agent_taskflow/executor_launch.py",
    """    combined_output: bool\n    environment_mode: str = "inherit_with_overrides"\n""",
    """    combined_output: bool\n    process_role: str = "executor"\n    environment_mode: str = "inherit_with_overrides"\n""",
)
replace_once(
    "agent_taskflow/executor_launch.py",
    '''        object.__setattr__(self, "argv", argv)\n        object.__setattr__(self, "cwd", require_absolute_path(self.cwd, "cwd"))\n''',
    '''        object.__setattr__(self, "argv", argv)\n        object.__setattr__(self, "process_role", _validate_process_role(self.process_role))\n        object.__setattr__(self, "cwd", require_absolute_path(self.cwd, "cwd"))\n''',
)
replace_once(
    "agent_taskflow/executor_launch.py",
    '''        return {\n            "schema_version": "executor_launch_spec.v1",\n''',
    '''        return {\n            "schema_version": (\n                "validator_launch_spec.v1"\n                if self.process_role == "validator"\n                else "executor_launch_spec.v1"\n            ),\n            "process_role": self.process_role,\n''',
)
replace_once(
    "agent_taskflow/executor_launch.py",
    """    executor_name: str\n    pid: int | None\n""",
    """    executor_name: str\n    process_role: str\n    pid: int | None\n""",
)
replace_once(
    "agent_taskflow/executor_launch.py",
    '''        executor_name=row["executor_name"],\n        pid=row["pid"],\n''',
    '''        executor_name=row["executor_name"],\n        process_role=(row["process_role"] if "process_role" in row.keys() else "executor"),\n        pid=row["pid"],\n''',
)
replace_once(
    "agent_taskflow/executor_launch.py",
    """        executor_name: str,\n        state: str,\n""",
    """        executor_name: str,\n        process_role: str,\n        state: str,\n""",
)
replace_once(
    "agent_taskflow/executor_launch.py",
    '''                    executor_name, state, launch_spec_path, pid_manifest_path,\n                    created_at, updated_at\n                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n''',
    '''                    executor_name, process_role, state, launch_spec_path, pid_manifest_path,\n                    created_at, updated_at\n                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n''',
)
replace_once(
    "agent_taskflow/executor_launch.py",
    '''                    executor_name,\n                    state,\n                    str(launch_spec_path),\n''',
    '''                    executor_name,\n                    _validate_process_role(process_role),\n                    state,\n                    str(launch_spec_path),\n''',
)
# Role-aware transition reason codes.
replace_once(
    "agent_taskflow/executor_launch.py",
    '''        now = utc_now_iso()\n        return self._transition(\n            process_id,\n            to_state="running",\n            reason_code="executor_process_started",\n''',
    '''        now = utc_now_iso()\n        record = self.get(process_id)\n        if record is None:\n            raise KeyError(f"Runtime process not found: {process_id}")\n        return self._transition(\n            process_id,\n            to_state="running",\n            reason_code=_role_reason(record.process_role, "process_started"),\n''',
)
replace_once(
    "agent_taskflow/executor_launch.py",
    '''        return self._transition(\n            process_id,\n            to_state="start_failed",\n            reason_code="executor_process_start_failed",\n''',
    '''        record = self.get(process_id)\n        if record is None:\n            raise KeyError(f"Runtime process not found: {process_id}")\n        return self._transition(\n            process_id,\n            to_state="start_failed",\n            reason_code=_role_reason(record.process_role, "process_start_failed"),\n''',
)
replace_once(
    "agent_taskflow/executor_launch.py",
    '''            reason_code=(\n                "executor_process_sigterm_sent"\n                if signal_name == "SIGTERM"\n                else "executor_process_sigkill_sent"\n            ),\n''',
    '''            reason_code=_role_reason(\n                (self.get(process_id) or (_ for _ in ()).throw(\n                    KeyError(f"Runtime process not found: {process_id}")\n                )).process_role,\n                "process_sigterm_sent" if signal_name == "SIGTERM" else "process_sigkill_sent",\n            ),\n''',
)
replace_once(
    "agent_taskflow/executor_launch.py",
    '''        reason = (\n            "executor_process_exit_verified"\n            if verified_exit\n            else "executor_process_exit_unverified"\n        )\n''',
    '''        existing = self.get(process_id)\n        if existing is None:\n            raise KeyError(f"Runtime process not found: {process_id}")\n        reason = _role_reason(\n            existing.process_role,\n            "process_exit_verified" if verified_exit else "process_exit_unverified",\n        )\n''',
)
replace_once(
    "agent_taskflow/executor_launch.py",
    '''                reason_code="executor_process_identity_mismatch",\n''',
    '''                reason_code=_role_reason(\n                    (self.get(process_id) or (_ for _ in ()).throw(\n                        KeyError(f"Runtime process not found: {process_id}")\n                    )).process_role,\n                    "process_identity_mismatch",\n                ),\n''',
)
replace_once(
    "agent_taskflow/executor_launch.py",
    'raise ValueError(f"Unknown executor process reason_code: {reason_code!r}")',
    'raise ValueError(f"Unknown runtime process reason_code: {reason_code!r}")',
)
# Preflight and artifacts use role-aware language/names.
replace_once(
    "agent_taskflow/executor_launch.py",
    '''    errors: list[str] = []\n    warnings: list[str] = []\n''',
    '''    errors: list[str] = []\n    warnings: list[str] = []\n    role_label = _role_label(spec.process_role)\n''',
)
replace_once(
    "agent_taskflow/executor_launch.py",
    'errors.append("managed executor process groups require POSIX os.killpg support")',
    'errors.append(f"managed {role_label} process groups require POSIX os.killpg support")',
)
replace_once(
    "agent_taskflow/executor_launch.py",
    'errors.append("managed executor identity verification requires Linux /proc")',
    'errors.append(f"managed {role_label} identity verification requires Linux /proc")',
)
replace_once(
    "agent_taskflow/executor_launch.py",
    'errors.append(f"executor binary is not an executable file: {candidate}")',
    'errors.append(f"{role_label} binary is not an executable file: {candidate}")',
)
replace_once(
    "agent_taskflow/executor_launch.py",
    'errors.append(f"executor binary was not found on PATH: {executable}")',
    'errors.append(f"{role_label} binary was not found on PATH: {executable}")',
)
replace_once(
    "agent_taskflow/executor_launch.py",
    '''                f"Attempt already has an active executor process: {active['process_id']}"\n''',
    '''                f"Attempt already has an active managed process: {active['process_id']}"\n''',
)
replace_once(
    "agent_taskflow/executor_launch.py",
    '''        warnings.append(\n            "environment inheritance is retained for executor credentials; values are not persisted"\n        )\n    warnings.append("PR-7 does not provide network or container isolation")\n''',
    '''        warnings.append(\n            f"environment inheritance is retained for {role_label} credentials; values are not persisted"\n        )\n    warnings.append("managed process launch does not provide network or container isolation")\n''',
)
replace_once(
    "agent_taskflow/executor_launch.py",
    '''    safe_executor = _safe_name(spec.executor_name)\n    launch_spec_path = binding.artifact_root / f"executor-launch-spec-{safe_executor}.json"\n    pid_manifest_path = binding.artifact_root / f"executor-process-{safe_executor}.pid.json"\n''',
    '''    safe_executor = _safe_name(spec.executor_name)\n    role_label = _role_label(spec.process_role)\n    launch_spec_path = binding.artifact_root / f"{role_label}-launch-spec-{safe_executor}.json"\n    pid_manifest_path = binding.artifact_root / f"{role_label}-process-{safe_executor}.pid.json"\n''',
)
replace_once(
    "agent_taskflow/executor_launch.py",
    '''        "schema_version": "executor_process_pid.v1",\n        "process_id": process_id,\n''',
    '''        "schema_version": f"{role_label}_process_pid.v1",\n        "process_role": spec.process_role,\n        "process_id": process_id,\n''',
)
replace_once(
    "agent_taskflow/executor_launch.py",
    '''            executor_name=spec.executor_name,\n            state="preflight_failed",\n''',
    '''            executor_name=spec.executor_name,\n            process_role=spec.process_role,\n            state="preflight_failed",\n''',
)
replace_once(
    "agent_taskflow/executor_launch.py",
    '''            reason_code="executor_launch_preflight_failed",\n''',
    '''            reason_code=_role_reason(spec.process_role, "launch_preflight_failed"),\n''',
)
replace_once(
    "agent_taskflow/executor_launch.py",
    '''            termination_reason="executor_launch_preflight_failed",\n''',
    '''            termination_reason=_role_reason(spec.process_role, "launch_preflight_failed"),\n''',
)
replace_once(
    "agent_taskflow/executor_launch.py",
    '''        executor_name=spec.executor_name,\n        state="allocated",\n''',
    '''        executor_name=spec.executor_name,\n        process_role=spec.process_role,\n        state="allocated",\n''',
)
replace_once(
    "agent_taskflow/executor_launch.py",
    '''        reason_code="executor_launch_allocated",\n''',
    '''        reason_code=_role_reason(spec.process_role, "launch_allocated"),\n''',
)
replace_once(
    "agent_taskflow/executor_launch.py",
    '''                termination_reason="executor_process_start_failed",\n''',
    '''                termination_reason=_role_reason(spec.process_role, "process_start_failed"),\n''',
)
# There are two start-failed result branches.
replace_once(
    "agent_taskflow/executor_launch.py",
    '''                termination_reason="executor_process_start_failed",\n''',
    '''                termination_reason=_role_reason(spec.process_role, "process_start_failed"),\n''',
)
replace_once(
    "agent_taskflow/executor_launch.py",
    '''                "schema_version": "executor_process_pid.v1",\n                "process_id": process_id,\n''',
    '''                "schema_version": f"{role_label}_process_pid.v1",\n                "process_role": spec.process_role,\n                "process_id": process_id,\n''',
)
replace_once(
    "agent_taskflow/executor_launch.py",
    '''                termination_reason = "executor_timeout"\n''',
    '''                termination_reason = _role_reason(spec.process_role, "timeout")\n''',
)
replace_once(
    "agent_taskflow/executor_launch.py",
    '''            termination_reason = termination_reason or "executor_descendant_cleanup"\n''',
    '''            termination_reason = termination_reason or _role_reason(\n                spec.process_role, "descendant_cleanup"\n            )\n''',
)
replace_once(
    "agent_taskflow/executor_launch.py",
    '''                termination_reason = termination_reason or "executor_descendant_cleanup"\n''',
    '''                termination_reason = termination_reason or _role_reason(\n                    spec.process_role, "descendant_cleanup"\n                )\n''',
)

# Shared command helper: canonical bound validators use the managed launcher.
(ROOT / "agent_taskflow/validators/command.py").write_text(
    '''"""Shared command-execution helpers for shell-based validators."""\n\nfrom __future__ import annotations\n\nimport subprocess\nfrom pathlib import Path\nfrom typing import Sequence\n\nfrom agent_taskflow.executor_launch import (\n    ExecutorLaunchBinding,\n    ExecutorLaunchSpec,\n    run_managed_process,\n)\n\n_DANGEROUS_FRAGMENTS = frozenset({\n    "rm", "sudo", "git push", "git merge", "gh pr merge", "cleanup",\n    "npm install", "pip install", "curl", "wget",\n})\n_AUTO_FIX_FRAGMENTS = frozenset({"--fix", "--write", "--apply"})\n\n\ndef _validate_command(command: Sequence[str]) -> list[str]:\n    if not command:\n        raise ValueError("command must not be empty")\n    if not isinstance(command, (list, tuple)):\n        raise TypeError(\n            f"command must be a list or tuple of strings, not {type(command).__name__!r}"\n        )\n    normalized: list[str] = []\n    for i, part in enumerate(command):\n        if not isinstance(part, str):\n            raise TypeError(\n                f"command[{i}] must be a string, not {type(part).__name__!r}"\n            )\n        if not part:\n            raise ValueError(f"command[{i}] must not be empty")\n        normalized.append(part)\n    return normalized\n\n\ndef _check_dangerous(command: Sequence[str]) -> str | None:\n    joined = " ".join(command).lower()\n    for fragment in _DANGEROUS_FRAGMENTS:\n        if fragment in joined:\n            return f"command contains dangerous fragment: {fragment!r}"\n    return None\n\n\ndef run_command(\n    validator_name: str,\n    command: list[str],\n    worktree_path: Path,\n    artifact_dir: Path,\n    timeout_seconds: int | None,\n    run_env: dict[str, str] | None,\n    launch_binding: ExecutorLaunchBinding | None = None,\n) -> tuple[subprocess.CompletedProcess[str] | None, Path, str, str, dict[str, Path]]:\n    """Run a validator command through legacy or Attempt-managed process launch."""\n    artifact_dir.mkdir(parents=True, exist_ok=True)\n    log_path = artifact_dir / f"{validator_name}.log"\n    managed_artifacts: dict[str, Path] = {}\n\n    if launch_binding is not None:\n        preamble = (\n            f"Validator: {validator_name}\\n"\n            f"Command: {command!r}\\n"\n            f"Worktree: {worktree_path}\\n"\n            "Environment: not logged\\n"\n        )\n        spec = ExecutorLaunchSpec(\n            executor_name=validator_name,\n            process_role="validator",\n            argv=tuple(command),\n            cwd=worktree_path,\n            artifact_dir=artifact_dir,\n            timeout_seconds=timeout_seconds,\n            stdin_mode="devnull",\n            combined_output=True,\n            environment_keys=tuple((run_env or {}).keys()),\n        )\n        managed = run_managed_process(\n            launch_binding,\n            spec,\n            stdout_path=log_path,\n            run_env=run_env,\n            preamble=preamble,\n        )\n        managed_artifacts = {\n            "launch_spec": managed.launch_spec_path,\n            "pid_manifest": managed.pid_manifest_path,\n        }\n        if managed.start_error:\n            return (\n                None,\n                log_path,\n                f"{validator_name} validation command failed to start: {managed.start_error}",\n                "blocked",\n                managed_artifacts,\n            )\n        if managed.timed_out:\n            return (\n                None,\n                log_path,\n                f"{validator_name} validation timed out after {timeout_seconds} seconds.",\n                "failed",\n                managed_artifacts,\n            )\n        if managed.kill_requested:\n            return (\n                None,\n                log_path,\n                f"{validator_name} validation aborted by operator kill request.",\n                "blocked",\n                managed_artifacts,\n            )\n        if not managed.verified_exit:\n            return (\n                None,\n                log_path,\n                f"{validator_name} validator process-group exit could not be verified; verified_exit=false.",\n                "blocked",\n                managed_artifacts,\n            )\n        if managed.termination_reason == "validator_descendant_cleanup":\n            return (\n                None,\n                log_path,\n                f"{validator_name} validator leader exited with live descendants; "\n                "the process group was terminated and verified.",\n                "blocked",\n                managed_artifacts,\n            )\n        completed = subprocess.CompletedProcess(command, managed.exit_code or 0)\n        return completed, log_path, "", "", managed_artifacts\n\n    with log_path.open("w", encoding="utf-8") as log_file:\n        log_file.write(f"Validator: {validator_name}\\n")\n        log_file.write(f"Command: {command!r}\\n")\n        log_file.write(f"Worktree: {worktree_path}\\n")\n        log_file.write("Environment: not logged\\n\\n")\n        log_file.flush()\n        try:\n            completed = subprocess.run(\n                command,\n                cwd=worktree_path,\n                stdout=log_file,\n                stderr=subprocess.STDOUT,\n                timeout=timeout_seconds,\n                env=run_env,\n                text=True,\n                shell=False,\n                check=False,\n            )\n            return completed, log_path, "", "", managed_artifacts\n        except subprocess.TimeoutExpired:\n            summary = (\n                f"{validator_name} validation timed out after "\n                f"{timeout_seconds} seconds."\n            )\n            log_file.write(f"\\n{summary}\\n")\n            return None, log_path, summary, "failed", managed_artifacts\n        except FileNotFoundError as exc:\n            summary = f"{validator_name} validation command failed to start: {exc}"\n            log_file.write(f"\\n{summary}\\n")\n            return None, log_path, summary, "blocked", managed_artifacts\n\n\n__all__ = [\n    "_AUTO_FIX_FRAGMENTS",\n    "_DANGEROUS_FRAGMENTS",\n    "_check_dangerous",\n    "_validate_command",\n    "run_command",\n]\n''',
    encoding="utf-8",
)

# Command-backed validators consume managed artifacts.
for path in ("agent_taskflow/validators/lint.py", "agent_taskflow/validators/typecheck.py"):
    replace_once(
        path,
        '''        completed, log_path, error_summary, error_status = run_command(\n''',
        '''        completed, log_path, error_summary, error_status, process_artifacts = run_command(\n''',
    )
    replace_once(
        path,
        '''            run_env=run_env,\n        )\n''',
        '''            run_env=run_env,\n            launch_binding=context.launch_binding,\n        )\n''',
    )
    replace_once(
        path,
        '''                artifacts={"log": log_path},\n''',
        '''                artifacts={"log": log_path, **process_artifacts},\n''',
    )
    replace_once(
        path,
        '''            artifacts={"log": log_path},\n''',
        '''            artifacts={"log": log_path, **process_artifacts},\n''',
    )

# Pytest and OpenSpec use the common managed command path.
(ROOT / "agent_taskflow/validators/pytest.py").write_text(
    '''"""Pytest validator for Agent Taskflow."""\n\nfrom __future__ import annotations\n\nimport os\nimport sys\nfrom pathlib import Path\nfrom typing import Sequence\n\nfrom agent_taskflow.validators.base import Validator, ValidatorContext, ValidatorResult\nfrom agent_taskflow.validators.command import run_command\n\n\ndef _validate_args(args: Sequence[str] | None, field_name: str) -> list[str]:\n    if args is None:\n        return []\n    if isinstance(args, str):\n        raise TypeError(f"{field_name} must be a sequence of strings, not a raw string")\n    normalized = list(args)\n    for part in normalized:\n        if not isinstance(part, str):\n            raise TypeError(f"{field_name} entries must be strings")\n        if not part:\n            raise ValueError(f"{field_name} entries must not be empty")\n    return normalized\n\n\nclass PytestValidator(Validator):\n    name = "pytest"\n\n    def __init__(self, python_bin: str | None = None, extra_args: Sequence[str] | None = None) -> None:\n        resolved = sys.executable if python_bin is None else python_bin.strip()\n        if not resolved:\n            raise ValueError("python_bin must not be empty")\n        self.python_bin = resolved\n        self.extra_args = _validate_args(extra_args, "extra_args")\n\n    @property\n    def command(self) -> list[str]:\n        return [self.python_bin, "-m", "pytest", *self.extra_args]\n\n    def _log_path(self, artifact_dir: Path) -> Path:\n        return artifact_dir / "pytest.log"\n\n    def run(self, context: ValidatorContext) -> ValidatorResult:\n        run_env = None\n        if context.env is not None:\n            run_env = os.environ.copy()\n            run_env.update(context.env)\n        completed, log_path, error_summary, error_status, process_artifacts = run_command(\n            validator_name=self.name,\n            command=self.command,\n            worktree_path=context.worktree_path,\n            artifact_dir=context.artifact_dir,\n            timeout_seconds=context.timeout_seconds,\n            run_env=run_env,\n            launch_binding=context.launch_binding,\n        )\n        if completed is None:\n            return ValidatorResult(\n                validator=self.name, status=error_status, exit_code=None,\n                log_path=log_path, summary=error_summary,\n                artifacts={"log": log_path, **process_artifacts},\n            )\n        status = "passed" if completed.returncode == 0 else "failed"\n        summary = (\n            "Pytest validation passed."\n            if status == "passed"\n            else f"Pytest validation failed with exit code {completed.returncode}."\n        )\n        return ValidatorResult(\n            validator=self.name, status=status, exit_code=completed.returncode,\n            log_path=log_path, summary=summary,\n            artifacts={"log": log_path, **process_artifacts},\n        )\n\n\n__all__ = ["PytestValidator"]\n''',
    encoding="utf-8",
)
(ROOT / "agent_taskflow/validators/openspec.py").write_text(
    '''"""OpenSpec validator for Agent Taskflow."""\n\nfrom __future__ import annotations\n\nimport os\nfrom typing import Sequence\n\nfrom agent_taskflow.validators.base import Validator, ValidatorContext, ValidatorResult\nfrom agent_taskflow.validators.command import run_command\n\n\ndef _validate_args(args: Sequence[str] | None, field_name: str) -> list[str]:\n    if args is None:\n        return []\n    if isinstance(args, str):\n        raise TypeError(f"{field_name} must be a sequence of strings, not a raw string")\n    normalized = list(args)\n    for part in normalized:\n        if not isinstance(part, str):\n            raise TypeError(f"{field_name} entries must be strings")\n        if not part:\n            raise ValueError(f"{field_name} entries must not be empty")\n    return normalized\n\n\nclass OpenSpecValidator(Validator):\n    name = "openspec"\n\n    def __init__(self, openspec_bin: str = "openspec", args: Sequence[str] | None = None) -> None:\n        self.openspec_bin = openspec_bin.strip()\n        if not self.openspec_bin:\n            raise ValueError("openspec_bin must not be empty")\n        self.args = _validate_args(\n            args if args is not None else ["validate", "--all", "--no-interactive"],\n            "args",\n        )\n\n    @property\n    def command(self) -> list[str]:\n        return [self.openspec_bin, *self.args]\n\n    def run(self, context: ValidatorContext) -> ValidatorResult:\n        if not (context.worktree_path / "openspec").exists():\n            return ValidatorResult(\n                validator=self.name, status="skipped", exit_code=None, log_path=None,\n                summary="openspec directory not found", artifacts={},\n            )\n        run_env = None\n        if context.env is not None:\n            run_env = os.environ.copy()\n            run_env.update(context.env)\n        completed, log_path, error_summary, error_status, process_artifacts = run_command(\n            validator_name="openspec-validate",\n            command=self.command,\n            worktree_path=context.worktree_path,\n            artifact_dir=context.artifact_dir,\n            timeout_seconds=context.timeout_seconds,\n            run_env=run_env,\n            launch_binding=context.launch_binding,\n        )\n        if completed is None:\n            return ValidatorResult(\n                validator=self.name, status=error_status, exit_code=None,\n                log_path=log_path, summary=error_summary,\n                artifacts={"log": log_path, **process_artifacts},\n            )\n        status = "passed" if completed.returncode == 0 else "failed"\n        summary = (\n            "OpenSpec validation passed."\n            if status == "passed"\n            else f"OpenSpec validation failed with exit code {completed.returncode}."\n        )\n        return ValidatorResult(\n            validator=self.name, status=status, exit_code=completed.returncode,\n            log_path=log_path, summary=summary,\n            artifacts={"log": log_path, **process_artifacts},\n        )\n\n\n__all__ = ["OpenSpecValidator"]\n''',
    encoding="utf-8",
)

# Changed-files git status also joins the managed validator boundary.
replace_once(
    "agent_taskflow/validators/changed_files.py",
    "import subprocess\n",
    "import os\nimport subprocess\n",
)
replace_once(
    "agent_taskflow/validators/changed_files.py",
    "from agent_taskflow.mission_contract import read_mission_contract\n",
    '''from agent_taskflow.executor_launch import ExecutorLaunchSpec, run_managed_process\nfrom agent_taskflow.mission_contract import read_mission_contract\n''',
)
replace_once(
    "agent_taskflow/validators/changed_files.py",
    '''def collect_changed_files(worktree_path: Path) -> list[ChangedFile]:\n''',
    '''def collect_changed_files(worktree_path: Path) -> list[ChangedFile]:\n''',
)
insert_anchor = '''    return _parse_porcelain_z(completed.stdout)\n\n\nclass ChangedFilesValidator(Validator):\n'''
insert_text = '''    return _parse_porcelain_z(completed.stdout)\n\n\ndef _collect_changed_files_managed(\n    context: ValidatorContext,\n) -> tuple[list[ChangedFile], dict[str, Path]]:\n    assert context.launch_binding is not None\n    command = [\n        "git", "status", "--porcelain=v1", "-z", "--untracked-files=all",\n    ]\n    stdout_path = context.artifact_dir / "changed-files-git-status.out"\n    stderr_path = context.artifact_dir / "changed-files-git-status.err"\n    run_env = None\n    if context.env is not None:\n        run_env = os.environ.copy()\n        run_env.update(context.env)\n    managed = run_managed_process(\n        context.launch_binding,\n        ExecutorLaunchSpec(\n            executor_name="changed-files-git-status",\n            process_role="validator",\n            argv=tuple(command),\n            cwd=context.worktree_path,\n            artifact_dir=context.artifact_dir,\n            timeout_seconds=context.timeout_seconds,\n            stdin_mode="devnull",\n            combined_output=False,\n            environment_keys=tuple((run_env or {}).keys()),\n        ),\n        stdout_path=stdout_path,\n        stderr_path=stderr_path,\n        run_env=run_env,\n    )\n    artifacts = {\n        "git_status_stdout": stdout_path,\n        "git_status_stderr": stderr_path,\n        "launch_spec": managed.launch_spec_path,\n        "pid_manifest": managed.pid_manifest_path,\n    }\n    if managed.start_error:\n        raise RuntimeError(f"git status failed to start: {managed.start_error}")\n    if managed.timed_out:\n        raise RuntimeError("git status validator timed out")\n    if managed.kill_requested:\n        raise RuntimeError("git status validator aborted by operator kill request")\n    if not managed.verified_exit:\n        raise RuntimeError(\n            "git status validator process-group exit could not be verified; "\n            "verified_exit=false"\n        )\n    if managed.termination_reason == "validator_descendant_cleanup":\n        raise RuntimeError(\n            "git status validator leader exited with live descendants"\n        )\n    if managed.exit_code != 0:\n        error = stderr_path.read_text(encoding="utf-8", errors="replace").strip()\n        raise RuntimeError(error or "git status failed")\n    return _parse_porcelain_z(\n        stdout_path.read_text(encoding="utf-8", errors="replace")\n    ), artifacts\n\n\nclass ChangedFilesValidator(Validator):\n'''
replace_once("agent_taskflow/validators/changed_files.py", insert_anchor, insert_text)
replace_once(
    "agent_taskflow/validators/changed_files.py",
    '''        forbidden_paths: list[str] = []\n\n        with log_path.open("w", encoding="utf-8") as log_file:\n''',
    '''        forbidden_paths: list[str] = []\n        process_artifacts: dict[str, Path] = {}\n\n        with log_path.open("w", encoding="utf-8") as log_file:\n''',
)
replace_once(
    "agent_taskflow/validators/changed_files.py",
    '''                        changed_files = collect_changed_files(context.worktree_path)\n''',
    '''                        if context.launch_binding is None:\n                            changed_files = collect_changed_files(context.worktree_path)\n                        else:\n                            changed_files, process_artifacts = _collect_changed_files_managed(\n                                context\n                            )\n''',
)
for _ in range(4):
    replace_once(
        "agent_taskflow/validators/changed_files.py",
        '''                    artifacts={"log": log_path, "audit": audit_path},\n''',
        '''                    artifacts={\n                        "log": log_path,\n                        "audit": audit_path,\n                        **process_artifacts,\n                    },\n''',
    )

# Install PR-9 after reset so it remains the final store layer.
replace_once(
    "agent_taskflow/__init__.py",
    '''install_reset_runtime_path(\n    dispatcher_module=_dispatcher_module,\n    approved_task_runner_module=_approved_task_runner_module,\n)\n\nDEFAULT_VALIDATORS = _dispatcher_module.DEFAULT_VALIDATORS\n''',
    '''install_reset_runtime_path(\n    dispatcher_module=_dispatcher_module,\n    approved_task_runner_module=_approved_task_runner_module,\n)\n\nfrom agent_taskflow.validator_process_reason_compat import (\n    install_validator_process_reason_compat,\n)\n\ninstall_validator_process_reason_compat()\n\nfrom agent_taskflow.validator_process_runtime_path import (\n    install_validator_process_runtime_path,\n)\n\ninstall_validator_process_runtime_path(\n    dispatcher_module=_dispatcher_module,\n    approved_task_runner_module=_approved_task_runner_module,\n)\n\nDEFAULT_VALIDATORS = _dispatcher_module.DEFAULT_VALIDATORS\n''',
)

# Operator process CLI now reports and targets either role from the shared registry.
replace_once(
    "scripts/terminate_executor_process.py",
    '"""Inspect or hard-terminate registered executor process groups."""',
    '"""Inspect or hard-terminate registered executor or validator process groups."""',
)
replace_once(
    "scripts/terminate_executor_process.py",
    '''        "executor_name": record.executor_name,\n''',
    '''        "process_role": record.process_role,\n        "process_name": record.executor_name,\n        "executor_name": record.executor_name,\n''',
)
replace_once(
    "scripts/terminate_executor_process.py",
    'raise KeyError(f"Executor process not found: {process_id}")',
    'raise KeyError(f"Managed process not found: {process_id}")',
)
replace_once(
    "scripts/terminate_executor_process.py",
    'raise KeyError(f"No active executor process for Attempt: {attempt_id}")',
    'raise KeyError(f"No active managed process for Attempt: {attempt_id}")',
)

# Remove this one-shot script after it has rewritten the branch.
Path(__file__).unlink()
