import json
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, get_args

import typer

from karotte import Runtime

if TYPE_CHECKING:
    from karotte.suite.runner import PlannedRun

app = typer.Typer(help="Run many tasks × model setups × samples, resumably.")


@app.command("run")
def run_suite_command(
    suite: Annotated[str, typer.Argument(help="The suite file (TOML).")],
    out: Annotated[
        str | None,
        typer.Option(help="Where results go. Default: out/suites/<suite name>."),
    ] = None,
    runtime: Annotated[
        Runtime | None,
        typer.Option(
            metavar="NAME", help=f"Container runtime: {', '.join(get_args(Runtime))}."
        ),
    ] = None,
    concurrency: Annotated[int, typer.Option(min=1, help="Runs at once.")] = 1,
    max_retries: Annotated[
        int,
        typer.Option(min=0, help="Retries of a run whose provider or sandbox failed."),
    ] = 2,
    prune_images: Annotated[
        bool, typer.Option(help="Remove each task's image after its last run.")
    ] = False,
) -> None:
    """Run a suite, skipping the runs an earlier invocation finished."""
    import tomllib

    from karotte.load_tasks import load_all_task_classes, require_environment
    from karotte.suite.suite_file import load_suite

    require_environment()
    task_ids = [cls.id for cls in load_all_task_classes()]
    try:
        planned = load_suite(Path(suite), task_ids)
        name = tomllib.loads(Path(suite).read_text())["name"]
    except (ValueError, OSError, tomllib.TOMLDecodeError) as e:
        typer.secho(f"Invalid suite {suite}: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from e
    out_dir = Path(out) if out else Path("out") / "suites" / name
    results = execute(planned, out_dir, runtime, concurrency, max_retries, prune_images)
    _print_summary(out_dir)
    raise typer.Exit(0 if _all_good(results) else 1)


@app.command("summary")
def summary_command(
    out: Annotated[str, typer.Argument(help="A suite's results directory.")],
) -> None:
    """Summarize a suite's results so far."""
    from karotte.suite.runner import RESULTS_FILE, summarize

    lines = [
        json.loads(line)
        for line in (Path(out) / RESULTS_FILE).read_text().splitlines()
        if line.strip()
    ]
    typer.echo(json.dumps(summarize(lines), indent=1))


def execute(
    planned: Sequence["PlannedRun"],
    out_dir: Path,
    runtime: Runtime | None,
    concurrency: int,
    max_retries: int,
    prune_images: bool,
    build_context: str = ".",
) -> list[dict[str, Any]]:
    """Check the runtime once, then run ``planned`` with each task's image
    built when its first run needs it."""
    from karotte.build import (
        build_container,
        require_builder,
        require_runtime,
        task_image_build_options,
    )
    from karotte.cli.build import (
        _default_build_runtime,  # pyright: ignore[reportPrivateUsage]
    )
    from karotte.load_tasks import load_all_task_classes
    from karotte.schemas.evaluation_run_config import EvaluationRunConfig
    from karotte.suite.runner import run_suite
    from karotte.task_image import image_tag

    placeholder = EvaluationRunConfig(run_id="", task_id="", model="", model_api_key="")
    wanted = {run.task_id for run in planned}
    tasks = {
        cls.id: cls(placeholder) for cls in load_all_task_classes() if cls.id in wanted
    }
    if runtime is None:
        runtime = _default_build_runtime(list(tasks.values()))
    require_runtime(runtime)
    require_builder(runtime)
    for hardware, platform in {
        (t.required_hardware, t.base_image and t.base_image.platform)
        for t in tasks.values()
    }:
        if runtime == "apple-container":
            from karotte.apple_container import validate_container_runtime

            validate_container_runtime(hardware)
        elif runtime == "firecracker":
            from karotte.firecracker.preflight import validate_firecracker_runtime

            validate_firecracker_runtime(hardware, None, platform=platform)

    def build_image(task_id: str) -> None:
        task = tasks[task_id]
        build_container(
            runtime,
            build_context,
            **task_image_build_options(build_context, task.id, task.base_image),
        )

    return run_suite(
        planned,
        out_dir,
        runtime=runtime,
        build_image=build_image,
        image_of=lambda task_id: image_tag(tasks[task_id].base_image),
        build_context=build_context,
        concurrency=concurrency,
        max_retries=max_retries,
        prune_images=prune_images,
    )


def _all_good(results: list[dict[str, Any]]) -> bool:
    last = {r["run_key"]: r for r in results}
    return all(
        r["final"] and r["outcome"] in ("scored", "model_limit") for r in last.values()
    )


def _print_summary(out_dir: Path) -> None:
    from karotte.suite.runner import SUMMARY_FILE

    path = out_dir / SUMMARY_FILE
    if path.is_file():
        typer.echo(path.read_text())
    typer.echo(f"Results: {out_dir}")
