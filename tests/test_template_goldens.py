"""The Containerfiles existing environments are built from, as `create-env`
renders them. Templates override the default's blocks to build on other bases;
these goldens catch an edit that changes what the default itself renders.

Regenerate after a deliberate change with `UPDATE_GOLDEN=1 pytest tests/test_template_goldens.py`
and review the diff.
"""

import os
from pathlib import Path

import pytest

from karotte.create_env import template_environment
from karotte.templates import TEMPLATES_DIR, discover_templates

GOLDEN_DIR = Path(__file__).parent / "resources" / "golden"


@pytest.mark.parametrize(
    ("template", "stack"),
    [
        ("default/Containerfile", ["default"]),
        ("language-toolchains/Containerfile", ["default", "language-toolchains"]),
        ("harbor/Containerfile", ["default", "harbor"]),
    ],
)
def test_containerfile_renders_as_before(template: str, stack: list[str]):
    env = template_environment(discover_templates(TEMPLATES_DIR))
    rendered = env.get_template(template).render(env_name="golden", templates=stack)
    golden = GOLDEN_DIR / f"{template.replace('/', '__')}.txt"
    if os.environ.get("UPDATE_GOLDEN"):
        golden.write_text(rendered)
    assert rendered == golden.read_text()
