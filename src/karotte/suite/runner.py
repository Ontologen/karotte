"""Run many tasks × models × samples, resumably, and record what became of each.

Every attempt of every run gets a directory of its own and one line in an
append-only ``results.jsonl``. Outcomes are read from transcripts, never from
exit codes, and only failures that say nothing about the model are retried: a
provider that stayed down through every retry the agent made, or a sandbox
that never started. Retrying anything the model could have caused would
resample its failures and inflate its score.
"""

import hashlib
import json
import socket
import subprocess
import threading
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from karotte.runtime import Runtime, get_engine
from karotte.schemas.evaluation_run_config import EvaluationRunConfig
from karotte.task_image import image_ref

RESULTS_FILE = "results.jsonl"
SUMMARY_FILE = "summary.json"

MODEL_LIMIT_ERRORS = frozenset(
    {
        "TurnLimitReachedError",
        "StepTimeLimitReachedError",
        "StepContextWindowLimitReachedError",
        "EmptyTurnLimitReachedError",
        "ContextWindowExceededError",
        "ContentPolicyViolationError",
    }
)
"""The model ran out of room: a result about the model, scored 0."""

PROVIDER_ERRORS = frozenset(
    {
        "ModelEndpointUnreachableError",
        "RateLimitError",
        "InternalServerError",
        "ServiceUnavailableError",
        "APIConnectionError",
        "AuthenticationError",
    }
)
"""The provider failed through every retry the agent made: nothing about the model.
Not ``Timeout``, which a model's own very long answer can cause."""

FINAL = frozenset({"scored", "model_limit", "error", "build_error"})

BUILD_ATTEMPTS = 3


@dataclass(frozen=True)
class PlannedRun:
    """One run of a suite: a task, a model setup, a sample."""

    task_id: str
    label: str
    sample: int
    config: EvaluationRunConfig
    """Everything but the run id and the transcript file, which each attempt gets."""
    mounts: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        return f"{self.task_id}|{self.label}|{self.sample}"


@dataclass(frozen=True)
class Outcome:
    outcome: str
    score: float | None = None
    verifier_status: str | None = None
    error_type: str | None = None
    error_message: str | None = None
    tokens: dict[str, int | None] = field(default_factory=dict)


def classify(transcript: Path) -> Outcome:
    """What a run's transcript says became of it."""
    try:
        raw = json.loads(transcript.read_text())
    except (OSError, ValueError):
        # Nothing was streamed, so the sandbox never ran the task.
        return Outcome("infra_error", error_type="NoTranscript")
    events: list[dict[str, Any]] = raw["events"] if isinstance(raw, dict) else raw
    tokens: dict[str, int | None] = {"input": None, "output": None}
    for event in events:
        if event.get("type") == "token_usage":
            tokens["input"] = (tokens["input"] or 0) + event.get("input_tokens", 0)
            tokens["output"] = (tokens["output"] or 0) + event.get("output_tokens", 0)
    scoring = next((e["scoring"] for e in events if e.get("type") == "scoring"), None)
    error = next((e for e in events if e.get("type") == "error"), None)
    completed = any(e.get("type") == "task_completed" for e in events)
    if scoring is not None:
        return Outcome(
            "scored",
            score=scoring["score"],
            verifier_status=scoring.get("metadata", {}).get("verifier_status"),
            tokens=tokens,
        )
    if error is not None:
        kind = error.get("exception_type", "")
        outcome = (
            "model_limit"
            if kind in MODEL_LIMIT_ERRORS
            else "infra_error"
            if kind in PROVIDER_ERRORS
            else "error"
        )
        return Outcome(
            outcome,
            score=0.0 if outcome == "model_limit" else None,
            error_type=kind,
            error_message=str(error.get("message", ""))[:500],
            tokens=tokens,
        )
    if not completed:
        # It started and then stopped: possibly the student's doing, so not retried.
        return Outcome("error", error_type="Unfinished", tokens=tokens)
    return Outcome("error", error_type="NoScore", tokens=tokens)


