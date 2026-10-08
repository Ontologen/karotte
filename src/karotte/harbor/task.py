"""Karotte tasks made from an imported Harbor dataset's lock file.

One task class per task the import accepted, registered into a task package's
namespace the way the default template's task suites are. Everything a task
reads on the host comes from the lock; its instruction and tests are root-only
data the import put in the environment's ``root_data``.
"""

import json
import os
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, ClassVar

from loguru import logger
from pydantic import BaseModel

from karotte.harbor import LOCK_FILE, VALIDATION_FILE
from karotte.harbor.session import BASE_HOME, seed_home, session_env, task_env
from karotte.harbor.verifier import HarborGrading, HarborVerifierJudge
from karotte.judges.judge import Judge
from karotte.schemas.base_image import BaseImage
from karotte.schemas.evaluation_run_config import EvaluationRunConfig
from karotte.step import Step
from karotte.task import Task
from karotte.tool_base import ToolConfigWriter
from karotte.tools.bash import BashConfig

SOLUTION_MOUNT = Path("/solution")

_NEUTRAL_GIT_IDENTITY = "[user]\n\tname = Student\n\temail = student@karotte.invalid\n"


class LockedImage(BaseModel):
    ref: str
    platform: str | None = None
    workdir: str


class LockedTask(BaseModel):
    """A task as the import accepted it: pins and what grading needs."""

    id: str
    name: str
    source: dict[str, str]
    dir_name: str
    image: LockedImage
    cpus: int
    memory_mb: int
    storage_mb: int
    agent_timeout_sec: float
    verifier_timeout_sec: float
    verifier_env: dict[str, str]
    collect: list[tuple[str, float]]
    frame: str
    require_cmds: list[str]
    metadata: dict[str, Any]
    status: str
    """``ok``, or why the import refused the task."""


def hardware_name(task: LockedTask) -> str:
    """What a task's sandbox holds, as :mod:`karotte.harbor.plugins` reads it back."""
    return f"harbor-c{task.cpus}-m{task.memory_mb}-d{task.storage_mb}"


class HarborTask(Task):
    """A task imported from a Harbor dataset. Subclasses set the class
    attributes from one lock entry."""

    locked: ClassVar[LockedTask]
    data_dir: ClassVar[Path]
    """The task's instruction, tests and ``phase_a.sh``, in root-only data."""
    validated: ClassVar[bool | None]
    """Whether the oracle and empty submissions scored as they must; ``None``
    until the task has been validated."""
    make_system_prompt: ClassVar[Callable[[EvaluationRunConfig], str | None] | None] = (
        None
    )
    tool_names: ClassVar[tuple[str, ...]] = ("bash",)

    @property
    def system_prompt(self) -> str | None:
        make = type(self).make_system_prompt
        return make(self.config) if make is not None else None

    @property
    def tools(self) -> list[str]:
        return list(self.tool_names)

    @property
    def required_hardware(self) -> str | None:
        return hardware_name(self.locked)

    @property
    def base_image(self) -> BaseImage | None:
        image = self.locked.image
        return BaseImage(ref=image.ref, platform=image.platform, workdir=image.workdir)

    @property
    def steps(self) -> Iterable[Step]:
        return [_HarborStep(self.config, self)]

    def configure_tools(self) -> None:
        """The student's shell starts in the task's workdir, with the base
        image's environment, as Harbor's agent does."""
        home = _student_home()
        env = session_env(str(home), "student", base=task_env())
        ToolConfigWriter().write(
            "bash", BashConfig(cwd=self.locked.image.workdir, env=env, python_venv=None)
        )

    def pre_hook(self) -> dict[str, Any]:
        self._check_run_config()
        home = _student_home()
        student_uid = _student_uid()
        seed_home(home, student_uid, base=task_env())
        gitconfig = home / ".gitconfig"
        source = BASE_HOME / ".gitconfig"
        gitconfig.write_text(
            source.read_text() if source.is_file() else _NEUTRAL_GIT_IDENTITY
        )
        os.lchown(gitconfig, student_uid, student_uid)
        locked = self.locked
        return {
            "harbor_task": locked.name,
            "harbor_source": locked.source,
            "image": locked.image.ref,
            "verifier_frame": locked.frame,
            "validated": self.validated,
            "agent_timeout_sec": locked.agent_timeout_sec,
            "verifier_timeout_sec": locked.verifier_timeout_sec,
            "metadata": locked.metadata,
        }

    def _check_run_config(self) -> None:
        config = self.config
        validating = config.use_fake_model and (config.extra_config or {}).get(
            "validation"
        ) in ("oracle", "nop")
        if self.validated is False and not validating:
            raise RuntimeError(
                f"{self.id} failed validation (see harbor.validation.json): its reference"
                + " solution or an empty submission didn't score as they must"
            )
        oracle = (config.extra_config or {}).get("validation") == "oracle"
        if SOLUTION_MOUNT.exists() and not (oracle and config.use_fake_model):
            raise RuntimeError(
                f"{SOLUTION_MOUNT} holds the reference solution; it may only be "
                + "mounted for an oracle validation run with the fake model"
            )
        if not config.use_fake_model and (
            config.step_time_limit_seconds is None
            or config.on_step_time_limit != "score"
        ):
            raise RuntimeError(
                f"{self.id} gives the agent {self.locked.agent_timeout_sec:.0f}s and then "
                + "grades its work: set step_time_limit_seconds to that and "
                + 'on_step_time_limit to "score" in the run config'
            )


