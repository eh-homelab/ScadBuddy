"""Process hardening for the intake worker and its checks (#6133, spec 2026-11-13 §7).

- `forbid_dumping`: ``prctl(PR_SET_DUMPABLE, 0)`` at the worker's start, so no other
  process of the same user (the check's child among them) can ``ptrace`` it or read its
  ``/proc/<pid>/mem`` or ``environ``, where its service credential lives
  ([prctl(2)](https://man7.org/linux/man-pages/man2/prctl.2.html), ``PR_SET_DUMPABLE``;
  [ptrace(2)](https://man7.org/linux/man-pages/man2/ptrace.2.html), "Ptrace access mode
  checking").
- `isolate`: the child's ``unshare(CLONE_NEWUSER | CLONE_NEWNET)`` before it reads the
  file: a new network namespace has only a loopback device that is down, so the parse
  reaches no socket off the process
  ([unshare(2)](https://man7.org/linux/man-pages/man2/unshare.2.html),
  [network_namespaces(7)](https://man7.org/linux/man-pages/man7/network_namespaces.7.html)).
  The user namespace is what lets an unprivileged process make the network one. A
  container runtime's default seccomp profile may refuse it (EPERM); the check then goes
  on, and the worker logs that once.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
from typing import Final

PR_SET_DUMPABLE: Final = 4
PR_GET_DUMPABLE: Final = 3
CLONE_NEWUSER: Final = 0x10000000
CLONE_NEWNET: Final = 0x40000000


def _libc() -> ctypes.CDLL:
    return ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)


def forbid_dumping() -> None:
    """This process made non-dumpable; raises `OSError` when the kernel refuses."""
    if _libc().prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
        errno = ctypes.get_errno()
        raise OSError(errno, f"prctl(PR_SET_DUMPABLE, 0): {os.strerror(errno)}")


def dumpable() -> bool:
    return bool(_libc().prctl(PR_GET_DUMPABLE, 0, 0, 0, 0))


def isolate() -> str | None:
    """This (single-threaded) process moved into new user and network namespaces; None
    when it was, else why the kernel refused."""
    if _libc().unshare(CLONE_NEWUSER | CLONE_NEWNET) != 0:
        errno = ctypes.get_errno()
        return f"unshare(CLONE_NEWUSER | CLONE_NEWNET): {os.strerror(errno)}"
    return None
