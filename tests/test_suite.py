import json
from pathlib import Path
from typing import Any

import pytest

from karotte.schemas.evaluation_run_config import EvaluationRunConfig
from karotte.suite import runner
from karotte.suite.runner import PlannedRun, classify, run_suite, summarize
from karotte.suite.suite_file import load_suite


def _transcript(path: Path, *events: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"run_id": "r", "events": list(events)}))
    return path


SCORED = {
    "type": "scoring",
    "scoring": {
        "score": 1.0,
        "metadata": {"verifier_status": "ok"},
        "continue_task": True,
    },
}
COMPLETED = {"type": "task_completed", "status": "passed"}


def _error(kind: str) -> dict[str, Any]:
    return {"type": "error", "exception_type": kind, "message": f"{kind} happened"}


class TestClassify:
    def test_a_score_is_a_result(self, tmp_path: Path):
        outcome = classify(_transcript(tmp_path / "t.json", SCORED, COMPLETED))
        assert (outcome.outcome, outcome.score, outcome.verifier_status) == (
            "scored",
            1.0,
            "ok",
        )

    @pytest.mark.parametrize(
        "kind", ["TurnLimitReachedError", "StepTimeLimitReachedError"]
    )
    def test_running_out_of_room_is_the_models_and_scores_0(
        self, tmp_path: Path, kind: str
    ):
        outcome = classify(_transcript(tmp_path / "t.json", _error(kind), COMPLETED))
        assert (outcome.outcome, outcome.score) == ("model_limit", 0.0)

    @pytest.mark.parametrize(
        "kind", ["RateLimitError", "ModelEndpointUnreachableError"]
    )
    def test_a_provider_that_stayed_down_is_infra(self, tmp_path: Path, kind: str):
        assert (
            classify(_transcript(tmp_path / "t.json", _error(kind))).outcome
            == "infra_error"
        )

    def test_no_transcript_is_a_sandbox_that_never_ran(self, tmp_path: Path):
        assert classify(tmp_path / "missing.json").outcome == "infra_error"

    def test_anything_else_is_an_error_never_retried(self, tmp_path: Path):
        assert (
            classify(_transcript(tmp_path / "t.json", _error("ValueError"))).outcome
            == "error"
        )

    def test_a_run_that_stopped_partway_is_not_retried(self, tmp_path: Path):
        """The student could have brought it down."""
        started = {"type": "task_started"}
        assert classify(_transcript(tmp_path / "t.json", started)).outcome == "error"


def _planned(*keys: tuple[str, str, int]) -> list[PlannedRun]:
    config = EvaluationRunConfig(
        run_id="planned", task_id="x", model="m", model_api_key="k"
    )
    return [
        PlannedRun(t, label, sample, config.model_copy(update={"task_id": t}))
        for t, label, sample in keys
    ]


class FakeRuns:
    """Stands in for `run_containerized`: writes the transcript each attempt is scripted to."""

    def __init__(self, script: dict[str, list[list[dict[str, Any]]]]) -> None:
        self.script: dict[str, list[list[dict[str, Any]]]] = script
        self.calls: list[str] = []

    def __call__(self, config: EvaluationRunConfig, *_: object, **__: object) -> None:
        self.calls.append(config.task_id)
        events = self.script[config.task_id].pop(0)
        if events:
            _transcript(Path(str(config.transcript_file)), *events)


@pytest.fixture
def no_containers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("karotte.run_helpers.clean_up_old_containers", lambda *_: None)  # pyright: ignore[reportUnknownLambdaType]


