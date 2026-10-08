"""Grade a Harbor task the way its separate verifier environment would, inside
the run's own sandbox, without trusting anything the student touched.

Harbor grades a patch of the agent's commits in a fresh container. Here the
student is unprivileged, so everything it could change is either deleted (it
owns it) or restored from a root-only copy (the task's workdir): what's left is
the image as built, which is that fresh container. On top of that:

- the patch is collected by the student itself, so git never runs the
  student's repository config as root;
- the tests run as the grader, a uid of their own, offline, under limits and
  a deadline, with every process confirmed dead before anything is read;
- ``test.sh`` is split where its task-specific block ends: the suites run in
  one phase, ``grader.py grade`` in another on clean copies of their reports,
  so code under test can't write the reward or break the grading.

Code under test runs in the same process as its test framework, so it can
still forge that framework's reports; no grader of test results avoids that.
"""

import errno
import json
import math
import os
import shutil
import stat
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from karotte.confinement import FileLimit, limit_resources
from karotte.file_quota import file_quota_mounted
from karotte.harbor.session import seed_home, session_env, task_env
from karotte.judges.judge import Judge
from karotte.one_shot import OneShotResult, run_one_shot
from karotte.process_utils import kill_processes
from karotte.reclaim import delete_files
from karotte.save_artifact import save_artifact
from karotte.save_submission import save_submission
from karotte.schemas.evaluation_run_config import EvaluationRunConfig
from karotte.schemas.scoring import Scoring
from karotte.schemas.transcript import Transcript
from karotte.student_misbehavior import StudentMisbehaviorError
from karotte.trusted_bin import TRUSTED_BIN_PATH, trusted_binary

PRISTINE_WORKDIR = Path("/karotte/pristine/workdir")
LOGS = Path("/logs")
TESTS = Path("/tests")
GRADER_UID = 1001
GRADER_HOME = LOGS / "grader-home"

PHASE_B_SECONDS = 120.0
"""What grading reports into a reward takes, on top of the task's verifier timeout."""
PREFLIGHT_SECONDS = 60.0
PATCH_MAX_BYTES = 64 * 1024**2
REPORT_MAX_BYTES = 256 * 1024**2
REWARD_MAX_BYTES = 64 * 1024
ARTIFACT_MAX_BYTES = 16 * 1024**2
PROCESS_LIMIT = 2048
FILE_COUNT_LIMIT = 1_000_000
_TAIL = 4000


@dataclass
class HarborGrading:
    """What grading a task needs to know about it."""

    task_dir_name: str
    tests_dir: Path
    """The task's tests and its ``phase_a.sh``, root-only."""
    workdir: str
    student_uid: int
    collect: list[tuple[str, float]]
    verifier_timeout_sec: float
    verifier_env: dict[str, str]
    memory_mb: int
    storage_mb: int
    require_cmds: list[str] = field(default_factory=list)


class HarborVerifierJudge(Judge):
    def __init__(self, grading: HarborGrading, config: EvaluationRunConfig) -> None:
        self.grading: HarborGrading = grading
        self.config: EvaluationRunConfig = config

    def evaluate(self, transcript: Transcript) -> Scoring:
        del transcript
        return _Grader(self.grading, self.config).grade()


