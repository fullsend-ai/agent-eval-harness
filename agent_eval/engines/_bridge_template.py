"""cap-evolve's CapabilityAdapter bridge into agent-eval-harness.

This file is COPIED byte-for-byte (not imported) into each generated
cap-evolve project as ``adapters/aeh_bridge.py`` by
``agent_eval.engines.capevolve``. It is never imported from the harness's own
install location at run time: cap-evolve's loader already puts the
``adapters/`` directory itself on ``sys.path`` (``cap_evolve/check.py``'s
``load_adapter``), so the generated ``adapters/adapter.py`` shim can do
``from aeh_bridge import Bridge as Adapter`` with no dependency on where the
harness that scaffolded the project happens to live. That matters because a
campaign can run for hours, unattended, well after the invoking session ends.

Deliberately stdlib-only, and never imports ``agent_eval._bootstrap``:
cap-evolve loads this in its own interpreter (a console script, so the
bootstrap's execv-on-ABI-mismatch guard would misfire there). Every harness
operation below runs in a freshly spawned subprocess that activates the real
harness venv itself.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

try:
    from cap_evolve import CapabilityAdapter, Rollout, Score, Task
except ModuleNotFoundError as exc:  # Keep non-optimization harness installs importable.
    if exc.name != "cap_evolve":
        raise
    from dataclasses import dataclass, field

    class CapabilityAdapter:
        """Import-time fallback for unit tests without the optional extra."""

        def runner_model(self):
            return None

    @dataclass
    class Task:
        id: str
        input: object = None
        target: object = None
        metadata: dict = field(default_factory=dict)

    @dataclass
    class Rollout:
        task_id: str
        output: object = None
        trace: object = None
        tool_calls: list = field(default_factory=list)
        cost_usd: float = 0.0
        tokens: int = 0
        error: str | None = None
        metadata: dict = field(default_factory=dict)

        def to_dict(self):
            return vars(self)

        @classmethod
        def from_dict(cls, value):
            return cls(**value)

    @dataclass
    class Score:
        task_id: str
        reward: float = 0.0
        feedback: str = ""
        raw: dict = field(default_factory=dict)

        def to_dict(self):
            return vars(self)

        @classmethod
        def from_dict(cls, value):
            return cls(**value)


class Bridge(CapabilityAdapter):
    """Implements cap-evolve's tasks/run_target/score contract for harness evals.

    Zero-arg ``Adapter()`` construction (cap-evolve's requirement) is provided
    by the generated ``adapter.py`` shim, which passes this class the on-disk
    ``substrate.json`` path next to it.
    """

    def __init__(self, substrate_path: str | Path):
        self.substrate_path = Path(substrate_path).resolve()
        self.substrate = json.loads(self.substrate_path.read_text(encoding="utf-8"))
        self.manifest = json.loads(
            self.substrate_path.with_name("candidate.json").read_text(encoding="utf-8")
        )
        self.project_root = Path(self.manifest["project_root"]).resolve()
        self.task_ids = tuple(str(task_id) for task_id in self.manifest["task_ids"])

    def _invoke_rollout(self, task_id: str, candidate_dir: Path, seed: int) -> dict:
        env = dict(os.environ)
        # Belt and suspenders: the chosen interpreter (below) should already
        # have the harness installed, but prepend its source root too in case
        # it's reachable only via PYTHONPATH rather than an installed package.
        harness_root = self.manifest.get("harness_root")
        if harness_root:
            existing = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = str(harness_root) + (os.pathsep + existing if existing else "")
        # The bootstrap sentinel belongs to this process only. Pop it after all
        # other environment edits so each child can activate its own venv.
        env.pop("_AGENT_EVAL_BOOTSTRAP_DONE", None)
        harness_python = self.substrate.get("harness_python") or sys.executable
        command = [
            harness_python, "-m", "agent_eval.optimize.rollout",
            "--manifest", str(self.substrate_path.with_name("candidate.json")),
            "--task-id", task_id,
            "--candidate-dir", str(candidate_dir),
            "--seed", str(seed),
        ]
        proc = subprocess.run(command, cwd=self.project_root, env=env, capture_output=True, text=True)
        # The rollout's stdout is a strict JSON contract. Stderr is diagnostic
        # text only and is never parsed for results.
        if not proc.stdout.strip():
            raise RuntimeError("harness rollout returned empty stdout")
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"harness rollout returned invalid JSON: {exc}") from exc
        if proc.returncode != 0 or payload.get("error"):
            detail = payload.get("error") or (proc.stderr or "rollout failed").strip()
            raise RuntimeError(str(detail))
        return payload

    def tasks(self, split: str) -> list:
        # cap-evolve freezes the split itself by filtering tasks('all').
        # Returning a stable full list is required for that split machinery.
        task_inputs = self.manifest.get("task_inputs") or {}
        return [
            Task(id=task_id, input=task_inputs.get(task_id), metadata={"case_id": task_id})
            for task_id in self.task_ids
        ]

    def run_target(self, task, ctx, *, seed: int = 0):
        try:
            candidate_dir = Path(ctx).resolve()
            result = self._invoke_rollout(task.id, candidate_dir, seed)
            return Rollout.from_dict(result["rollout"])
        except Exception as exc:
            # cap-evolve interprets Rollout.error as missing data/noise. Raising
            # here would abort serial evaluations instead of applying on_error.
            return Rollout(
                task_id=task.id,
                error=f"harness rollout failed ({type(exc).__name__}); measurement unavailable",
            )

    def score(self, task, rollout):
        """A pure read of what run_target() already judged.

        No subprocess call, no disk access on the live path — run_target()
        already embedded the composed reward and feedback into this exact
        Rollout's metadata. cap-evolve never catches a score() exception and
        treats any unmarked Score as a real reward averaged into the gate, so
        every non-measurement path below must carry raw={"errored": True}.
        """
        if getattr(rollout, "error", None):
            return Score(
                task_id=task.id, reward=0.0,
                feedback="Harness rollout failed for an infrastructure reason; treat this as missing data.",
                raw={"errored": True},
            )
        meta = getattr(rollout, "metadata", None) or {}
        if not meta.get("harness_run_id"):
            # cap-evolve's own `check` probes score() with a synthetic rollout.
            return Score(
                task_id=task.id, reward=0.0, feedback="No harness result in probe rollout.",
                raw={"errored": True},
            )
        return Score(task_id=task.id, reward=meta.get("reward", 0.0), feedback=meta.get("feedback", ""), raw={})

    def runner_model(self):
        return self.manifest.get("target_model") or None
