"""Process hardening for the intake worker and its checks (#6133, spec 2026-11-13 §7).

`forbid_dumping`: ``prctl(PR_SET_DUMPABLE, 0)`` at the worker's start, so no other process
of the same user (the check's child among them) can ``ptrace`` it or read its
``/proc/<pid>/mem``, ``environ`` or ``cwd``, where its service credential lives
([prctl(2)](https://man7.org/linux/man-pages/man2/prctl.2.html), ``PR_SET_DUMPABLE``;
[ptrace(2)](https://man7.org/linux/man-pages/man2/ptrace.2.html), "Ptrace access mode
checking"). The child's namespaces, capabilities and seccomp filter are set by its
launcher, `confine.py`, before the parser starts.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
from typing import Final

PR_SET_DUMPABLE: Final = 4
PR_GET_DUMPABLE: Final = 3


def _libc() -> ctypes.CDLL:
    return ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)


def forbid_dumping() -> None:
    """This process made non-dumpable; raises `OSError` when the kernel refuses."""
    if _libc().prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
        errno = ctypes.get_errno()
        raise OSError(errno, f"prctl(PR_SET_DUMPABLE, 0): {os.strerror(errno)}")


def dumpable() -> bool:
    return bool(_libc().prctl(PR_GET_DUMPABLE, 0, 0, 0, 0))
