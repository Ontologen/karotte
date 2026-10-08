"""Which image a task runs in, and where the harness lives inside it.

A task without a base image runs in the environment's own image, ``karotte``. A
task with one runs in an image built from the environment's Containerfile on top
of that base, tagged by the base, so tasks that share a base share an image.

Those images keep the harness's private files in ``/karotte`` instead of
``/root``: ``/root`` stays the base's home, where its toolchains live.
"""

import hashlib
import os
import re
import stat
from collections.abc import Iterable, Mapping
from pathlib import Path

from karotte.schemas.base_image import BaseImage

DEFAULT_TAG = "karotte"

DEFAULT_HARNESS_HOME = "/root"
BASE_IMAGE_HARNESS_HOME = "/karotte"
"""Root's home, and the harness's, in an image built on a task's base."""

BASE_IMAGE_ARG = "BASE_IMAGE"
TASK_WORKDIR_ARG = "TASK_WORKDIR"

_DECLARES_BASE_IMAGE = re.compile(rf"^\s*ARG\s+{BASE_IMAGE_ARG}\b", re.MULTILINE)


def image_tag(base: BaseImage | None) -> str:
    """The tag of the image a task with ``base`` runs in."""
    if base is None:
        return DEFAULT_TAG
    digest = hashlib.sha256(base.model_dump_json().encode()).hexdigest()
    return f"{DEFAULT_TAG}-{digest[:16]}"


def image_ref(tag: str, engine: str) -> str:
    """``tag`` as the engine names a locally built image."""
    return f"localhost/{tag}" if engine == "podman" else tag


def build_args(base: BaseImage | None) -> dict[str, str]:
    """The ``--build-arg`` values for the image a task with ``base`` runs in."""
    if base is None:
        return {}
    return {BASE_IMAGE_ARG: base.ref, TASK_WORKDIR_ARG: base.workdir or ""}


def harness_home(base: BaseImage | None) -> str:
    """Root's home in the image: where the harness's venv and private files are."""
    return DEFAULT_HARNESS_HOME if base is None else BASE_IMAGE_HARNESS_HOME


def karotte_bin(base: BaseImage | None) -> str:
    """The command that starts the harness in the image."""
    return f"{harness_home(base)}/.venv/bin/karotte"


def dev_target(base: BaseImage | None) -> str:
    """Where ``--dev`` mounts the environment's source in the image."""
    return f"{harness_home(base)}/.venv/lib/python3.12/site-packages/environment/"


class ContainerfileMismatchError(Exception):
    """The Containerfile and the tasks disagree on whether tasks bring a base image."""


def check_containerfile(
    containerfile: Path, tasks: Iterable[tuple[str, BaseImage | None]]
) -> None:
    """Raise unless every task brings a base image exactly when the Containerfile
    takes one as ``ARG BASE_IMAGE``.

    Otherwise a base would be silently ignored and the task built on the
    environment's own base, or the build would fail on an empty ``FROM``.
    """
    declares = bool(_DECLARES_BASE_IMAGE.search(containerfile.read_text()))
    tasks = list(tasks)
    if declares:
        missing = sorted(task_id for task_id, base in tasks if base is None)
        if missing:
            raise ContainerfileMismatchError(
                f"{containerfile} builds on a task's base image, but these tasks name none: "
                + ", ".join(missing)
            )
    else:
        extra = sorted(task_id for task_id, base in tasks if base is not None)
        if extra:
            raise ContainerfileMismatchError(
                f"These tasks name a base image, but {containerfile} has no "
                + f"`ARG {BASE_IMAGE_ARG}` to build on it: {', '.join(extra)}"
            )


def check_harness_home(environ: Mapping[str, str] = os.environ) -> None:
    """Raise unless the harness's home is root's private directory, in an image
    whose ``ROOT_WORKDIR`` is not ``/root``.

    Karotte keeps its private files relative to ``HOME`` (the protected store,
    submissions, tool configs), and some are created with default modes. In
    such an image ``/root`` is the base's home and readable by the student, so
    ``HOME`` must be ``ROOT_WORKDIR``, owned by root and closed to everyone else.
    """
    root_workdir = environ.get("ROOT_WORKDIR")
    if root_workdir in (None, "", DEFAULT_HARNESS_HOME):
        return
    home = environ.get("HOME")
    if home != root_workdir:
        raise RuntimeError(
            f"HOME is {home!r}, but this image keeps the harness's private files in {root_workdir!r}"
        )
    st = os.lstat(root_workdir)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != 0 or st.st_mode & 0o077:
        raise RuntimeError(
            f"{root_workdir} must be a directory owned by root with mode 0700, "
            + f"not uid {st.st_uid} mode {stat.filemode(st.st_mode)}"
        )
