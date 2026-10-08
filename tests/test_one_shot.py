import os
import socket
import time
from collections.abc import Iterator
from pathlib import Path

import psutil
import pytest

from karotte import one_shot
from karotte.one_shot import run_one_shot

_SPARE_UID = 60125
"""Uid the commands below run as, kept clear of anything else on the box."""


class TestCappedOutput:
    def test_keeps_everything_under_the_cap(self):
        output = one_shot._CappedOutput(10)  # pyright: ignore[reportPrivateUsage]
        output.feed(b"abc")
        output.feed(b"de")
        assert output.value() == b"abcde"
        assert not output.truncated

    def test_keeps_the_head_and_tail_over_it(self):
        output = one_shot._CappedOutput(10)  # pyright: ignore[reportPrivateUsage]
        for chunk in (b"01234", b"56789", b"abcde"):
            output.feed(chunk)
        assert output.truncated
        assert output.value().startswith(b"01234")
        assert output.value().endswith(b"abcde")


def test_refuses_to_run_outside_a_sandbox(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("KAROTTE_CONTAINERIZED", raising=False)
    with pytest.raises(RuntimeError, match="from root in a sandbox"):
        run_one_shot(["/bin/true"], uid=_SPARE_UID, cwd="/", env={}, timeout=5)


@pytest.fixture(params=["vm", "runc"])
def sandbox(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Both ways a command gets its own network namespace: built by root, as
    in a VM, or in user namespaces of its own, as on runc."""
    monkeypatch.setenv("KAROTTE_CONTAINERIZED", "1")
    monkeypatch.setenv("KAROTTE_DEMOTE_ID", "1000")
    monkeypatch.setenv("KAROTTE_SANDBOX", request.param)
    yield


def _python() -> str:
    for candidate in ("/usr/bin/python3", "/usr/local/bin/python3"):
        if os.path.exists(candidate) and os.stat(candidate).st_uid == 0:
            return candidate
    pytest.skip("no root-owned system python3")


def _run(command: str, timeout: float = 30, **kwargs: object) -> one_shot.OneShotResult:
    return run_one_shot(
        ["/bin/bash", "-c", command],
        uid=_SPARE_UID,
        cwd="/",
        env={"PATH": "/usr/local/bin:/usr/bin:/bin", "MARK": "given"},
        timeout=timeout,
        **kwargs,  # pyright: ignore[reportArgumentType]
    )


@pytest.mark.requires_root
@pytest.mark.usefixtures("sandbox")
class TestRunOneShot:
    def test_runs_as_the_uid_with_exactly_the_env_given(self):
        result = _run("id -u; echo $MARK; echo ${KAROTTE_DEMOTE_ID:-unset}")

        assert result.returncode == 0
        assert not result.timed_out
        assert result.output.decode().split() == [str(_SPARE_UID), "given", "unset"]

    def test_never_runs_as_root(self):
        with pytest.raises(ValueError, match="never runs a command as root"):
            run_one_shot(["/bin/true"], uid=0, cwd="/", env={}, timeout=5)

    def test_refuses_an_executable_others_can_write(self, tmp_path: Path):
        script = tmp_path / "run"
        script.write_text("#!/bin/sh\n")
        script.chmod(0o777)
        with pytest.raises(ValueError, match="writable by root only"):
            run_one_shot([str(script)], uid=_SPARE_UID, cwd="/", env={}, timeout=5)

    def test_kills_it_at_the_timeout(self):
        started = time.monotonic()
        result = _run("echo started; sleep 60", timeout=1)

        assert result.timed_out
        assert result.returncode is None
        assert b"started" in result.output
        assert time.monotonic() - started < 30

    def test_caps_the_output(self):
        result = _run("head -c 100000 /dev/zero | tr '\\0' x", output_cap=1000)

        assert result.truncated
        assert len(result.output) < 1100

    def test_returns_when_it_exits_and_leaves_nothing_behind(self):
        """A background child holding the pipe must not stall it, or outlive it."""
        started = time.monotonic()
        result = _run("sleep 300 & echo done")

        assert result.returncode == 0
        assert time.monotonic() - started < 30
        assert not [
            p
            for p in psutil.process_iter(["uids"])
            if p.info["uids"].real == _SPARE_UID
        ]

    def test_reaches_loopback_but_not_the_harness(self):
        harness = socket.socket()
        harness.bind(("127.0.0.1", 0))
        harness.listen()
        port = harness.getsockname()[1]
        probe = (
            "import socket\n"
            f"try:\n socket.create_connection(('127.0.0.1', {port}), timeout=2); print('harness reached')\n"
            "except OSError: print('harness blocked')\n"
            "s = socket.socket(); s.bind(('127.0.0.1', 0)); s.listen()\n"
            "socket.create_connection(s.getsockname(), timeout=2); print('loopback ok')\n"
            "try:\n socket.create_connection(('1.1.1.1', 80), timeout=2); print('outside reached')\n"
            "except OSError: print('outside blocked')\n"
        )
        try:
            result = run_one_shot(
                [_python(), "-I", "-c", probe],
                uid=_SPARE_UID,
                cwd="/",
                env={},
                timeout=30,
            )
        finally:
            harness.close()

        assert result.output.decode().split("\n")[:3] == [
            "harness blocked",
            "loopback ok",
            "outside blocked",
        ], result.output
