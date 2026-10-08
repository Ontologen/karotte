import sys
from collections.abc import Iterator
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

import pytest
import typer

from karotte.cli.build import build
from karotte.schemas.base_image import BaseImage
from karotte.task_image import image_tag


@pytest.fixture(autouse=True)
def _engine_on_path() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]
    with (
        patch("karotte.build.which", return_value="/usr/bin/engine"),
        patch("karotte.build._buildx_available", return_value=True),
        patch("karotte.build._buildctl_available", return_value=True),
    ):
        yield


@pytest.mark.parametrize(
    ("runtime", "message"),
    [
        ("docker", "docker not found. Install it or pass --runtime podman."),
        ("podman", "podman not found. Install it or pass --runtime docker."),
        ("nerdctl", "nerdctl not found. Install it or pass --runtime docker."),
    ],
)
def test_missing_runtime_exits_with_one_line(
    capsys: pytest.CaptureFixture[str], runtime: str, message: str
):
    with (
        patch("karotte.build.which", return_value=None),
        patch("karotte.cli.build.build_container") as mock_build,
        pytest.raises(typer.Exit) as exc_info,
    ):
        build(runtime=runtime)  # pyright: ignore[reportArgumentType]

    assert exc_info.value.exit_code == 1
    assert capsys.readouterr().err.strip() == message
    mock_build.assert_not_called()


BUILDX_MISSING = (
    "docker buildx not found, and building the image needs it. "
    + "Install the docker-buildx package or Docker's docker-buildx-plugin."
)


@pytest.mark.parametrize("runtime", ["docker", "docker:gvisor"])
def test_missing_buildx_exits_with_one_line(
    capsys: pytest.CaptureFixture[str], runtime: str
):
    with (
        patch("karotte.build._buildx_available", return_value=False),
        patch("karotte.cli.build.build_container") as mock_build,
        pytest.raises(typer.Exit) as exc_info,
    ):
        build(runtime=runtime)  # pyright: ignore[reportArgumentType]

    assert exc_info.value.exit_code == 1
    assert capsys.readouterr().err.strip() == BUILDX_MISSING
    mock_build.assert_not_called()


@pytest.mark.parametrize("runtime", ["podman", "nerdctl"])
def test_buildx_is_only_checked_for_docker(runtime: str):
    with (
        patch("karotte.build._buildx_available") as buildx,
        patch("karotte.cli.build.build_container"),
    ):
        build(runtime=runtime)  # pyright: ignore[reportArgumentType]

    buildx.assert_not_called()


def test_missing_buildctl_exits_with_one_line(capsys: pytest.CaptureFixture[str]):
    with (
        patch("karotte.build._buildctl_available", return_value=False),
        patch("karotte.cli.build.build_container") as mock_build,
        pytest.raises(typer.Exit) as exc_info,
    ):
        build(runtime="nerdctl")

    assert exc_info.value.exit_code == 1
    assert capsys.readouterr().err.strip() == (
        "buildctl not found, and nerdctl needs BuildKit to build the image. "
        + "Install BuildKit and start buildkitd."
    )
    mock_build.assert_not_called()


@pytest.mark.parametrize("runtime", ["docker", "podman"])
def test_buildctl_is_only_checked_for_nerdctl(runtime: str):
    with (
        patch("karotte.build._buildctl_available") as buildctl,
        patch("karotte.cli.build.build_container"),
    ):
        build(runtime=runtime)  # pyright: ignore[reportArgumentType]

    buildctl.assert_not_called()


@pytest.fixture(autouse=True)
def _docker_by_default(monkeypatch: pytest.MonkeyPatch):  # pyright: ignore[reportUnusedFunction]
    """The real default depends on the OS running the tests."""
    # The package re-exports the `build` function under the module's name.
    monkeypatch.setattr(
        sys.modules["karotte.cli.build"],
        "default_runtime",
        lambda _hardware=None: "docker",  # pyright: ignore[reportUnknownLambdaType]
    )


