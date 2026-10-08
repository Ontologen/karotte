"""Run one command as an unprivileged uid: offline, confined, with a deadline.

For running code nobody vouches for during grading, such as a test suite that
exercises the student's patch. The command runs as ``uid`` in a session of its
own, under that uid's confinement (its cgroup or watchdog, set with
:func:`karotte.limit_resources`), reaching loopback only. Every process the uid
owns is killed before it starts and again after it ends, so nothing it left
behind can touch what the caller reads next.
"""

import ctypes
import fcntl
import os
import select
import socket
import stat
import struct
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from karotte.confinement import Sandbox, current_sandbox
from karotte.container import is_containerized
from karotte.process_utils import kill_processes
from karotte.subprocess import student_session_command
from karotte.trusted_bin import trusted_binary

DEFAULT_OUTPUT_CAP = 1 << 20

_SIOCGIFFLAGS = 0x8913
_SIOCSIFFLAGS = 0x8914
_IFF_UP = 0x1
_IFREQ = struct.Struct("16sH22x")
"""``struct ifreq`` as far as the flags: the name, the flags, padding to 40 bytes."""

_BRING_UP_LOOPBACK = 'set -e; "$1" link set lo up; shift; exec "$@"'
"""Run by ``sh`` inside the outer user namespace: ``$1`` is ``ip``, the rest the
command to exec once ``lo`` is up."""

_probed_user_namespaces: set[int] = set()
_DRAIN_AFTER_EXIT_SECONDS = 2.0

_PR_SET_NO_NEW_PRIVS = 38
_libc = ctypes.CDLL(None, use_errno=True)


@dataclass(frozen=True)
class OneShotResult:
    returncode: int | None
    """The command's exit status, or ``None`` when it was killed at the timeout."""
    timed_out: bool
    output: bytes
    """Its stdout and stderr together; the head and tail when longer than the cap."""
    truncated: bool


def run_one_shot(
    argv: Sequence[str],
    *,
    uid: int,
    cwd: str,
    env: Mapping[str, str],
    timeout: float,
    output_cap: int = DEFAULT_OUTPUT_CAP,
) -> OneShotResult:
    """Run ``argv`` as ``uid`` and return once every process ``uid`` owns is gone.

    ``env`` is the command's whole environment: nothing of the harness's is
    passed on. ``argv[0]`` must be an absolute path to a root-owned file
    nobody else can write, so the command is the one the caller meant.

    Raises when the command cannot be run as promised (not root in a sandbox,
    the network can't be cut off, the session can't start): those are failures
    of the harness. :class:`~karotte.process_utils.UnreapableCohortError`
    propagates, since a cohort that can't be reaped is the uid's doing.
    """
    if not is_containerized() or os.geteuid() != 0:
        raise RuntimeError(
            "run_one_shot runs commands as another uid from root in a sandbox only"
        )
    if uid == 0:
        raise ValueError("run_one_shot never runs a command as root")
    _check_trusted_executable(argv[0])

    kill_processes(uid)
    wrapped, enter_network = _loopback_only(list(argv), uid)
    session_argv, session_preexec = student_session_command(
        wrapped, 1, 2, disable_networking=False, uid=uid
    )

    def preexec() -> None:
        # Nothing the command execs gains privileges: not a setuid or
        # file-capability binary a task's base image may ship.
        if _libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "prctl(PR_SET_NO_NEW_PRIVS)")
        # Both need root, which the session's preexec gives up.
        if enter_network is not None:
            enter_network()
        if session_preexec is not None:
            session_preexec()

    deadline = time.monotonic() + timeout
    process = subprocess.Popen(
        session_argv,
        preexec_fn=preexec,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=cwd,
        env=dict(env),
        close_fds=True,
    )
    assert process.stdout is not None
    output = _CappedOutput(output_cap)
    timed_out = False
    try:
        timed_out = not _drain_until_exit(process, output, deadline)
    finally:
        kill_processes(uid)
        # The uid's processes are gone, so the pipe's write ends are closed.
        _ = _drain_until_exit(
            process, output, time.monotonic() + _DRAIN_AFTER_EXIT_SECONDS
        )
        process.stdout.close()
        returncode = process.wait()
    return OneShotResult(
        returncode=None if timed_out else returncode,
        timed_out=timed_out,
        output=output.value(),
        truncated=output.truncated,
    )


def _check_trusted_executable(path: str) -> None:
    if not os.path.isabs(path):
        raise ValueError(
            f"run_one_shot needs an absolute executable path, got {path!r}"
        )
    st = os.stat(path)
    if st.st_uid != 0 or st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ValueError(f"{path} must be owned by root and writable by root only")


