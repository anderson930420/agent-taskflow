"""Characterization: the runtime chain ``import agent_taskflow`` installs.

V0 Scope Freeze, batch 1 (RULINGS 74/80/81). This test changes no behaviour.
It pins, as it is on ``main``, the composition the V0 execution path runs
through (docs/v0-supported-surface.md, the installer rows). The eleven
installers in ``agent_taskflow/__init__.py`` build it at import time, and class
bases depend on their order. Any later §6.4 change that removes, reorders or
skips an installer must update this test on purpose, not by accident.
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

import agent_taskflow  # noqa: F401  installs the layered runtime path
from agent_taskflow import approved_task_runner
from agent_taskflow import attempt_scoped_runtime_path as attempt_path
from agent_taskflow import canonical_runtime_path as canonical_path
from agent_taskflow import dispatcher as dispatcher_module
from agent_taskflow import lifecycle_control
from agent_taskflow.validator_process_runtime_path import ValidatorProcessRuntimeTaskStore

_LAYER_MARKERS = (
    "__lifecycle_entrypoint_controls__",
    "__attempt_scoped_runtime__",
    "__canonical_runtime__",
)


def _names(classes: tuple[type, ...]) -> list[str]:
    return [f"{cls.__module__}.{cls.__qualname__}" for cls in classes if cls is not object]


class InstalledRuntimeChainTests(unittest.TestCase):
    def test_dispatcher_layers_in_order(self) -> None:
        mro = [cls for cls in dispatcher_module.Dispatcher.__mro__ if cls is not object]
        self.assertEqual(len(mro), 4)
        # Every layer reports the name "Dispatcher"; only the markers tell them apart.
        self.assertEqual({cls.__name__ for cls in mro}, {"Dispatcher"})
        markers = [
            [marker for marker in _LAYER_MARKERS if vars(cls).get(marker) is True]
            for cls in mro
        ]
        self.assertEqual(markers, [[marker] for marker in _LAYER_MARKERS] + [[]])
        self.assertEqual(mro[-1].__module__, "agent_taskflow.dispatcher")

    def test_final_runtime_store_mro(self) -> None:
        self.assertEqual(
            _names(ValidatorProcessRuntimeTaskStore.__mro__),
            [
                "agent_taskflow.validator_process_runtime_path.ValidatorProcessRuntimeTaskStore",
                "agent_taskflow.reset_runtime_path.ResetLineageRuntimeTaskStore",
                "agent_taskflow.executor_process_runtime_path.ExecutorProcessRuntimeTaskStore",
                "agent_taskflow.lifecycle_runtime_path.LifecycleRuntimeTaskStore",
                "agent_taskflow.attempt_scoped_runtime_path.AttemptScopedRuntimeTaskStore",
                "agent_taskflow.canonical_runtime_path.CanonicalRuntimeTaskStore",
                "agent_taskflow.store.TaskMirrorStore",
            ],
        )
        with tempfile.TemporaryDirectory(prefix="v0-chain-") as tmp:
            store = canonical_path.canonical_runtime_task_store(Path(tmp) / "state.db")
        self.assertIs(type(store), ValidatorProcessRuntimeTaskStore)

    def test_admission_store_mro(self) -> None:
        self.assertEqual(
            _names(canonical_path.CanonicalRuntimeAdmissionStore.__mro__),
            [
                "agent_taskflow.reset_runtime_path.ResetAwareRuntimeAdmissionStore",
                "agent_taskflow.lifecycle_runtime_path.LifecycleRuntimeAdmissionStore",
                "agent_taskflow.canonical_runtime_path.CanonicalRuntimeAdmissionStore",
                "agent_taskflow.runtime_admission.RuntimeAdmissionStore",
            ],
        )
        # The compat layer restores the public PR-4 symbol.
        self.assertEqual(
            _names((canonical_path.CanonicalRuntimeTaskStore,)),
            ["agent_taskflow.canonical_runtime_path.CanonicalRuntimeTaskStore"],
        )

    def test_store_factory_bindings(self) -> None:
        # Last writer wins: only the validator-process installer's bindings are live.
        for function, name in (
            (canonical_path._canonicalize_store, "validator_canonicalize_store"),
            (attempt_path._attempt_store_for_request, "validator_attempt_store_for_request"),
        ):
            with self.subTest(name=name):
                self.assertEqual(function.__module__, "agent_taskflow.validator_process_runtime_path")
                self.assertEqual(
                    function.__qualname__,
                    f"install_validator_process_runtime_path.<locals>.{name}",
                )
        patched = attempt_path.AttemptScopedRuntimeTaskStore.update_task_status
        self.assertEqual(patched.__module__, "agent_taskflow.attempt_scoped_runtime_compat")
        self.assertEqual(
            patched.__qualname__,
            "install_attempt_scoped_runtime_compat.<locals>.update_task_status",
        )

    def test_legacy_runner_wrappers(self) -> None:
        wrapper = approved_task_runner.run_approved_task
        for marker in _LAYER_MARKERS:
            self.assertIs(getattr(wrapper, marker, False), True, marker)
        depth = 0
        while hasattr(wrapper, "__wrapped__"):
            wrapper = wrapper.__wrapped__
            depth += 1
        self.assertEqual(depth, 3)
        self.assertEqual(wrapper.__module__, "agent_taskflow.approved_task_runner")

    def test_runtime_reason_codes(self) -> None:
        codes = lifecycle_control.RUNTIME_REASON_CODES
        self.assertIn("canonical_runtime_pickup_claimed", codes)
        self.assertIn("executor_descendant_cleanup", codes)
        self.assertIn("validator_descendant_cleanup", codes)
        self.assertEqual(len(codes), 47)


if __name__ == "__main__":
    unittest.main()
