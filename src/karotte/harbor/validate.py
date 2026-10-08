"""Check that imported tasks grade as they must before anyone trusts a score.

For each task, two scripted runs through the real grading path, on the runtime
that will run it for real: the reference solution must score 1, and handing in
nothing must score 0 with its tests run (not crashed). Results go to
``harbor.validation.json`` next to the lock, with the image and content they
were checked against, so a re-import that changes either makes them stale.
"""

import json
from importlib.metadata import version
from pathlib import Path
from typing import Any

from karotte.harbor import LOCK_FILE, VALIDATION_FILE
from karotte.harbor.importer import LOCK_DIR, Source, solution_dir
from karotte.schemas.evaluation_run_config import EvaluationRunConfig
from karotte.suite.runner import PlannedRun

ORACLE = "oracle"
NOP = "nop"


def plan_validation(env_dir: Path, task_ids: list[str]) -> list[PlannedRun]:
    """An oracle and a nop run for each of ``task_ids``, the oracle's with that
    task's solution, and only that, mounted read-only."""
    lock = json.loads((env_dir / LOCK_DIR / LOCK_FILE).read_text())
    sources = {s["name"]: Source.model_validate(s) for s in lock["sources"]}
    planned: list[PlannedRun] = []
    for task_id in task_ids:
        locked = lock["tasks"][task_id]
        source = sources[locked["source"]["name"]]
        solution = (
            env_dir
            / (source.local or "")
            / source.path
            / locked["dir_name"]
            / "solution"
            if source.local is not None
            else solution_dir(source, locked["dir_name"])
        )
        for mode in (ORACLE, NOP):
            config = EvaluationRunConfig(
                run_id="planned",
                task_id=task_id,
                model="fake",
                use_fake_model=True,
                extra_config={"validation": mode},
            )
            mounts = (f"{solution}:/solution:ro",) if mode == ORACLE else ()
            planned.append(PlannedRun(task_id, mode, 0, config, mounts))
    return planned


def record_validation(
    env_dir: Path, results: list[dict[str, Any]], runtime: str
) -> dict[str, dict[str, Any]]:
    """Write what the oracle and nop runs show into ``harbor.validation.json``,
    keeping earlier results for tasks not run now. Returns this run's."""
    lock_dir = env_dir / LOCK_DIR
    lock = json.loads((lock_dir / LOCK_FILE).read_text())
    path = lock_dir / VALIDATION_FILE
    recorded: dict[str, Any] = json.loads(path.read_text()) if path.is_file() else {}
    last = {r["run_key"]: r for r in results}
    checked: dict[str, dict[str, Any]] = {}
    for task_id in sorted({r["task_id"] for r in results}):
        oracle = last.get(f"{task_id}|{ORACLE}|0", {})
        nop = last.get(f"{task_id}|{NOP}|0", {})
        # A verdict only when both runs were graded: a build or a provider that
        # failed says nothing about the task.
        graded = oracle.get("outcome") == "scored" and nop.get("outcome") == "scored"
        passed = (
            None
            if not graded
            else oracle.get("score") == 1
            and oracle.get("verifier_status") == "ok"
            and nop.get("score") == 0
            and nop.get("verifier_status") == "ok"
        )
        locked = lock["tasks"][task_id]
        checked[task_id] = {
            "passed": passed,
            "oracle": _brief(oracle),
            "nop": _brief(nop),
            "image": locked["image"]["ref"],
            "content_sha256": locked["content_sha256"],
            "runtime": runtime,
            "karotte_version": version("karotte"),
        }
    recorded.update(checked)
    path.write_text(json.dumps(recorded, indent=1, sort_keys=True) + "\n")
    return checked


def _brief(result: dict[str, Any]) -> dict[str, Any]:
    return {
        key: result.get(key)
        for key in ("outcome", "score", "verifier_status", "error_type", "transcript")
    }
