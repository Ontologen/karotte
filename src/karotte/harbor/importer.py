"""Import Harbor tasks into an environment made from the `harbor` template.

``harbor.toml`` names the sources: a git repository at a pinned commit, or a
directory of the environment's own. The import reads every task in them
strictly (see :mod:`karotte.harbor.spec`), pins each image to its digest, and
writes:

- ``src/environment/tasks/harbor/harbor.lock.json``: what was accepted and
  pinned, or why a task was refused. Commit it: the image builds and the task
  list follow it, and a re-import shows what changed upstream as its diff.
- ``root_data/harbor/<task id>/``: each task's instruction and tests, root-only
  in the image. The reference solutions stay outside the environment, in the
  source's clone, and only validation mounts them.
"""

import hashlib
import json
import os
import shutil
import subprocess
import tomllib
from dataclasses import asdict
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, model_validator

from karotte.harbor import LOCK_FILE, registry
from karotte.harbor.spec import (
    TEST_FILES,
    UnsupportedHarborTask,
    load_task_dir,
    split_test_script,
)

CONFIG_FILE = "harbor.toml"
LOCK_DIR = Path("src/environment/tasks/harbor")
DATA_DIR = Path("root_data/harbor")
DEFAULT_WORKDIR = "/app"


class Source(BaseModel):
    """One ``[[source]]`` of ``harbor.toml``."""

    model_config = ConfigDict(extra="forbid")  # pyright: ignore[reportUnannotatedClassAttribute]

    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    """Prefixes the ids of its tasks."""
    repo: str | None = None
    commit: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    local: str | None = None
    """A directory instead of a repository: relative to the environment, or absolute."""
    path: str = "."
    """Where the task directories are, in the repository or directory."""
    include: list[str] = Field(default_factory=lambda: ["*"])
    exclude: list[str] = Field(default_factory=list)
    pin_images: bool = True
    """Off for images that exist only locally, such as a test fixture's."""
    workdir: str = DEFAULT_WORKDIR
    """The task's workdir when its image can't be read from a registry."""

    @model_validator(mode="after")
    def _one_kind(self) -> "Source":
        if (self.repo is None) == (self.local is None):
            raise ValueError(
                "a source names either a repo (with a commit) or a local directory"
            )
        if self.repo is not None and self.commit is None:
            raise ValueError(f"source {self.name!r} needs the full commit hash to pin")
        return self


def import_tasks(env_dir: Path, *, from_lock: bool = False) -> dict[str, Any]:
    """Import every source's tasks into ``env_dir``; returns the lock.

    ``from_lock`` re-creates the data of a committed lock instead, reusing its
    image pins: what ``setup_data.py`` does on a fresh checkout.
    """
    lock_path = env_dir / LOCK_DIR / LOCK_FILE
    old_lock: dict[str, Any] = (
        json.loads(lock_path.read_text()) if lock_path.is_file() else {"tasks": {}}
    )
    if from_lock:
        sources = [Source.model_validate(s) for s in old_lock.get("sources", [])]
    else:
        config = tomllib.loads((env_dir / CONFIG_FILE).read_text())
        sources = [Source.model_validate(s) for s in config.get("source", [])]

    data_root = env_dir / DATA_DIR
    if data_root.exists():
        shutil.rmtree(data_root)
    tasks: dict[str, Any] = {}
    for source in sources:
        root = _checkout(source, env_dir) / source.path
        for task_dir in sorted(
            p for p in root.iterdir() if (p / "task.toml").is_file()
        ):
            if not _selected(task_dir.name, source):
                continue
            task_id = f"{source.name}-{task_dir.name}"
            pinned = old_lock["tasks"].get(task_id) if from_lock else None
            tasks[task_id] = _import_task(task_id, task_dir, source, data_root, pinned)

    lock = {
        "sources": [s.model_dump(exclude_defaults=True) for s in sources],
        "tasks": tasks,
    }
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(json.dumps(lock, indent=1, sort_keys=True) + "\n")
    refused = {i: t["status"] for i, t in tasks.items() if t["status"] != "ok"}
    logger.info(f"Imported {len(tasks) - len(refused)} tasks, refused {len(refused)}")
    for task_id, reason in refused.items():
        logger.warning(f"Refused {task_id}: {reason}")
    return lock


