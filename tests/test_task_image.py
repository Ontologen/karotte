import os
import stat
from pathlib import Path

import pytest
from pydantic import ValidationError

from karotte.schemas.base_image import BaseImage
from karotte.task_image import (
    ContainerfileMismatchError,
    build_args,
    check_containerfile,
    check_harness_home,
    dev_target,
    harness_home,
    image_ref,
    image_tag,
    karotte_bin,
)

BASE = BaseImage(
    ref="registry.example/task:v1@sha256:" + "a" * 64,
    platform="linux/amd64",
    workdir="/app",
)


class TestBaseImage:
    def test_a_digest_pins_it(self):
        assert BASE.pinned
        assert not BaseImage(ref="registry.example/task:v1").pinned

    @pytest.mark.parametrize("ref", ["", "  ", "two words"])
    def test_rejects_a_ref_that_is_not_one(self, ref: str):
        with pytest.raises(ValidationError):
            BaseImage(ref=ref)

    def test_rejects_a_relative_workdir(self):
        with pytest.raises(ValidationError):
            BaseImage(ref="task:v1", workdir="app")


class TestImageNaming:
    def test_a_task_without_a_base_runs_in_the_environment_image(self):
        assert image_tag(None) == "karotte"
        assert build_args(None) == {}

    def test_tasks_sharing_a_base_share_an_image(self):
        assert image_tag(BASE) == image_tag(BASE.model_copy())
        assert image_tag(BASE).startswith("karotte-")
        assert image_tag(BASE) != image_tag(BASE.model_copy(update={"workdir": "/src"}))

    def test_build_args_name_the_base_and_its_workdir(self):
        assert build_args(BASE) == {"BASE_IMAGE": BASE.ref, "TASK_WORKDIR": "/app"}
        assert build_args(BaseImage(ref="task:v1"))["TASK_WORKDIR"] == ""

    def test_podman_names_local_images_under_localhost(self):
        assert image_ref("karotte", "podman") == "localhost/karotte"
        assert image_ref("karotte", "docker") == "karotte"


class TestHarnessPaths:
    def test_the_environment_image_keeps_the_harness_in_root(self):
        assert harness_home(None) == "/root"
        assert karotte_bin(None) == "/root/.venv/bin/karotte"
        assert (
            dev_target(None) == "/root/.venv/lib/python3.12/site-packages/environment/"
        )

    def test_an_image_on_a_task_base_keeps_it_in_karotte(self):
        assert harness_home(BASE) == "/karotte"
        assert karotte_bin(BASE) == "/karotte/.venv/bin/karotte"
        assert dev_target(BASE).startswith("/karotte/.venv/")


class TestCheckContainerfile:
    @pytest.fixture
    def with_base_arg(self, tmp_path: Path) -> Path:
        path = tmp_path / "Containerfile"
        path.write_text("ARG BASE_IMAGE\nFROM ${BASE_IMAGE}\n")
        return path

    @pytest.fixture
    def without_base_arg(self, tmp_path: Path) -> Path:
        path = tmp_path / "Containerfile"
        path.write_text("FROM docker.io/library/amazonlinux:2023\n")
        return path

    def test_tasks_with_bases_build_on_a_containerfile_that_takes_one(
        self, with_base_arg: Path
    ):
        check_containerfile(with_base_arg, [("a", BASE)])

    def test_tasks_without_bases_build_on_one_that_takes_none(
        self, without_base_arg: Path
    ):
        check_containerfile(without_base_arg, [("a", None)])

    def test_a_task_without_a_base_is_refused_where_one_is_taken(
        self, with_base_arg: Path
    ):
        with pytest.raises(ContainerfileMismatchError, match="name none: b"):
            check_containerfile(with_base_arg, [("a", BASE), ("b", None)])

    def test_a_base_is_never_silently_ignored(self, without_base_arg: Path):
        with pytest.raises(ContainerfileMismatchError, match="ARG BASE_IMAGE"):
            check_containerfile(without_base_arg, [("a", BASE)])


class TestCheckHarnessHome:
    def test_an_image_whose_harness_lives_in_root_needs_nothing(self):
        check_harness_home({"ROOT_WORKDIR": "/root", "HOME": "/root"})
        check_harness_home({})

    def test_home_must_be_the_root_workdir(self):
        with pytest.raises(RuntimeError, match="private files in '/karotte'"):
            check_harness_home({"ROOT_WORKDIR": "/karotte", "HOME": "/root"})

    @pytest.mark.parametrize(
        ("uid", "mode"),
        [
            (0, stat.S_IFDIR | 0o755),
            (1000, stat.S_IFDIR | 0o700),
            (0, stat.S_IFREG | 0o700),
        ],
    )
    def test_home_must_be_roots_and_private(
        self, monkeypatch: pytest.MonkeyPatch, uid: int, mode: int
    ):
        fake = os.stat_result((mode, 0, 0, 1, uid, 0, 0, 0, 0, 0))
        monkeypatch.setattr("karotte.task_image.os.lstat", lambda _path: fake)  # pyright: ignore[reportUnknownLambdaType]
        with pytest.raises(RuntimeError, match="mode 0700"):
            check_harness_home({"ROOT_WORKDIR": "/karotte", "HOME": "/karotte"})

    def test_a_private_root_owned_home_passes(self, monkeypatch: pytest.MonkeyPatch):
        fake = os.stat_result((stat.S_IFDIR | 0o700, 0, 0, 1, 0, 0, 0, 0, 0, 0))
        monkeypatch.setattr("karotte.task_image.os.lstat", lambda _path: fake)  # pyright: ignore[reportUnknownLambdaType]
        check_harness_home({"ROOT_WORKDIR": "/karotte", "HOME": "/karotte"})
