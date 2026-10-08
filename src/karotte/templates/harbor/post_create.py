# /// script
# requires-python = ">=3.12"
# ///
"""Post-creation hook for a Harbor environment.

Every task here runs on its own base image, so the default template's example
task, which has none, can't be built here; neither can the student venv, since
the student uses the task image's own toolchains.
"""

import shutil
from pathlib import Path

if __name__ == "__main__":
    shutil.rmtree(Path("src/environment/tasks/example_task"), ignore_errors=True)
    shutil.rmtree(Path("venvs/student"), ignore_errors=True)