def _loopback_only(
    argv: list[str], uid: int
) -> tuple[list[str], Callable[[], None] | None]:
    """``argv`` and a preexec to run as root before the privilege drop, such
    that the command reaches loopback and nothing else: a network namespace of
    its own, with only ``lo`` in it, up. The harness's listeners and every
    outside address are absent from it, so tests can bind any port.

    Where the kernel is the sandbox's own (a VM, gVisor), root builds the
    namespace in the preexec. On runc that needs a capability the container
    lacks, so the command builds it in user namespaces of its own, as the
    student itself could: an outer one where it is root, to bring ``lo`` up,
    and an inner one mapping it back to ``uid`` with no capabilities left.
    """
    if current_sandbox() in (Sandbox.VM, Sandbox.GVISOR):
        return argv, _enter_loopback_namespace

    unshare = trusted_binary("unshare")
    wrapped = [
        unshare,
        "--user",
        "--map-root-user",
        "--net",
        "--",
        trusted_binary("sh"),
        "-c",
        _BRING_UP_LOOPBACK,
        "sh",
        trusted_binary("ip"),
        unshare,
        "--user",
        f"--map-user={uid}",
        f"--map-group={uid}",
        "--",
        *argv,
    ]
    _probe_user_namespaces(uid, wrapped[: -len(argv)])
    return wrapped, None


def _probe_user_namespaces(uid: int, wrapper: list[str]) -> None:
    """Once per uid: that the wrapper runs a command at all. Otherwise a host
    that refuses user namespaces would make every command fail, and a test
    suite failing reads as the student's fault."""
    if uid in _probed_user_namespaces:
        return
    argv, preexec = student_session_command(
        [*wrapper, trusted_binary("true")], disable_networking=False, uid=uid
    )
    result = subprocess.run(
        argv, preexec_fn=preexec, capture_output=True, timeout=30, check=False
    )
    kill_processes(uid)
    if result.returncode != 0:
        raise RuntimeError(
            f"Could not give uid {uid} a network namespace of its own (user namespaces"
            + f" refused?): {result.stderr.decode(errors='replace').strip()}"
            + _apparmor_hint()
        )
    _probed_user_namespaces.add(uid)


def _apparmor_hint() -> str:
    """Ubuntu 23.10 and later let an unconfined process create a user namespace
    but not map its uid there, which reads as a bare EPERM."""
    try:
        with open("/proc/sys/kernel/apparmor_restrict_unprivileged_userns") as f:
            restricted = f.read().strip() == "1"
    except OSError:
        return ""
    if not restricted:
        return ""
    return (
        ". AppArmor restricts unprivileged user namespaces here"
        + " (kernel.apparmor_restrict_unprivileged_userns=1)"
    )


def _enter_loopback_namespace() -> None:
    """In the forked child, still root: a new network namespace with ``lo`` up."""
    os.unshare(os.CLONE_NEWNET)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        _, flags = _IFREQ.unpack(
            fcntl.ioctl(sock, _SIOCGIFFLAGS, _IFREQ.pack(b"lo", 0))
        )
        fcntl.ioctl(sock, _SIOCSIFFLAGS, _IFREQ.pack(b"lo", flags | _IFF_UP))


class _CappedOutput:
    """Keeps the first and last ``cap // 2`` bytes of what it is fed."""

    def __init__(self, cap: int) -> None:
        self._half: int = max(cap // 2, 1)
        self._head: bytearray = bytearray()
        self._tail: bytearray = bytearray()
        self.truncated: bool = False

    def feed(self, data: bytes) -> None:
        room = self._half - len(self._head)
        if room > 0:
            self._head += data[:room]
            data = data[room:]
        if data:
            self._tail += data
            if len(self._tail) > self._half:
                del self._tail[: len(self._tail) - self._half]
                self.truncated = True

    def value(self) -> bytes:
        if self.truncated:
            return (
                bytes(self._head)
                + b"\n[... output truncated ...]\n"
                + bytes(self._tail)
            )
        return bytes(self._head + self._tail)


def _drain_until_exit(
    process: "subprocess.Popen[bytes]", output: _CappedOutput, deadline: float
) -> bool:
    """Read ``process``'s output until it exits and its pipe is quiet, or the
    deadline passes. Returns whether it exited in time.

    Waits for the process, not for end of file: something it left running can
    hold the pipe open forever.
    """
    assert process.stdout is not None
    fd = process.stdout.fileno()
    os.set_blocking(fd, False)
    while (remaining := deadline - time.monotonic()) > 0:
        readable, _, _ = select.select([fd], [], [], min(remaining, 0.2))
        if not readable:
            if process.poll() is not None:
                return True
            continue
        try:
            chunk = os.read(fd, 65536)
        except BlockingIOError:
            continue
        if chunk:
            output.feed(chunk)
            continue
        # End of file: every write end is closed, so only the exit is left.
        try:
            process.wait(timeout=max(deadline - time.monotonic(), 0))
        except subprocess.TimeoutExpired:
            return False
        return True
    return process.poll() is not None
