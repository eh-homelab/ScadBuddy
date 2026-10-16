"""Confine an ``openscad`` run to the files it is allowed to read (#994).

A model's source is untrusted, and OpenSCAD opens whatever path it is given:
``include </etc/passwd>``, ``import("/proc/self/environ")``, a path computed at run
time, or a symbolic link in the model's own directory. This runs the binary under a
Landlock ruleset (https://docs.kernel.org/userspace-api/landlock.html), which the
kernel enforces on every open whatever the path is spelled as, so the only files it
can read are the system's own (libraries, fonts), the model's directory and the
libraries it pins, and the only place it can write is where its output goes.

Landlock needs no privilege — no capability, no user namespace — so it works in a
pod that drops every capability. It does not hide whether a path *exists* (a stat is
not a read), which is why ``include``/``use`` targets are also refused before the
run, in :mod:`scadbuddy.render.runner`.

This file is also the launcher: ``python -I -S sandbox.py --read P --write P -- cmd``
applies the ruleset to itself and ``exec``s ``cmd``, which inherits it. It is run as a
script, not a module, so it imports nothing from the package and starts in tens of
milliseconds — a parse check runs on every pause in typing. Restricting the child
from ``preexec_fn`` instead would call into ctypes between ``fork`` and ``exec`` of a
threaded process, which can deadlock.
"""

from __future__ import annotations

import ctypes
import os
import sys
from collections.abc import Iterable, Sequence

_SYS_CREATE_RULESET = 444
_SYS_ADD_RULE = 445
_SYS_RESTRICT_SELF = 446
_CREATE_RULESET_VERSION = 1
_RULE_PATH_BENEATH = 1
_PR_SET_NO_NEW_PRIVS = 38

EXECUTE = 1 << 0
WRITE_FILE = 1 << 1
READ_FILE = 1 << 2
READ_DIR = 1 << 3
REMOVE_DIR = 1 << 4
REMOVE_FILE = 1 << 5
MAKE_CHAR = 1 << 6
MAKE_DIR = 1 << 7
MAKE_REG = 1 << 8
MAKE_SOCK = 1 << 9
MAKE_FIFO = 1 << 10
MAKE_BLOCK = 1 << 11
MAKE_SYM = 1 << 12
REFER = 1 << 13  # ABI 2
TRUNCATE = 1 << 14  # ABI 3
IOCTL_DEV = 1 << 15  # ABI 5

#: The rights that apply to a file rather than a directory; a rule on a file may
#: grant only these (the kernel answers EINVAL otherwise).
_FILE_RIGHTS = EXECUTE | WRITE_FILE | READ_FILE | TRUNCATE | IOCTL_DEV
READ = EXECUTE | READ_FILE | READ_DIR
WRITE = (
    READ | WRITE_FILE | REMOVE_DIR | REMOVE_FILE | MAKE_DIR | MAKE_REG | MAKE_SYM | REFER | TRUNCATE
)


class _PathBeneath(ctypes.Structure):
    _pack_ = 1
    _fields_ = (("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32))


def _libc() -> ctypes.CDLL:
    return ctypes.CDLL(None, use_errno=True)


def _handled(abi: int) -> int:
    """Every filesystem right this kernel's Landlock knows: one it does not handle is
    allowed everywhere, so handling all of them is what makes the list exhaustive."""
    rights = (1 << 13) - 1
    if abi >= 2:
        rights |= REFER
    if abi >= 3:
        rights |= TRUNCATE
    if abi >= 5:
        rights |= IOCTL_DEV
    return rights


def abi_version() -> int:
    """The kernel's Landlock ABI, or 0 when it has none (not built, not enabled, or a
    seccomp profile that refuses the call)."""
    try:
        version = _libc().syscall(_SYS_CREATE_RULESET, None, 0, _CREATE_RULESET_VERSION)
    except (AttributeError, OSError):
        return 0
    return max(int(version), 0)


def confine(read: Iterable[str], write: Iterable[str]) -> None:
    """Restrict this process, and everything it ``exec``s, to reading ``read`` and
    reading and writing ``write``. A path that does not exist is left out. Raises
    OSError when the kernel refuses; never returns unconfined."""
    abi = abi_version()
    if abi < 1:
        raise OSError("Landlock is not available")
    libc = _libc()
    handled = _handled(abi)
    attr = ctypes.c_uint64(handled)
    ruleset = libc.syscall(_SYS_CREATE_RULESET, ctypes.byref(attr), ctypes.sizeof(attr), 0)
    if ruleset < 0:
        raise OSError(ctypes.get_errno(), "landlock_create_ruleset")
    try:
        for paths, access in ((read, READ), (write, WRITE)):
            for path in paths:
                try:
                    fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
                except OSError:
                    continue
                try:
                    allowed = access & handled
                    if not os.path.isdir(f"/proc/self/fd/{fd}"):
                        allowed &= _FILE_RIGHTS
                    rule = _PathBeneath(allowed, fd)
                    if libc.syscall(
                        _SYS_ADD_RULE, ruleset, _RULE_PATH_BENEATH, ctypes.byref(rule), 0
                    ):
                        raise OSError(ctypes.get_errno(), f"landlock_add_rule {path}")
                finally:
                    os.close(fd)
        if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0):
            raise OSError(ctypes.get_errno(), "prctl(PR_SET_NO_NEW_PRIVS)")
        if libc.syscall(_SYS_RESTRICT_SELF, ruleset, 0):
            raise OSError(ctypes.get_errno(), "landlock_restrict_self")
    finally:
        os.close(ruleset)


def command(argv: Sequence[str], *, read: Iterable[str], write: Iterable[str]) -> list[str]:
    """``argv`` run through this launcher, confined to ``read`` and ``write``."""
    launcher = [sys.executable, "-I", "-S", os.path.abspath(__file__)]
    for path in read:
        launcher += ["--read", str(path)]
    for path in write:
        launcher += ["--write", str(path)]
    return [*launcher, "--", *argv]


def main(args: Sequence[str]) -> None:
    read: list[str] = []
    write: list[str] = []
    index = 0
    while index < len(args) and args[index] != "--":
        flag, value = args[index], args[index + 1]
        (read if flag == "--read" else write).append(value)
        index += 2
    argv = list(args[index + 1 :])
    try:
        confine(read, write)
    except OSError as error:
        # Never fall through to an unconfined run: the caller only launches this
        # when the kernel said it could confine.
        print(f"ERROR: the openscad sandbox could not be applied: {error}", file=sys.stderr)
        sys.exit(126)
    os.execv(argv[0], argv)


if __name__ == "__main__":
    main(sys.argv[1:])