class _Grader:
    def __init__(self, grading: HarborGrading, config: EvaluationRunConfig) -> None:
        self.g: HarborGrading = grading
        self.config: EvaluationRunConfig = config
        self.base: dict[str, str] = task_env()
        self.metadata: dict[str, Any] = {}

    def grade(self) -> Scoring:
        kill_processes(self.g.student_uid)
        # Room for the patch: the student's own files, but for its work, go first.
        delete_files(self.g.student_uid, extend_exclude=(Path(self.g.workdir),))
        patch = self._collect()
        delete_files(self.g.student_uid)
        if patch is None:
            return self._score(0.0, "collect_failed")
        if patch.stat().st_size == 0:
            self.metadata["no_patch"] = True
        self._stage(patch)
        self._preflight()

        phase_a = self._run_as_grader(
            ["/bin/bash", str(TESTS / "phase_a.sh")],
            cwd=self.g.workdir,
            timeout=self.g.verifier_timeout_sec,
        )
        self.metadata["phase_a_exit"] = phase_a.returncode
        self.metadata["phase_a_output"] = _tail(phase_a.output)
        if phase_a.timed_out:
            return self._finish(0.0, "verifier_timeout")

        apply_failed, reports = self._hand_off_reports()
        if not reports and not apply_failed:
            return self._finish(0.0, "tests_not_run")
        # Phase B runs only grader.py, on clean copies of the reports: not with
        # the workdir in reach (an editable install puts it on sys.path), not
        # with site processing (-S), not with any of the task's environment.
        _close_to_grader(Path(self.g.workdir))
        phase_b = self._run_as_grader(
            [
                _trusted_python(),
                "-I",
                "-S",
                str(TESTS / "grader.py"),
                "grade",
                *(["--apply-failed"] if apply_failed else []),
            ],
            cwd=str(LOGS / "grade"),
            timeout=PHASE_B_SECONDS,
            env={
                "PATH": TRUSTED_BIN_PATH,
                "HOME": str(GRADER_HOME),
                "LANG": "C.UTF-8",
                "VERIFIER_DIR": str(LOGS / "grade"),
                "TESTS_DIR": str(TESTS),
            },
        )
        self.metadata["phase_b_output"] = _tail(phase_b.output)
        if phase_b.timed_out or phase_b.returncode != 0:
            return self._finish(0.0, "verifier_error")
        reward = _read_reward(LOGS / "grade" / "reward.json")
        if reward is None:
            return self._finish(0.0, "verifier_error")
        self.metadata.update({k: v for k, v in reward.items() if k != "reward"})
        return self._finish(float(reward["reward"]), "ok")

    # --- the student's side ---------------------------------------------------

    def _collect(self) -> Path | None:
        """The patch the task's collect hooks make of the student's work, in
        custody; ``None`` when a hook failed. The hooks run as the student, so
        git reads the student's repository config with the student's rights."""
        artifacts = LOGS / "artifacts"
        with _full_disk_is_misbehavior():
            _empty(LOGS)
            _fresh_dir(artifacts, 0o700, owner=self.g.student_uid)
        env = session_env(
            os.environ.get("KAROTTE_WORKDIR", "/workdir"),
            "student",
            base=self.base,
            extra={"TMPDIR": "/tmp"},
        )
        for command, timeout in self.g.collect:
            result = run_one_shot(
                ["/bin/bash", "-c", command],
                uid=self.g.student_uid,
                cwd=self.g.workdir,
                env=env,
                timeout=timeout,
            )
            if result.timed_out or result.returncode != 0:
                self.metadata["collect_output"] = _tail(result.output)
                return None
        saved = save_submission(
            artifacts / "model.patch",
            max_file_bytes=PATCH_MAX_BYTES,
            owner_uid=self.g.student_uid,
        )
        patch = saved / "model.patch"
        if not patch.is_file():
            return None
        _save_artifact(self.config, patch)
        return patch

    # --- staging the verifier ---------------------------------------------------

    def _stage(self, patch: Path) -> None:
        """The task's workdir as the image had it, owned by the grader; the
        tests and the patch as root's, read-only; fresh scratch space."""
        # Deleting the student's files took the workdir itself, unless it's a
        # mount point (the disk quota's), which only empties.
        workdir = Path(self.g.workdir)
        if workdir.exists():
            _empty(workdir)
        shutil.copytree(PRISTINE_WORKDIR, workdir, symlinks=True, dirs_exist_ok=True)
        _chown_tree(workdir, GRADER_UID)

        _fresh_dir(TESTS, 0o755)
        for name in ("test.sh", "test.patch", "grader.py", "config.json", "phase_a.sh"):
            shutil.copyfile(self.g.tests_dir / name, TESTS / name)
            (TESTS / name).chmod(0o755 if name.endswith(".sh") else 0o644)

        _empty(LOGS)
        _fresh_dir(LOGS / "artifacts", 0o755)
        shutil.copyfile(patch, LOGS / "artifacts" / "model.patch")
        (LOGS / "artifacts" / "model.patch").chmod(0o644)
        _fresh_dir(LOGS / "verifier", 0o755, owner=GRADER_UID)
        _fresh_dir(GRADER_HOME, 0o700, owner=GRADER_UID)
        seed_home(GRADER_HOME, GRADER_UID, base=self.base)

        limit_resources(
            uid=GRADER_UID,
            memory_bytes=self.g.memory_mb * 1024**2,
            process_count=PROCESS_LIMIT,
            # The sandbox's quota, where there is one, covers these already.
            file=None
            if file_quota_mounted()
            else FileLimit(
                path=(LOGS, Path("/tmp"), workdir),
                bytes=self.g.storage_mb * 1024**2,
                count=FILE_COUNT_LIMIT,
            ),
        )

    def _preflight(self) -> None:
        """What the tests need is there and writable before they run. A failure
        here is the image's or the harness's, never the student's."""
        checks = ['test -w "$1"', "test -w /logs/verifier"] + [
            f"command -v {tool} > /dev/null" for tool in self.g.require_cmds
        ]
        result = self._run_as_grader(
            ["/bin/bash", "-c", " && ".join(checks), "preflight", self.g.workdir],
            cwd="/",
            timeout=PREFLIGHT_SECONDS,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"The grader can't run {self.g.task_dir_name}'s tests: {_tail(result.output)}"
            )

    def _hand_off_reports(self) -> tuple[bool, list[Path]]:
        """Clean, root-owned copies of the reports ``config.json`` names, in a
        fresh ``/logs/verifier`` where nothing else from phase A is. Returns
        whether ``prepare`` said the student's patch didn't apply, and the
        reports there were."""
        config = json.loads((self.g.tests_dir / "config.json").read_text())
        reports = [Path(p) for p in config.get("grade", {}).get("reports", [])]
        phase_a = LOGS / "verifier.phase_a"
        handed: list[Path] = []
        with _full_disk_is_misbehavior():
            os.rename(LOGS / "verifier", phase_a)
            _fresh_dir(LOGS / "verifier", 0o755)
            for report in reports:
                if self._hand_off(phase_a, report):
                    handed.append(report)
            _fresh_dir(LOGS / "grade", 0o755, owner=GRADER_UID)
        marker = _read_reward(phase_a / "reward.json", untrusted=True)
        apply_failed = bool(marker and marker.get("apply_failed"))
        self.metadata["apply_failed"] = apply_failed
        return apply_failed, handed

    def _hand_off(self, phase_a: Path, report: Path) -> bool:
        if report.parent != LOGS / "verifier" or report.name.startswith("reward"):
            raise RuntimeError(
                f"config.json names a report outside the verifier's own: {report}"
            )
        try:
            saved = save_submission(
                phase_a / report.name,
                max_file_bytes=REPORT_MAX_BYTES,
                owner_uid=GRADER_UID,
            )
        except StudentMisbehaviorError as e:
            # Code under test can make its reports anything; a bad one is a
            # missing one, which grade() counts as failed tests.
            self.metadata.setdefault("reports_refused", []).append(str(e))
            return False
        if not (saved / report.name).is_file():
            return False
        shutil.copyfile(saved / report.name, report)
        report.chmod(0o644)
        return True

    # --- running as the grader ---------------------------------------------------

    def _run_as_grader(
        self,
        argv: list[str],
        *,
        cwd: str,
        timeout: float,
        env: Mapping[str, str] | None = None,
    ) -> OneShotResult:
        """``argv`` as the grader, by default in the task's environment."""
        if env is None:
            env = session_env(
                str(GRADER_HOME),
                "grader",
                base=self.base,
                extra={**self.g.verifier_env, "TMPDIR": "/tmp"},
            )
        return run_one_shot(argv, uid=GRADER_UID, cwd=cwd, env=env, timeout=timeout)

    # --- the result ---------------------------------------------------

    def _finish(self, score: float, status: str) -> Scoring:
        for name in ("reward.json", "ctrf.json"):
            path = LOGS / "grade" / name
            if (
                path.is_file()
                and not path.is_symlink()
                and path.stat().st_size <= ARTIFACT_MAX_BYTES
            ):
                _save_artifact(self.config, path)
        kill_processes(GRADER_UID)
        delete_files(GRADER_UID)
        return self._score(score, status)

    def _score(self, score: float, status: str) -> Scoring:
        self.metadata["verifier_status"] = status
        # A Harbor task is one step: it passes when it's solved.
        return Scoring(score=score, metadata=self.metadata, continue_task=score >= 1)