class _HarborStep(Step):
    def __init__(self, config: EvaluationRunConfig, task: HarborTask) -> None:
        super().__init__(config)
        self.task: HarborTask = task

    @property
    def instructions(self) -> str:
        return (self.task.data_dir / "instruction.md").read_text()

    @property
    def judge(self) -> Judge:
        locked = self.task.locked
        return HarborVerifierJudge(
            HarborGrading(
                task_dir_name=locked.dir_name,
                tests_dir=self.task.data_dir / "tests",
                workdir=locked.image.workdir,
                student_uid=_student_uid(),
                collect=locked.collect,
                verifier_timeout_sec=locked.verifier_timeout_sec,
                verifier_env=locked.verifier_env,
                memory_mb=locked.memory_mb,
                storage_mb=locked.storage_mb,
                require_cmds=locked.require_cmds,
            ),
            self.config,
        )


def register_harbor_tasks(
    namespace: dict[str, Any],
    lock_dir: Path,
    data_dir: Path,
    *,
    system_prompt: Callable[[EvaluationRunConfig], str | None] | None = None,
    tools: Iterable[str] = ("bash",),
) -> None:
    """Add a task class to ``namespace`` for every task in ``lock_dir``'s lock
    file that the import accepted. A task that failed validation is kept, so it
    can be validated again, but refuses any other run.

    ``data_dir`` holds each task's instruction and tests under its id, as the
    import wrote them into the environment's root-only data.
    """
    lock_path = lock_dir / LOCK_FILE
    if not lock_path.is_file():
        logger.warning(f"No {lock_path}; run `karotte harbor import` to import tasks")
        return
    lock = json.loads(lock_path.read_text())
    validation_path = lock_dir / VALIDATION_FILE
    validation: dict[str, Any] = (
        json.loads(validation_path.read_text()) if validation_path.is_file() else {}
    )
    for task_id, raw in sorted(lock["tasks"].items()):
        if raw.get("status") != "ok":
            continue
        result = validation.get(task_id) or {}
        passed: bool | None = result.get("passed")
        if result.get("image") != raw["image"]["ref"] or result.get(
            "content_sha256"
        ) != raw.get("content_sha256"):
            # Validated against an image or tests the lock no longer pins.
            passed = None
        cls = type(
            task_id.replace("-", "_"),
            (HarborTask,),
            {
                "id": task_id,
                "locked": LockedTask.model_validate(raw),
                "data_dir": data_dir / task_id,
                "validated": passed,
                "make_system_prompt": system_prompt,
                "tool_names": tuple(tools),
            },
        )
        namespace[cls.__name__] = cls


def _student_uid() -> int:
    return int(os.environ.get("KAROTTE_DEMOTE_ID", "1000"))


def _student_home() -> Path:
    return Path(os.environ.get("KAROTTE_WORKDIR", "/workdir"))