def run_suite(
    planned: Sequence[PlannedRun],
    out_dir: Path,
    *,
    runtime: Runtime,
    build_image: Callable[[str], None],
    image_of: Callable[[str], str],
    build_context: str = ".",
    concurrency: int = 1,
    max_retries: int = 2,
    prune_images: bool = False,
) -> list[dict[str, Any]]:
    """Run every planned run not already final in ``out_dir``; returns every
    result line, old and new.

    ``image_of`` names the image a task runs in; ``build_image`` builds the
    image a task runs in, once per image however many runs need it.
    """
    from karotte.run_helpers import clean_up_old_containers, run_containerized

    out_dir.mkdir(parents=True, exist_ok=True)
    results_path = out_dir / RESULTS_FILE
    results = _read_results(results_path)
    final = {r["run_key"] for r in results if r["final"]}
    todo = sorted(
        (run for run in planned if run.key not in final),
        key=lambda run: (image_of(run.task_id), run.key),
    )
    if not todo:
        return results
    suite_id = hashlib.sha1(str(out_dir.resolve()).encode()).hexdigest()[:8]
    clean_up_old_containers(runtime, [f"{suite_id}-{_run_hash(r)}" for r in todo])

    lock = threading.Lock()
    builds: dict[str, threading.Lock] = defaultdict(threading.Lock)
    built: dict[str, bool] = {}
    remaining = Counter(image_of(run.task_id) for run in todo)

    def record(line: dict[str, Any]) -> None:
        with lock:
            results.append(line)
            with results_path.open("a") as f:
                f.write(json.dumps(line, sort_keys=True) + "\n")

    def ensure_image(image: str, task_id: str) -> bool:
        """Build ``image`` once. A build involves no model, so retrying one
        (a download that dropped, say) can't bias a score."""
        with builds[image]:
            for attempt in range(1, BUILD_ATTEMPTS + 1):
                if image in built:
                    break
                try:
                    build_image(task_id)
                    built[image] = True
                except BaseException as e:  # noqa: BLE001 - typer aborts are exceptions too
                    logger.error(f"Could not build {image} (attempt {attempt}): {e!r}")
                    if attempt == BUILD_ATTEMPTS:
                        built[image] = False
            return built[image]

    def execute(run: PlannedRun) -> None:
        image = image_of(run.task_id)
        attempts = [r["attempt"] for r in results if r["run_key"] == run.key]
        attempt = max(attempts, default=0)
        try:
            if not ensure_image(image, run.task_id):
                record(
                    _line(
                        run,
                        attempt + 1,
                        Outcome("build_error"),
                        "",
                        final=True,
                        image=image,
                    )
                )
                return
            while True:
                attempt += 1
                run_id = f"{suite_id}-{_run_hash(run)}-a{attempt}"
                attempt_dir = (
                    out_dir
                    / "runs"
                    / run.task_id
                    / run.label
                    / f"s{run.sample}"
                    / f"a{attempt}"
                )
                attempt_dir.mkdir(parents=True, exist_ok=True)
                started = time.monotonic()
                config = run.config.model_copy(
                    update={
                        "run_id": run_id,
                        "transcript_file": str(attempt_dir / "transcript.json"),
                        # Free on the host, whatever else runs there.
                        "websocket_config": run.config.websocket_config.model_copy(
                            update={"port": _free_port()}
                        ),
                    }
                )
                try:
                    run_containerized(
                        config,
                        runtime,
                        dev=False,
                        log_file=attempt_dir / "log.txt",
                        build_context=build_context,
                        mounts=list(run.mounts),
                        parallel_runs=concurrency,
                    )
                except (subprocess.CalledProcessError, RuntimeError) as e:
                    # A run that ends in an error exits non-zero; its transcript says why.
                    logger.debug(f"{run.key} attempt {attempt}: {e!r}")
                outcome = classify(attempt_dir / "transcript.json")
                retry = outcome.outcome == "infra_error" and attempt <= max_retries
                record(
                    _line(
                        run,
                        attempt,
                        outcome,
                        run_id,
                        final=not retry,
                        image=image,
                        duration=time.monotonic() - started,
                        transcript=attempt_dir / "transcript.json",
                    )
                )
                logger.info(
                    f"{run.key} attempt {attempt}: {outcome.outcome} {outcome.score}"
                )
                if not retry:
                    return
        finally:
            with lock:
                remaining[image] -= 1
                last = remaining[image] == 0
            if last and prune_images:
                _remove_image(runtime, image)

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        for future in [pool.submit(execute, run) for run in todo]:
            future.result()
    (out_dir / SUMMARY_FILE).write_text(json.dumps(summarize(results), indent=1) + "\n")
    return results


def summarize(results: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Per label: how the runs ended, and the mean score two ways — with every
    run that never got a score as 0, and over the scored runs alone."""
    last: dict[str, dict[str, Any]] = {}
    for line in results:
        last[line["run_key"]] = line
    by_label: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for line in last.values():
        by_label[line["label"]].append(line)
    summary: dict[str, Any] = {}
    for label, lines in sorted(by_label.items()):
        scores = [line["score"] for line in lines if line["score"] is not None]
        per_task: dict[str, list[float]] = defaultdict(list)
        for line in lines:
            per_task[line["task_id"]].append(line["score"] or 0.0)
        summary[label] = {
            "runs": len(lines),
            "outcomes": dict(Counter(line["outcome"] for line in lines)),
            "verifier_status": dict(
                Counter(
                    line["verifier_status"] for line in lines if line["verifier_status"]
                )
            ),
            "mean_score": sum(line["score"] or 0.0 for line in lines) / len(lines),
            "mean_score_of_scored": sum(scores) / len(scores) if scores else None,
            "per_task": {t: sum(s) / len(s) for t, s in sorted(per_task.items())},
        }
    return summary


def _line(
    run: PlannedRun,
    attempt: int,
    outcome: Outcome,
    run_id: str,
    *,
    final: bool,
    image: str,
    duration: float | None = None,
    transcript: Path | None = None,
) -> dict[str, Any]:
    from importlib.metadata import version

    return {
        "run_key": run.key,
        "task_id": run.task_id,
        "label": run.label,
        "model": run.config.model,
        "sample": run.sample,
        "attempt": attempt,
        "run_id": run_id,
        "outcome": outcome.outcome,
        "final": final,
        "score": outcome.score,
        "verifier_status": outcome.verifier_status,
        "error_type": outcome.error_type,
        "error_message": outcome.error_message,
        "tokens": outcome.tokens,
        "duration_s": None if duration is None else round(duration, 1),
        "transcript": None if transcript is None else str(transcript),
        "image": image,
        "karotte_version": version("karotte"),
    }


def _read_results(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _run_hash(run: PlannedRun) -> str:
    """Fixed width, so no run's id is a prefix of another's."""
    return hashlib.sha1(run.key.encode()).hexdigest()[:10]


def _remove_image(runtime: Runtime, image: str) -> None:
    engine = get_engine(runtime)
    if engine == "container":
        logger.info(
            f"Not removing {image}: Apple `container` images are removed by hand"
        )
        return
    result = subprocess.run(
        [engine, "image", "rm", "--force", image_ref(image, engine)],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        logger.warning(
            f"Could not remove {image}: {result.stderr.decode(errors='replace').strip()}"
        )