def _read_reward(path: Path, *, untrusted: bool = False) -> dict[str, Any] | None:
    """``reward.json`` if it's a regular file holding a reward in [0, 1].

    Phase B's is written by the grader running ``grader.py`` alone; an
    ``untrusted`` one is only ever read for advice."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > REWARD_MAX_BYTES:
            return None
        data = json.loads(os.read(fd, REWARD_MAX_BYTES))
    except (OSError, ValueError):
        return None
    finally:
        os.close(fd)
    if not isinstance(data, dict):
        return None
    if untrusted:
        return data
    reward = data.get("reward")
    if isinstance(reward, bool) or not isinstance(reward, int | float):
        return None
    if not math.isfinite(reward) or not 0 <= reward <= 1:
        return None
    return data


def _trusted_python() -> str:
    """The image's ``python3``, checked to be root's along with every directory
    above it, since phase B runs only it and ``grader.py``."""
    path = Path(trusted_binary("python3")).resolve()
    for part in (path, *path.parents):
        st = part.stat()
        if st.st_uid != 0 or st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise RuntimeError(f"{part} is writable by someone other than root")
    return str(path)


@contextmanager
def _full_disk_is_misbehavior() -> Iterator[None]:
    """Root's own writes where the student, or code it wrote, can fill the
    disk quota first: a full quota there is theirs, not the harness's."""
    try:
        yield
    except OSError as e:
        if e.errno in (errno.ENOSPC, errno.EDQUOT):
            raise StudentMisbehaviorError(f"the disk quota was full: {e}") from e
        raise


