from pathlib import Path
from typing import Annotated

import typer

from karotte import Runtime

app = typer.Typer(help="Run tasks from Harbor-format datasets, such as DeepSWE.")


@app.command("import")
def import_(
    env: Annotated[
        str, typer.Option(help="The environment, made from the `harbor` template.")
    ] = ".",
    from_lock: Annotated[
        bool,
        typer.Option(
            help="Re-create the data of the committed lock, keeping its image pins,"
            + " instead of reading harbor.toml and the registries anew."
        ),
    ] = False,
) -> None:
    """Import the tasks harbor.toml names: pin their images, write the lock and the tasks' data."""
    from karotte.harbor.importer import import_tasks

    lock = import_tasks(Path(env), from_lock=from_lock)
    accepted = sum(1 for task in lock["tasks"].values() if task["status"] == "ok")
    typer.echo(f"{accepted} of {len(lock['tasks'])} tasks imported.")


@app.command("validate")
def validate(
    tasks: Annotated[
        list[str] | None,
        typer.Argument(help="Globs over task ids. Default: every imported task."),
    ] = None,
    env: Annotated[str, typer.Option(help="The environment.")] = ".",
    runtime: Annotated[
        Runtime | None,
        typer.Option(metavar="NAME", help="The runtime the tasks will run on."),
    ] = None,
    concurrency: Annotated[int, typer.Option(min=1, help="Runs at once.")] = 1,
    out: Annotated[
        str, typer.Option(help="Where the runs go.")
    ] = "out/harbor-validation",
    prune_images: Annotated[
        bool, typer.Option(help="Remove each task's image once it's validated.")
    ] = False,
) -> None:
    """Run each task's reference solution (must score 1) and an empty submission
    (must score 0) through the real grading path; record which tasks pass."""
    from fnmatch import fnmatch

    from karotte.cli.suite import execute
    from karotte.harbor.task import HarborTask
    from karotte.harbor.validate import plan_validation, record_validation
    from karotte.load_tasks import load_all_task_classes, require_environment

    require_environment()
    task_ids = [
        cls.id
        for cls in load_all_task_classes()
        if issubclass(cls, HarborTask)
        and any(fnmatch(cls.id, p) for p in tasks or ["*"])
    ]
    if not task_ids:
        typer.secho("No imported tasks match.", fg=typer.colors.RED, err=True)
        raise typer.Exit(2)
    planned = plan_validation(Path(env), task_ids)
    results = execute(planned, Path(out), runtime, concurrency, 0, prune_images, env)
    checked = record_validation(Path(env), results, str(runtime or "default"))
    for task_id, result in checked.items():
        oracle, nop = result["oracle"], result["nop"]
        mark = "ok  " if result["passed"] else "FAIL"
        typer.echo(
            f"{mark} {task_id}: oracle {oracle['score']} ({oracle['verifier_status'] or oracle['outcome']}),"
            + f" nop {nop['score']} ({nop['verifier_status'] or nop['outcome']})"
        )
    failed = [t for t, r in checked.items() if not r["passed"]]
    typer.echo(f"{len(checked) - len(failed)} of {len(checked)} tasks validated.")
    raise typer.Exit(1 if failed else 0)
