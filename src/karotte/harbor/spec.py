"""The part of the Harbor task format karotte runs, read strictly.

A task outside it is refused with the reason, never run in a way that quietly
differs from what its authors meant. What is supported is what DeepSWE uses:
a prebuilt image, no network, a verifier in a separate environment that grades
a patch the agent's work is collected into, and DeepSWE's verifier frame.
"""

import hashlib
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

RUN_TESTS_START = "# >>> RUN TESTS (task-specific) <<<"
RUN_TESTS_END = "# >>> END RUN TESTS <<<"

# DeepSWE's verifier, shared verbatim by its tasks: `test.sh` around its
# task-specific block, and `grader.py`. Grading splits `test.sh` at the block's
# end and runs `grader.py grade` itself, so it must be exactly this frame. A
# new frame is reviewed and pinned here before its tasks are accepted.
_KNOWN_FRAMES = {
    (
        "268055c6f4f41defcfcf494c8b520a304ad7dbefbc7b8f457a219de5a83d8540",
        "30660f880581f4a63735fc08efd4b64bbda58f336a310e791030e4fd6ca88c0c",
        "47cc9eaadf21e636323c360ec4fa786f0733ec9fd1d21ea5a5717ff9f8c4077c",
    ): "deepswe-1.1",
}

TEST_FILES = ("test.sh", "test.patch", "grader.py", "config.json")

_REQUIRE_CMD = re.compile(r"\brequire_cmd\s+([A-Za-z0-9_.+-]+)")
_TESTS_DOCKERFILE_COPY = re.compile(r"^COPY (\S+) /tests/(\S+)$")