def _run(
    planned: list[PlannedRun],
    out: Path,
    fake: FakeRuns,
    monkeypatch: pytest.MonkeyPatch,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    monkeypatch.setattr("karotte.run_helpers.run_containerized", fake)
    return run_suite(
        planned,
        out,
        runtime="docker",
        build_image=kwargs.pop("build_image", lambda _task: None),  # pyright: ignore[reportUnknownLambdaType]
        image_of=lambda task: f"img-{task}",
        **kwargs,
    )


@pytest.mark.usefixtures("no_containers")
class TestRunSuite:
    def test_retries_infra_errors_up_to_the_cap(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        fake = FakeRuns({"a": [[], [_error("RateLimitError")], [SCORED, COMPLETED]]})
        results = _run(
            _planned(("a", "m", 0)), tmp_path, fake, monkeypatch, max_retries=2
        )

        assert [r["outcome"] for r in results] == [
            "infra_error",
            "infra_error",
            "scored",
        ]
        assert [r["final"] for r in results] == [False, False, True]

    def test_gives_up_after_the_cap(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        fake = FakeRuns({"a": [[], []]})
        results = _run(
            _planned(("a", "m", 0)), tmp_path, fake, monkeypatch, max_retries=1
        )

        assert [(r["outcome"], r["final"]) for r in results] == [
            ("infra_error", False),
            ("infra_error", True),
        ]

    def test_never_retries_what_the_model_could_cause(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        fake = FakeRuns(
            {
                "a": [[_error("ValueError")]],
                "b": [[_error("TurnLimitReachedError"), COMPLETED]],
            }
        )
        results = _run(
            _planned(("a", "m", 0), ("b", "m", 0)),
            tmp_path,
            fake,
            monkeypatch,
            max_retries=3,
        )

        assert sorted(fake.calls) == ["a", "b"]
        assert all(r["final"] for r in results)

    def test_resumes_where_it_stopped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        planned = _planned(("a", "m", 0), ("b", "m", 0))
        _run(planned[:1], tmp_path, FakeRuns({"a": [[SCORED, COMPLETED]]}), monkeypatch)
        fake = FakeRuns({"b": [[SCORED, COMPLETED]]})
        results = _run(planned, tmp_path, fake, monkeypatch)

        assert fake.calls == ["b"]
        assert len(results) == 2
        lines = (tmp_path / runner.RESULTS_FILE).read_text().splitlines()
        assert len(lines) == 2

    def test_a_failed_build_fails_that_images_runs_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        def build_image(task: str) -> None:
            if task == "a":
                raise RuntimeError("no such base")

        fake = FakeRuns({"b": [[SCORED, COMPLETED]]})
        results = _run(
            _planned(("a", "m", 0), ("b", "m", 0)),
            tmp_path,
            fake,
            monkeypatch,
            build_image=build_image,
        )

        assert {r["task_id"]: r["outcome"] for r in results} == {
            "a": "build_error",
            "b": "scored",
        }

    def test_a_build_that_fails_once_is_retried(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        builds: list[str] = []

        def build_image(task: str) -> None:
            builds.append(task)
            if len(builds) == 1:
                raise RuntimeError("download dropped")

        fake = FakeRuns({"a": [[SCORED, COMPLETED]]})
        results = _run(
            _planned(("a", "m", 0)),
            tmp_path,
            fake,
            monkeypatch,
            build_image=build_image,
        )

        assert builds == ["a", "a"]
        assert [r["outcome"] for r in results] == ["scored"]

    def test_each_attempt_gets_its_own_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        fake = FakeRuns({"a": [[], [SCORED, COMPLETED]]})
        results = _run(_planned(("a", "m", 0)), tmp_path, fake, monkeypatch)

        transcripts = [r["transcript"] for r in results]
        assert transcripts[0] != transcripts[1]
        assert len({r["run_id"] for r in results}) == 2


def test_summary_counts_unscored_runs_as_zero():
    lines = [
        {
            "run_key": "a|m|0",
            "task_id": "a",
            "label": "m",
            "outcome": "scored",
            "score": 1.0,
            "verifier_status": "ok",
        },
        {
            "run_key": "b|m|0",
            "task_id": "b",
            "label": "m",
            "outcome": "error",
            "score": None,
            "verifier_status": None,
        },
    ]
    summary = summarize(lines)["m"]
    assert summary["mean_score"] == 0.5
    assert summary["mean_score_of_scored"] == 1.0
    assert summary["outcomes"] == {"scored": 1, "error": 1}


class TestSuiteFile:
    def test_expands_tasks_models_and_samples(self, tmp_path: Path):
        suite = tmp_path / "suite.toml"
        suite.write_text(
            'name = "s"\nsamples = 2\ntasks = ["deepswe-*"]\nexclude = ["deepswe-b"]\n'
            + "[defaults]\nstep_time_limit_seconds = 600\n"
            + '[[models]]\nlabel = "one"\nmodel = "fake"\nuse_fake_model = true\n'
        )
        planned = load_suite(suite, ["deepswe-a", "deepswe-b", "other"])

        assert [p.key for p in planned] == ["deepswe-a|one|0", "deepswe-a|one|1"]
        assert planned[0].config.step_time_limit_seconds == 600

    def test_per_run_fields_are_not_defaults(self, tmp_path: Path):
        suite = tmp_path / "suite.toml"
        suite.write_text(
            'name = "s"\n[defaults]\nrun_id = "x"\n[[models]]\nlabel = "one"\nmodel = "fake"\nuse_fake_model = true\n'
        )
        with pytest.raises(ValueError, match="run_id"):
            load_suite(suite, ["a"])
