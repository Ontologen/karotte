"""Hardware plugin for imported Harbor tasks, registered by karotte itself.

A Harbor task names its sandbox's CPUs, memory and disk; its task class puts
them in its hardware name (see :func:`karotte.harbor.task.hardware_name`), and
this reads them back. Imports nothing heavy: every karotte command loads it.
"""

import re

from karotte.hardware import HardwareLimits

_NAME = re.compile(r"^harbor-c(\d+)-m(\d+)-d(\d+)$")
_MIB = 1024**2
_HARNESS_RESERVE_MIB = 1024
"""The sandbox's RAM less this is the student's, so the task gets what it names."""


def hardware_limits(hardware: str) -> HardwareLimits | None:
    match = _NAME.match(hardware)
    if match is None:
        return None
    cpus, memory_mb, disk_mb = (int(group) for group in match.groups())
    return HardwareLimits(
        memory_bytes=(memory_mb + _HARNESS_RESERVE_MIB) * _MIB,
        disk_bytes=disk_mb * _MIB,
        cpus=cpus,
    )
