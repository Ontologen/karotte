"""The tasks `karotte harbor import` accepted, one class per task in the lock.

Edit harbor.toml at the environment's root to choose sources and tasks, then
run `uv run karotte harbor import` and commit the lock it writes here.
"""

from pathlib import Path

from karotte import EvaluationRunConfig
from karotte.harbor.task import register_harbor_tasks

from environment.paths import ROOT_DATA_DIR
from environment.system_prompts import get_system_prompt


def _system_prompt(config: EvaluationRunConfig) -> str:
    return get_system_prompt(config.model, config.extra_config)


register_harbor_tasks(
    globals(),
    lock_dir=Path(__file__).parent,
    data_dir=ROOT_DATA_DIR / "harbor",
    system_prompt=_system_prompt,
)
