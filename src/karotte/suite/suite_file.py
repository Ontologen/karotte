"""A suite file: which tasks, which model setups, how many samples each.

```toml
name = "deepswe-baseline"
samples = 3
tasks = ["deepswe-*"]
exclude = ["deepswe-wazero-*"]

[defaults]                  # any run config field, for every run
step_time_limit_seconds = 10800
on_step_time_limit = "score"

[[models]]                  # one model setup per entry; `label` names it
label = "opus"
model = "anthropic/claude-opus-5-5"
reasoning_effort = "max"
```
"""

import json
import tomllib
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from karotte.suite.runner import PlannedRun

_PER_ATTEMPT = {"run_id", "task_id", "transcript_file"}


class ModelSetup(BaseModel):
    model_config = ConfigDict(extra="allow")  # pyright: ignore[reportUnannotatedClassAttribute]

    label: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    mounts: list[str] = Field(default_factory=list)


class SuiteFile(BaseModel):
    model_config = ConfigDict(extra="forbid")  # pyright: ignore[reportUnannotatedClassAttribute]

    name: str
    samples: int = Field(default=1, ge=1)
    tasks: list[str] = Field(default_factory=lambda: ["*"])
    exclude: list[str] = Field(default_factory=list)
    defaults: dict[str, Any] = Field(default_factory=dict)
    models: list[ModelSetup] = Field(min_length=1)


def load_suite(path: Path, task_ids: list[str]) -> list[PlannedRun]:
    """The runs ``path`` describes, over the environment's ``task_ids``.

    Each model setup's config is the defaults with its own fields on top, read
    as ``karotte run --config`` reads a config, API keys included.
    """
    from karotte.run_config_preprocessors import apply_run_config_preprocessors
    from karotte.run_helpers import parse_config

    suite = SuiteFile.model_validate(tomllib.loads(path.read_text()))
    labels = [m.label for m in suite.models]
    if len(set(labels)) != len(labels):
        raise ValueError(f"model labels must be unique: {labels}")
    selected = [
        t
        for t in task_ids
        if any(fnmatch(t, p) for p in suite.tasks)
        and not any(fnmatch(t, p) for p in suite.exclude)
    ]
    if not selected:
        raise ValueError(f"{path} selects none of the environment's tasks")
    clashes = _PER_ATTEMPT & set(suite.defaults)
    if clashes:
        raise ValueError(
            f"{', '.join(sorted(clashes))} are set per run, not in [defaults]"
        )

    planned: list[PlannedRun] = []
    for setup in suite.models:
        fields = {**suite.defaults, **setup.model_dump(exclude={"label", "mounts"})}
        for task_id in selected:
            raw = {**fields, "run_id": "planned", "task_id": task_id}
            config = apply_run_config_preprocessors(parse_config(json.dumps(raw)))
            for sample in range(suite.samples):
                planned.append(
                    PlannedRun(
                        task_id, setup.label, sample, config, tuple(setup.mounts)
                    )
                )
    return planned
