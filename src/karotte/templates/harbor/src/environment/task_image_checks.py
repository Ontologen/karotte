import os
import pwd
import stat
from pathlib import Path

# What makes an interpreter the harness starts load code from elsewhere; the
# Containerfile empties them for the harness, and the task's processes get the
# base's values back from /karotte/task_env.
_HIJACK_VARS = (
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONSTARTUP",
    "PYTHONUSERBASE",
    "VIRTUAL_ENV",
    "BASH_ENV",
    "ENV",
    "NODE_OPTIONS",
    "PERL5LIB",
    "PERL5OPT",
    "RUBYOPT",
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "LD_AUDIT",
    "PYTHONPYCACHEPREFIX",
    "PYTHONPLATLIBDIR",
    "GCONV_PATH",
)

# PYTHON* and LD_* variables that only describe, or only steer output.
_HARMLESS = frozenset(
    {
        "PYTHONUNBUFFERED",
        "PYTHONSAFEPATH",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONIOENCODING",
        "PYTHON_VERSION",
        "PYTHON_SHA256",
        "PYTHON_PIP_VERSION",
        "PYTHON_SETUPTOOLS_VERSION",
        "PYTHON_GET_PIP_URL",
        "PYTHON_GET_PIP_SHA256",
    }
)


def check():
    """Check that an image built on a task's base keeps the harness private.

    Named `check` because `environment.check_permissions` discovers and runs
    every `environment.*_checks` module's `check()` during `karotte check`.
    Root's home is /karotte here, and /root is the base's, readable by the
    student: everything the harness keeps relative to its home has to land in
    /karotte, and nothing the base set may steer the harness into the task's
    files.
    """
    home = os.environ.get("HOME")
    assert home == "/karotte" == os.environ.get("ROOT_WORKDIR"), (
        f"HOME={home!r} and ROOT_WORKDIR={os.environ.get('ROOT_WORKDIR')!r} must both be /karotte"
    )
    assert pwd.getpwuid(0).pw_dir == "/karotte", "root's passwd home must be /karotte"
    _assert_private("/karotte")

    # `karotte check` runs with the harness's own venv activated.
    leaked = {
        var: value
        for var in _HIJACK_VARS
        if (value := os.environ.get(var))
        and not (var == "VIRTUAL_ENV" and value == f"{home}/.venv")
    }
    assert not leaked, f"the harness's environment sets {leaked}; they must be empty"

    task_path = os.environ.get("KAROTTE_TASK_PATH", "")
    under_karotte = [p for p in task_path.split(":") if p.startswith("/karotte")]
    assert not under_karotte, f"the task's PATH reaches into /karotte: {under_karotte}"

    _assert_private("/karotte/task_env", directory=False)

    # Variables that can change what an interpreter or the loader loads, beyond
    # the ones emptied above: refuse rather than guess what they do here.
    unexpected = {
        var: value
        for var, value in os.environ.items()
        if value
        and (var.startswith(("PYTHON", "LD_")) or var == "GCONV_PATH")
        and var not in _HARMLESS
    }
    assert not unexpected, f"the image sets {unexpected} for the harness; empty them"

    workdir = os.environ.get("KAROTTE_TASK_WORKDIR")
    if workdir:
        _assert_private("/karotte/pristine")
        student_uid = int(os.environ["KAROTTE_DEMOTE_ID"])
        owner = Path(workdir).lstat().st_uid
        assert owner == student_uid, (
            f"{workdir} is owned by uid {owner}, not the student"
        )


def _assert_private(path: str, directory: bool = True) -> None:
    st = os.lstat(path)
    kind = stat.S_ISDIR(st.st_mode) if directory else stat.S_ISREG(st.st_mode)
    assert kind and st.st_uid == 0 and not st.st_mode & 0o077, (
        f"{path} must be root's alone, not uid {st.st_uid} mode {stat.filemode(st.st_mode)}"
    )