def _close_to_grader(workdir: Path) -> None:
    """Take the workdir back from the grader before phase B, so nothing in it
    can be imported or loaded there."""
    os.chown(workdir, 0, 0)
    workdir.chmod(0o700)


def _fresh_dir(path: Path, mode: int, *, owner: int = 0) -> None:
    """``path`` as a new, empty directory, whatever was there before."""
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)
    path.mkdir(mode=mode)
    path.chmod(mode)
    os.chown(path, owner, owner)


def _empty(directory: Path) -> None:
    """Remove everything in ``directory``, whoever owns it, without following
    links. Clears rather than removes: it may be a mount point."""
    for entry in os.scandir(directory):
        if entry.is_dir(follow_symlinks=False):
            shutil.rmtree(entry.path)
        else:
            os.unlink(entry.path)


def _chown_tree(root: Path, uid: int) -> None:
    for dirpath, dirnames, filenames in os.walk(root):
        os.lchown(dirpath, uid, uid)
        for name in (*dirnames, *filenames):
            os.lchown(os.path.join(dirpath, name), uid, uid)


def _save_artifact(config: EvaluationRunConfig, path: Path) -> None:
    try:
        save_artifact(config, path)
    except Exception as e:  # noqa: BLE001 - an artifact never decides a score
        logger.warning(f"Could not save {path} as an artifact: {e}")


def _tail(output: bytes) -> str:
    return output[-_TAIL:].decode(errors="replace")
