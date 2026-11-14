"""Process hardening for the intake worker and its checks (#6133, spec 2026-11-13 §7).

- `forbid_dumping`: ``prctl(PR_SET_DUMPABLE, 0)`` at the worker's start, so no other
  process of the same user (the check's child among them) can ``ptrace`` it or read its
  ``/proc/<pid>/mem``, ``environ`` or ``cwd``, where its service credential lives
  ([prctl(2)](https://man7.org/linux/man-pages/man2/prctl.2.html), ``PR_SET_DUMPABLE``;
  [ptrace(2)](https://man7.org/linux/man-pages/man2/ptrace.2.html), "Ptrace access mode
  checking").
- `isolate`: the child's ``unshare(CLONE_NEWUSER | CLONE_NEWNET | CLONE_NEWNS)`` before it
  reads the file. A new network namespace has only a loopback device that is down, so the
  parse reaches no socket off the process
  ([network_namespaces(7)](https://man7.org/linux/man-pages/man7/network_namespaces.7.html)).
  In its own mount namespace it then mounts an empty tmpfs over the spool (``hide``), so
  of the spool it reaches only its working directory, its own job's box: never another
  job's ([mount_namespaces(7)](https://man7.org/linux/man-pages/man7/mount_namespaces.7.html)).
  The user namespace, with the process's own uid and gid mapped to themselves
  ([user_namespaces(7)](https://man7.org/linux/man-pages/man7/user_namespaces.7.html)), is
  what lets an unprivileged process make the other two. A container runtime's default
  seccomp profile may refuse it (EPERM); the check then goes on, and the worker logs that
  once.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
from typing import Final

PR_SET_DUMPABLE: Final = 4
PR_GET_DUMPABLE: Final = 3
CLONE_NEWNS: Final = 0x00020000
CLONE_NEWUSER: Final = 0x10000000
CLONE_NEWNET: Final = 0x40000000
MS_NOSUID: Final = 2
MS_NODEV: Final = 4
MS_NOEXEC: Final = 8
MS_REC: Final = 0x4000
MS_PRIVATE: Final = 1 << 18


def _libc() -> ctypes.CDLL:
    libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
    libc.mount.argtypes = [
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_ulong,
        ctypes.c_char_p,
    ]
    return libc


def _refused(call: str) -> str:
    errno = ctypes.get_errno()
    return f"{call}: {os.strerror(errno)}"


def forbid_dumping() -> None:
    """This process made non-dumpable; raises `OSError` when the kernel refuses."""
    if _libc().prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
        errno = ctypes.get_errno()
        raise OSError(errno, f"prctl(PR_SET_DUMPABLE, 0): {os.strerror(errno)}")


def dumpable() -> bool:
    return bool(_libc().prctl(PR_GET_DUMPABLE, 0, 0, 0, 0))


def _write(path: str, text: str) -> None:
    with open(path, "w") as out:
        out.write(text)


def isolate(hide: str | None = None) -> str | None:
    """This (single-threaded) process moved into new user, network and (with ``hide``)
    mount namespaces, with an empty tmpfs over ``hide``; None when all of it took, else
    what the kernel refused (the steps before it still hold)."""
    libc = _libc()
    uid, gid = os.getuid(), os.getgid()
    flags = CLONE_NEWUSER | CLONE_NEWNET | (CLONE_NEWNS if hide else 0)
    if libc.unshare(flags) != 0:
        return _refused("unshare(CLONE_NEWUSER | CLONE_NEWNET | CLONE_NEWNS)")
    try:
        _write("/proc/self/setgroups", "deny")
        _write("/proc/self/uid_map", f"{uid} {uid} 1")
        _write("/proc/self/gid_map", f"{gid} {gid} 1")
    except OSError as error:
        return f"the user namespace's id maps: {error.strerror}"
    if hide is None:
        return None
    if libc.mount(None, b"/", None, MS_REC | MS_PRIVATE, None) != 0:
        return _refused("mount(MS_REC | MS_PRIVATE)")
    flags = MS_NOSUID | MS_NODEV | MS_NOEXEC
    if libc.mount(b"tmpfs", hide.encode(), b"tmpfs", flags, b"size=4k,mode=0") != 0:
        return _refused("mount(tmpfs over the spool)")
    return None