class UnsupportedHarborTask(Exception):
    """The task uses part of the Harbor format karotte doesn't run."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")  # pyright: ignore[reportUnannotatedClassAttribute]


class _Resources(_Strict):
    build_timeout_sec: float | None = None
    cpus: int = Field(ge=1)
    memory_mb: int = Field(ge=1)
    storage_mb: int = Field(ge=1)


class _Environment(_Resources):
    docker_image: str
    os: Literal["linux"] = "linux"
    gpus: Literal[0] = 0
    mcp_servers: list[Any] = Field(default_factory=list, max_length=0)
    env: dict[str, str] = Field(default_factory=dict)


class _Collect(_Strict):
    command: str
    timeout_sec: float = Field(gt=0)


class _Verifier(_Strict):
    network_mode: Literal["no-network"]
    environment_mode: Literal["separate"]
    timeout_sec: float = Field(gt=0)
    env: dict[str, str] = Field(default_factory=dict)
    environment: _Resources
    collect: list[_Collect] = Field(min_length=1)


class _Agent(_Strict):
    network_mode: Literal["no-network"]
    timeout_sec: float = Field(gt=0)


class _TaskInfo(_Strict):
    name: str
    description: str = ""
    authors: list[Any] = Field(default_factory=list)
    keywords: list[Any] = Field(default_factory=list)


class _Solution(_Strict):
    env: dict[str, str] = Field(default_factory=dict)


class _TaskToml(_Strict):
    schema_version: str = Field(pattern=r"^1\.")
    artifacts: list[str] = Field(default_factory=list)
    task: _TaskInfo
    metadata: dict[str, Any] = Field(default_factory=dict)
    environment: _Environment
    agent: _Agent
    verifier: _Verifier
    solution: _Solution = Field(default_factory=_Solution)

    @model_validator(mode="after")
    def _verifier_runs_where_the_agent_did(self) -> "_TaskToml":
        mine = self.verifier.environment.model_dump(exclude={"build_timeout_sec"})
        agents = self.environment.model_dump(include=set(mine))
        if mine != agents:
            raise ValueError(
                f"[verifier.environment] {mine} differs from [environment] {agents}"
            )
        for artifact in self.artifacts:
            if not artifact.startswith("/logs/artifacts/"):
                raise ValueError(f"artifact {artifact!r} is outside /logs/artifacts/")
        return self


@dataclass(frozen=True)
class HarborTaskSpec:
    """A Harbor task karotte can run, as its directory describes it."""

    dir_name: str
    name: str
    image: str
    cpus: int
    memory_mb: int
    storage_mb: int
    agent_timeout_sec: float
    verifier_timeout_sec: float
    verifier_env: dict[str, str]
    collect: list[tuple[str, float]]
    artifacts: list[str]
    metadata: dict[str, Any]
    frame: str
    """Which pinned verifier frame its tests use."""
    require_cmds: list[str] = field(default_factory=list)
    """Tools its tests check for before running, all from the image."""


def load_task_dir(path: Path) -> HarborTaskSpec:
    """Read a Harbor task directory, or raise :class:`UnsupportedHarborTask`."""
    if (path / "steps").exists():
        raise UnsupportedHarborTask("multi-step tasks (a steps/ directory)")
    try:
        raw = tomllib.loads((path / "task.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise UnsupportedHarborTask(f"unreadable task.toml: {e}") from e
    try:
        toml = _TaskToml.model_validate(raw)
    except ValidationError as e:
        raise UnsupportedHarborTask(_describe(e)) from e

    if not (path / "instruction.md").is_file():
        raise UnsupportedHarborTask("no instruction.md")
    tests = path / "tests"
    missing = [name for name in TEST_FILES if not (tests / name).is_file()]
    if missing:
        raise UnsupportedHarborTask(f"tests/ lacks {', '.join(missing)}")
    _check_tests_dockerfile(tests / "Dockerfile", toml.environment.docker_image)
    frame, require_cmds = _check_frame(tests)

    return HarborTaskSpec(
        dir_name=path.name,
        name=toml.task.name,
        image=toml.environment.docker_image,
        cpus=toml.environment.cpus,
        memory_mb=toml.environment.memory_mb,
        storage_mb=toml.environment.storage_mb,
        agent_timeout_sec=toml.agent.timeout_sec,
        verifier_timeout_sec=toml.verifier.timeout_sec,
        verifier_env=toml.verifier.env,
        collect=[(c.command, c.timeout_sec) for c in toml.verifier.collect],
        artifacts=toml.artifacts,
        metadata=toml.metadata,
        frame=frame,
        require_cmds=require_cmds,
    )


def split_test_script(test_sh: str) -> str:
    """The part of ``test.sh`` that prepares the patches and runs the suites,
    up to and including the end of its task-specific block. Grading runs it
    alone, then ``grader.py grade`` in a phase of its own."""
    head, end, _ = test_sh.partition(RUN_TESTS_END)
    if not end:
        raise UnsupportedHarborTask(f"test.sh has no {RUN_TESTS_END!r} line")
    return head + end + "\n"


def _check_tests_dockerfile(dockerfile: Path, image: str) -> None:
    """The verifier image must be the task image plus the test files, so that
    karotte's restore of the workdir with the tests staged is the same thing."""
    lines = [
        line.strip()
        for line in dockerfile.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not lines or lines[0] != f"FROM {image}":
        raise UnsupportedHarborTask(
            "tests/Dockerfile doesn't build on the task's image"
        )
    for line in lines[1:]:
        copy = _TESTS_DOCKERFILE_COPY.match(line)
        if copy and copy.group(1) == copy.group(2) and copy.group(1) in TEST_FILES:
            continue
        if line == "RUN chmod +x /tests/test.sh":
            continue
        raise UnsupportedHarborTask(
            f"tests/Dockerfile does more than copy the tests: {line!r}"
        )


def _check_frame(tests: Path) -> tuple[str, list[str]]:
    test_sh = (tests / "test.sh").read_text()
    head, start, rest = test_sh.partition(RUN_TESTS_START)
    middle, end, tail = rest.partition(RUN_TESTS_END)
    if not start or not end:
        raise UnsupportedHarborTask("test.sh has no task-specific RUN TESTS block")
    key = (
        hashlib.sha256(head.encode()).hexdigest(),
        hashlib.sha256(tail.encode()).hexdigest(),
        hashlib.sha256((tests / "grader.py").read_bytes()).hexdigest(),
    )
    frame = _KNOWN_FRAMES.get(key)
    if frame is None:
        raise UnsupportedHarborTask(
            "test.sh or grader.py is not a verifier frame karotte knows"
        )
    return frame, sorted(set(_REQUIRE_CMD.findall(middle)))


def _describe(error: ValidationError) -> str:
    first = error.errors()[0]
    where = ".".join(str(part) for part in first["loc"]) or "task.toml"
    return f"task.toml {where}: {first['msg']}"
