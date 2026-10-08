"""Schema for the image a task brings to build on."""

from pathlib import PurePosixPath
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, field_validator


class BaseImage(BaseModel):
    """An image a task's sandbox is built on instead of the environment's own base.

    Karotte adds its harness, the student user and its checks on top, so the
    task keeps the base's toolchains and files.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    ref: str
    """What the Containerfile's ``FROM`` pulls. Pin it by digest
    (``repo:tag@sha256:...``) so a pushed tag can't change the task."""

    platform: str | None = None
    """The base's platform, e.g. ``linux/amd64``, for bases built for one
    architecture only."""

    workdir: str | None = None
    """The directory the task's work happens in, e.g. ``/app``. The student owns
    it, and a root-only copy of it as the base had it is kept for grading."""

    @field_validator("ref")
    @classmethod
    def _ref_is_set(cls, ref: str) -> str:
        if not ref.strip() or any(c.isspace() for c in ref):
            raise ValueError(f"not an image reference: {ref!r}")
        return ref

    @field_validator("workdir")
    @classmethod
    def _workdir_is_absolute(cls, workdir: str | None) -> str | None:
        if workdir is not None and not PurePosixPath(workdir).is_absolute():
            raise ValueError(f"workdir must be an absolute path, got {workdir!r}")
        return workdir

    @property
    def pinned(self) -> bool:
        """Whether ``ref`` names a digest, so the base can't change under the task."""
        return "@sha256:" in self.ref