def solution_dir(source: Source, dir_name: str) -> Path:
    """Where a task's reference solution is, outside the environment."""
    if source.local is not None:
        raise ValueError("a local source's solutions are in the environment itself")
    return _clone_dir(source) / source.path / dir_name / "solution"


def _import_task(
    task_id: str,
    task_dir: Path,
    source: Source,
    data_root: Path,
    pinned: dict[str, Any] | None,
) -> dict[str, Any]:
    head: dict[str, Any] = {
        "id": task_id,
        "dir_name": task_dir.name,
        "source": {
            "name": source.name,
            **(
                {"repo": source.repo, "commit": source.commit}
                if source.repo
                else {"local": source.local or ""}
            ),
        },
    }
    try:
        spec = load_task_dir(task_dir)
        image = (
            pinned["image"]
            if pinned and pinned.get("status") == "ok"
            else _image(spec.image, source)
        )
        if spec.frame == "deepswe-1.1" and image["workdir"] != "/app":
            # Its test.sh works in /app whatever the image's workdir.
            raise UnsupportedHarborTask(
                f"DeepSWE's verifier works in /app, but the image's workdir is {image['workdir']}"
            )
    except (UnsupportedHarborTask, registry.RegistryError) as e:
        return {**head, "status": str(e)}

    data = data_root / task_id
    (data / "tests").mkdir(parents=True)
    shutil.copyfile(task_dir / "instruction.md", data / "instruction.md")
    for name in TEST_FILES:
        shutil.copyfile(task_dir / "tests" / name, data / "tests" / name)
    (data / "tests" / "phase_a.sh").write_text(
        split_test_script((task_dir / "tests" / "test.sh").read_text())
    )
    fields = asdict(spec)
    for name in ("dir_name", "image", "artifacts"):
        fields.pop(name)
    # As JSON has it, so the lock returned is the lock written.
    fields["collect"] = [list(hook) for hook in spec.collect]
    return {
        **head,
        **fields,
        "image": image,
        "content_sha256": _content_hash(data),
        "status": "ok",
    }


def _image(ref: str, source: Source) -> dict[str, Any]:
    if not source.pin_images:
        return {"ref": ref, "platform": None, "workdir": source.workdir}
    resolved = registry.resolve(ref)
    return {
        "ref": resolved.ref,
        "platform": resolved.platform,
        "workdir": resolved.workdir or source.workdir,
    }


def _content_hash(data: Path) -> str:
    """What validation checked: the instruction and the tests, as imported."""
    digest = hashlib.sha256()
    for path in sorted(p for p in data.rglob("*") if p.is_file()):
        digest.update(str(path.relative_to(data)).encode() + b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _selected(name: str, source: Source) -> bool:
    return any(fnmatch(name, p) for p in source.include) and not any(
        fnmatch(name, p) for p in source.exclude
    )


def _checkout(source: Source, env_dir: Path) -> Path:
    if source.local is not None:
        return env_dir / source.local
    assert source.repo is not None and source.commit is not None
    clone = _clone_dir(source)
    if not (clone / ".git").is_dir():
        clone.parent.mkdir(parents=True, exist_ok=True)
        _git(
            "clone",
            "--quiet",
            "--filter=blob:none",
            "--no-checkout",
            source.repo,
            str(clone),
        )
    _git("-C", str(clone), "checkout", "--quiet", "--detach", source.commit)
    head = _git("-C", str(clone), "rev-parse", "HEAD").strip()
    if head != source.commit:
        raise RuntimeError(f"{source.repo} checked out at {head}, not {source.commit}")
    return clone


def _clone_dir(source: Source) -> Path:
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return cache / "karotte" / "harbor" / f"{source.name}-{(source.commit or '')[:12]}"


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True
    ).stdout
