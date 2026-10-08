"""The environment a task's processes run in, as an unprivileged user.

Harbor runs an agent as root, with the base image's environment and root's
home. Here the student and the grader are users of their own, so they get the
base's environment from the snapshot taken before karotte touched it, pointed at
the toolchains the base keeps in root's home, and a home of their own to write.
"""

import os
import shutil
from collections.abc import Mapping
from pathlib import Path

TASK_ENV = Path("/karotte/task_env")
"""The base image's environment, NUL-separated, written as the image is built."""

BASE_HOME = Path("/root")

_NOT_THE_TASKS = frozenset(
    {"HOME", "HOSTNAME", "USER", "LOGNAME", "TMPDIR", "PWD", "OLDPWD"}
)

_TOOLCHAIN_HOMES = {
    "CARGO_HOME": ".cargo",
    "RUSTUP_HOME": ".rustup",
    "RYE_HOME": ".rye",
    "BUN_INSTALL": ".bun",
}
"""Toolchains that find themselves through a variable defaulting to root's
home. They are used from where the base installed them, read-only."""


def task_env(path: Path = TASK_ENV) -> dict[str, str]:
    """The base image's own environment, less who it ran as."""
    env: dict[str, str] = {}
    for entry in path.read_bytes().split(b"\0"):
        name, sep, value = entry.decode(errors="surrogateescape").partition("=")
        if sep and name not in _NOT_THE_TASKS:
            env[name] = value
    return env


def session_env(
    home: str,
    user: str,
    *,
    base: Mapping[str, str],
    extra: Mapping[str, str] | None = None,
    base_home: Path = BASE_HOME,
) -> dict[str, str]:
    """The environment of a task process run as ``user`` with ``home``.

    Go's module cache stays where the base filled it, read-only; ``GOPATH``
    moves to the user's home, since tests may write to ``$GOPATH/bin`` as
    root could. :func:`seed_home` gives that directory the base's tools.
    """
    env = dict(base)
    gopath = Path(env.get("GOPATH") or base_home / "go")
    if (gopath / "pkg" / "mod").is_dir():
        env.setdefault("GOMODCACHE", str(gopath / "pkg" / "mod"))
    if gopath.is_dir():
        env["GOPATH"] = f"{home}/go"
    for var, name in _TOOLCHAIN_HOMES.items():
        if var not in env and (base_home / name).is_dir():
            env[var] = str(base_home / name)
    env.update(extra or {})
    env.update({"HOME": home, "USER": user, "LOGNAME": user})
    return env


def seed_home(
    home: Path, uid: int, *, base: Mapping[str, str], base_home: Path = BASE_HOME
) -> None:
    """Give a session's home what the base's home had that tools expect to
    write next to: ``$GOPATH/bin`` with the base's Go tools in it."""
    gopath = Path(base.get("GOPATH") or base_home / "go")
    if not (gopath / "bin").is_dir():
        return
    target = home / "go" / "bin"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(gopath / "bin", target, symlinks=True, dirs_exist_ok=True)
    for path in (home / "go", *(home / "go").rglob("*")):
        os.lchown(path, uid, uid)
