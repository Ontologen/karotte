from pathlib import Path
from typing import TYPE_CHECKING, Annotated, get_args

import typer
from loguru import logger

from karotte import Runtime
from karotte.build import build_container, require_builder, require_runtime
from karotte.hardware import default_hardware, default_runtime
from karotte.task_image import (
    ContainerfileMismatchError,
    build_args,
    check_containerfile,
    image_tag,
)

if TYPE_CHECKING:
    from karotte.task import Task


def _load_tasks(task_ids: list[str] | None) -> "list[Task]":
    """The environment's tasks, or those named by ``task_ids``, created with a
    placeholder config. Outside an environment there are none."""
    from karotte.load_tasks import load_all_task_classes
    from karotte.schemas.evaluation_run_config import EvaluationRunConfig

    try:
        task_classes = load_all_task_classes()
    except ModuleNotFoundError:
        return []
    config = EvaluationRunConfig(run_id="", task_id="", model="", model_api_key="")
    tasks = [cls(config) for cls in task_classes]
    if task_ids:
        unknown = sorted(set(task_ids) - {t.id for t in tasks})
        if unknown:
            typer.secho(
                f"Unknown task: {', '.join(unknown)}", fg=typer.colors.RED, err=True
            )
            raise typer.Exit(1)
        tasks = [t for t in tasks if t.id in task_ids]
    return tasks


def _default_build_runtime(tasks: "list[Task]") -> Runtime:
    """The default runtime `run` would pick for the environment's tasks, so
    `build` fills the image store `run` reads. `run` picks per task, by its
    hardware: tasks that would pick different runtimes need ``--runtime``.
    Outside an environment, the plugins' default hardware decides."""
    by_runtime: dict[Runtime, list[str]] = {}
    for task in tasks:
        by_runtime.setdefault(default_runtime(task.required_hardware), []).append(
            task.id
        )
    if not by_runtime:
        return default_runtime(default_hardware())
    if len(by_runtime) > 1:
        detail = "; ".join(
            f"{r}: {', '.join(ids)}" for r, ids in sorted(by_runtime.items())
        )
        typer.secho(
            f"The tasks run under different default runtimes ({detail}). Pass --runtime.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(1)
    return next(iter(by_runtime))


def build(
    runtime: Annotated[
        Runtime | None,
        typer.Option(
            metavar="NAME",
            help=f"Container runtime: {', '.join(get_args(Runtime))}. Default: the"
            + " OS's VM runtime (`apple-container` on macOS; `firecracker` on Linux, which"
            + " builds with docker); docker elsewhere.",
        ),
    ] = None,
    tag: Annotated[
        str | None,
        typer.Option(
            help="Tag to assign to the built container image. Default: `karotte`, or"
            + " one tag per base image for tasks that bring their own. Only allowed"
            + " when one image is built."
        ),
    ] = None,
    task: Annotated[
        list[str] | None,
        typer.Option(
            help="Build only the image this task runs in. Repeatable. Default: every"
            + " image the environment's tasks need."
        ),
    ] = None,
    build_context: Annotated[
        str, typer.Option(help="Path to the build context.")
    ] = ".",
    cache_from: Annotated[
        list[str] | None,
        typer.Option(help="Cache source passed to the container runtime."),
    ] = None,
    cache_to: Annotated[
        list[str] | None,
        typer.Option(help="Cache destination passed to the container runtime."),
    ] = None,
    build_secret: Annotated[
        list[str] | None,
        typer.Option(
            help="name=path of a file the Containerfile can mount as a build secret "
            + "with type=secret and id=<name>. Repeatable."
        ),
    ] = None,
) -> None:
    """Build the container images for the environment's tasks."""
    tasks = _load_tasks(task)
    containerfile = Path(build_context) / "Containerfile"
    if containerfile.is_file():
        try:
            check_containerfile(containerfile, ((t.id, t.base_image) for t in tasks))
        except ContainerfileMismatchError as e:
            typer.secho(str(e), fg=typer.colors.RED, err=True)
            raise typer.Exit(1) from e
    # One image per base; tasks without one share the environment's image.
    bases = list({image_tag(t.base_image): t.base_image for t in tasks}.values()) or [
        None
    ]
    if tag is not None and len(bases) > 1:
        typer.secho(
            f"--tag names one image, but the tasks need {len(bases)}; pass --task to build one.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(1)
    if runtime is None:
        runtime = _default_build_runtime(tasks)
        logger.info(f"Runtime: {runtime} (default; pass --runtime to change)")
    require_runtime(runtime)
    require_builder(runtime)
    if runtime == "apple-container":
        from karotte.apple_container import validate_container_runtime

        validate_container_runtime()
    for base in bases:
        build_container(
            runtime,
            build_context,
            tag or image_tag(base),
            cache_from=cache_from,
            cache_to=cache_to,
            build_secrets=build_secret or (),
            build_args=build_args(base),
            platform=base and base.platform,
        )