class TestBuildCommand:
    def _tasks(
        self, monkeypatch: pytest.MonkeyPatch, hardware: dict[str, str | None]
    ) -> None:
        def fake_task(task_id: str, hw: str | None) -> type:
            class FakeTask:
                id: str = task_id
                required_hardware: str | None = hw
                base_image: object = None

                def __init__(self, config: object) -> None:
                    del config

            return FakeTask

        classes = [fake_task(task_id, hw) for task_id, hw in hardware.items()]
        monkeypatch.setattr("karotte.load_tasks.load_all_task_classes", lambda: classes)
        monkeypatch.setattr(
            sys.modules["karotte.cli.build"],
            "default_runtime",
            lambda hw=None: "docker" if hw == "gpu-1" else "apple-container",  # pyright: ignore[reportUnknownLambdaType]
        )

    def test_the_default_runtime_is_the_one_run_picks_for_the_tasks(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """`run` picks by each task's hardware; `build` must fill that store."""
        self._tasks(monkeypatch, {"a": "gpu-1", "b": "gpu-1"})
        with patch("karotte.cli.build.build_container") as mock_build:
            build()
        assert mock_build.call_args.args[0] == "docker"

    def test_tasks_that_would_pick_different_runtimes_need_one_named(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        self._tasks(monkeypatch, {"a": "gpu-1", "b": "cpu-4"})
        with patch("karotte.cli.build.build_container") as mock_build:
            with pytest.raises(typer.Exit):
                build()
        mock_build.assert_not_called()

    def test_outside_an_environment_the_default_hardware_decides(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        def no_environment() -> list[object]:
            raise ModuleNotFoundError("environment")

        monkeypatch.setattr("karotte.load_tasks.load_all_task_classes", no_environment)
        monkeypatch.setattr(
            sys.modules["karotte.cli.build"], "default_hardware", lambda: "gpu-1"
        )
        monkeypatch.setattr(
            sys.modules["karotte.cli.build"],
            "default_runtime",
            lambda hw=None: "docker" if hw == "gpu-1" else "apple-container",  # pyright: ignore[reportUnknownLambdaType]
        )
        with patch("karotte.cli.build.build_container") as mock_build:
            build()
        assert mock_build.call_args.args[0] == "docker"

    def test_calls_build_container_with_defaults(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(
            sys.modules["karotte.cli.build"],
            "default_runtime",
            lambda _hardware=None: "nerdctl",  # pyright: ignore[reportUnknownLambdaType]
        )
        with patch("karotte.cli.build.build_container") as mock_build:
            build()

        mock_build.assert_called_once_with(
            "nerdctl",
            ".",
            "karotte",
            cache_from=None,
            cache_to=None,
            build_secrets=(),
            build_args={},
            platform=None,
        )

    def test_calls_build_container_with_custom_runtime(self):
        with patch("karotte.cli.build.build_container") as mock_build:
            build(runtime="podman")

        mock_build.assert_called_once_with(
            "podman",
            ".",
            "karotte",
            cache_from=None,
            cache_to=None,
            build_secrets=(),
            build_args={},
            platform=None,
        )

    def test_calls_build_container_with_custom_tag(self):
        with patch("karotte.cli.build.build_container") as mock_build:
            build(tag="my-custom-image")

        mock_build.assert_called_once_with(
            "docker",
            ".",
            "my-custom-image",
            cache_from=None,
            cache_to=None,
            build_secrets=(),
            build_args={},
            platform=None,
        )

    def test_calls_build_container_with_custom_build_context(self):
        with patch("karotte.cli.build.build_container") as mock_build:
            build(build_context="/path/to/context")

        mock_build.assert_called_once_with(
            "docker",
            "/path/to/context",
            "karotte",
            cache_from=None,
            cache_to=None,
            build_secrets=(),
            build_args={},
            platform=None,
        )

    def test_calls_build_container_with_all_custom_options(self):
        with patch("karotte.cli.build.build_container") as mock_build:
            build(runtime="podman", tag="my-image:v2", build_context="/custom/path")

        mock_build.assert_called_once_with(
            "podman",
            "/custom/path",
            "my-image:v2",
            cache_from=None,
            cache_to=None,
            build_secrets=(),
            build_args={},
            platform=None,
        )

    def test_calls_build_container_with_cache_from(self):
        with patch("karotte.cli.build.build_container") as mock_build:
            build(cache_from=["type=registry,ref=myrepo:cache"])

        mock_build.assert_called_once_with(
            "docker",
            ".",
            "karotte",
            cache_from=["type=registry,ref=myrepo:cache"],
            cache_to=None,
            build_secrets=(),
            build_args={},
            platform=None,
        )

    def test_calls_build_container_with_cache_to(self):
        with patch("karotte.cli.build.build_container") as mock_build:
            build(cache_to=["type=local,dest=/tmp/cache"])

        mock_build.assert_called_once_with(
            "docker",
            ".",
            "karotte",
            cache_from=None,
            cache_to=["type=local,dest=/tmp/cache"],
            build_secrets=(),
            build_args={},
            platform=None,
        )

    def test_calls_build_container_with_multiple_cache_flags(self):
        with patch("karotte.cli.build.build_container") as mock_build:
            build(
                cache_from=["type=registry,ref=a", "type=local,src=/tmp"],
                cache_to=["type=registry,ref=b"],
            )

        mock_build.assert_called_once_with(
            "docker",
            ".",
            "karotte",
            cache_from=["type=registry,ref=a", "type=local,src=/tmp"],
            cache_to=["type=registry,ref=b"],
            build_secrets=(),
            build_args={},
            platform=None,
        )

    def test_forwards_build_secrets(self):
        with patch("karotte.cli.build.build_container") as mock_build:
            build(build_secret=["uv_env=/tmp/uv_env"])

        mock_build.assert_called_once_with(
            "docker",
            ".",
            "karotte",
            cache_from=None,
            cache_to=None,
            build_secrets=["uv_env=/tmp/uv_env"],
            build_args={},
            platform=None,
        )


class TestTaskBaseImages:
    """Tasks that bring a base image each run in an image built on it."""

    BASE_A: ClassVar[BaseImage] = BaseImage(
        ref="registry.example/a:v1", platform="linux/amd64", workdir="/app"
    )
    BASE_B: ClassVar[BaseImage] = BaseImage(ref="registry.example/b:v1")

    def _tasks(
        self, monkeypatch: pytest.MonkeyPatch, bases: dict[str, BaseImage | None]
    ) -> None:
        def fake_task(task_id: str, base: BaseImage | None) -> type:
            class FakeTask:
                id: str = task_id
                required_hardware: str | None = None
                base_image: BaseImage | None = base

                def __init__(self, config: object) -> None:
                    del config

            return FakeTask

        classes = [fake_task(task_id, base) for task_id, base in bases.items()]
        monkeypatch.setattr("karotte.load_tasks.load_all_task_classes", lambda: classes)

    def test_builds_one_image_per_distinct_base(self, monkeypatch: pytest.MonkeyPatch):
        self._tasks(
            monkeypatch, {"a1": self.BASE_A, "a2": self.BASE_A, "b": self.BASE_B}
        )
        with patch("karotte.cli.build.build_container") as mock_build:
            build(runtime="docker")
        built = {c.args[2]: c.kwargs for c in mock_build.call_args_list}
        assert set(built) == {image_tag(self.BASE_A), image_tag(self.BASE_B)}
        assert built[image_tag(self.BASE_A)]["build_args"] == {
            "BASE_IMAGE": "registry.example/a:v1",
            "TASK_WORKDIR": "/app",
        }
        assert built[image_tag(self.BASE_A)]["platform"] == "linux/amd64"
        assert built[image_tag(self.BASE_B)]["platform"] is None

    def test_task_builds_only_its_image(self, monkeypatch: pytest.MonkeyPatch):
        self._tasks(monkeypatch, {"a": self.BASE_A, "b": self.BASE_B})
        with patch("karotte.cli.build.build_container") as mock_build:
            build(runtime="docker", task=["b"])
        mock_build.assert_called_once()
        assert mock_build.call_args.args[2] == image_tag(self.BASE_B)

    def test_a_tag_names_one_image_only(self, monkeypatch: pytest.MonkeyPatch):
        self._tasks(monkeypatch, {"a": self.BASE_A, "b": self.BASE_B})
        with patch("karotte.cli.build.build_container") as mock_build:
            with pytest.raises(typer.Exit):
                build(runtime="docker", tag="mine")
        mock_build.assert_not_called()

    def test_an_unknown_task_is_refused(self, monkeypatch: pytest.MonkeyPatch):
        self._tasks(monkeypatch, {"a": self.BASE_A})
        with pytest.raises(typer.Exit):
            build(runtime="docker", task=["nope"])

    def test_a_containerfile_without_the_base_arg_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        (tmp_path / "Containerfile").write_text(
            "FROM docker.io/library/amazonlinux:2023\n"
        )
        self._tasks(monkeypatch, {"a": self.BASE_A})
        with patch("karotte.cli.build.build_container") as mock_build:
            with pytest.raises(typer.Exit):
                build(runtime="docker", build_context=str(tmp_path))
        mock_build.assert_not_called()
