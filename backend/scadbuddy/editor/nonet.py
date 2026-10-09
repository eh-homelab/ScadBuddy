"""Run a program with no network: ``python -I -S nonet.py <program> [args...]``.

The editor's language server (#95) is a process a browser socket starts, so it is
given no way to open a network socket. A seccomp filter refuses ``socket()`` for every
family but ``AF_UNIX`` (``EAFNOSUPPORT``) and ``io_uring_setup`` (``ENOSYS``; an
io_uring could open a socket without the ``socket`` syscall), then the program is
``execv``'d in place, so its pid is this process's and a kill reaches it. With
``PR_SET_NO_NEW_PRIVS`` the filter needs no privilege and is kept across ``execve`` and by
every child: unlike ``unshare -n``, which a container's default seccomp profile refuses,
it works in the image as an ordinary user.

Standard library only, and run as a script with ``-I -S``: no ``scadbuddy`` import, so
starting it costs an interpreter and nothing else.

Only the image's two architectures have a filter. Anywhere else this exits with an error
without running the program (the editor then runs without a language server): the
syscall numbers differ per architecture, and a filter built for the wrong one would let
everything through. A process of another ABI is killed by the filter for the same reason,
and x86_64's x32 syscalls are refused.
"""

from __future__ import annotations

import ctypes
import os
import platform
import struct
import sys
from pathlib import Path
from typing import NoReturn

#: ``platform.machine()`` → (``AUDIT_ARCH_*``, ``__NR_socket``).
ARCHES: dict[str, tuple[int, int]] = {
    "x86_64": (0xC000003E, 41),
    "aarch64": (0xC00000B7, 198),
}
_NR_IO_URING_SETUP = 425  # the same on both (asm-generic numbering past 403)
_X32_SYSCALL_BIT = 0x40000000

_AF_UNIX = 1
_EAFNOSUPPORT = 97
_ENOSYS = 38

# Classic BPF over `struct seccomp_data` (nr at 0, arch at 4, args[0] at 16).
_LD_W_ABS = 0x20
_JEQ_K = 0x15
_JGE_K = 0x35
_RET_K = 0x06
_OFFSET_NR, _OFFSET_ARCH, _OFFSET_ARG0 = 0, 4, 16

_RET_ALLOW = 0x7FFF0000
_RET_KILL_PROCESS = 0x80000000
_RET_ERRNO = 0x00050000

_PR_SET_NO_NEW_PRIVS = 38
_PR_SET_SECCOMP = 22
_SECCOMP_MODE_FILTER = 2


def _op(code: int, k: int, jt: int = 0, jf: int = 0) -> bytes:
    return struct.pack("=HBBI", code, jt, jf, k)


def program(arch: int, nr_socket: int) -> list[bytes]:
    """The filter: wrong ABI → kill; x32 and ``io_uring_setup`` → ``ENOSYS``;
    ``socket(family != AF_UNIX)`` → ``EAFNOSUPPORT``; anything else → allowed. On a
    little-endian machine the low 32 bits of ``args[0]`` sit at offset 16, and the
    family is an ``int``.

    ``socket`` is the one call that makes a network endpoint: ``socketpair`` makes only
    connected local pairs, ``accept`` needs a listening socket the process could not
    make, and it inherits no descriptor (``create_subprocess_exec`` closes them).

    ``AF_UNIX`` stays open on purpose (libc's own lookups use it): "no network" is not
    "no IPC", and the server can still reach a filesystem socket the container
    exposes."""
    return [
        _op(_LD_W_ABS, _OFFSET_ARCH),
        _op(_JEQ_K, arch, jt=1),
        _op(_RET_K, _RET_KILL_PROCESS),
        _op(_LD_W_ABS, _OFFSET_NR),
        _op(_JGE_K, _X32_SYSCALL_BIT, jf=1),
        _op(_RET_K, _RET_ERRNO | _ENOSYS),
        _op(_JEQ_K, _NR_IO_URING_SETUP, jf=1),
        _op(_RET_K, _RET_ERRNO | _ENOSYS),
        _op(_JEQ_K, nr_socket, jt=1),
        _op(_RET_K, _RET_ALLOW),
        _op(_LD_W_ABS, _OFFSET_ARG0),
        _op(_JEQ_K, _AF_UNIX, jf=1),
        _op(_RET_K, _RET_ALLOW),
        _op(_RET_K, _RET_ERRNO | _EAFNOSUPPORT),
    ]


class _SockFprog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.c_void_p)]


def _fail(message: str) -> NoReturn:
    sys.stderr.write(f"nonet: {message}\n")
    raise SystemExit(126)


def main(argv: list[str]) -> NoReturn:
    if len(argv) < 2:
        _fail("usage: nonet.py <program> [args...]")
    if sys.platform != "linux" or platform.machine() not in ARCHES:
        _fail(f"no seccomp filter for {sys.platform}/{platform.machine()}; refusing to run")
    if sys.byteorder != "little":
        _fail("the filter reads args[0] as little-endian")
    arch, nr_socket = ARCHES[platform.machine()]
    filters = program(arch, nr_socket)
    code = ctypes.create_string_buffer(b"".join(filters), 8 * len(filters))
    fprog = _SockFprog(len(filters), ctypes.cast(code, ctypes.c_void_p))
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [
        ctypes.c_int,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
    ]
    if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, None, 0, 0) != 0:
        _fail(f"PR_SET_NO_NEW_PRIVS: {os.strerror(ctypes.get_errno())}")
    if libc.prctl(_PR_SET_SECCOMP, _SECCOMP_MODE_FILTER, ctypes.byref(fprog), 0, 0) != 0:
        _fail(f"PR_SET_SECCOMP: {os.strerror(ctypes.get_errno())}")
    try:
        os.execv(argv[1], argv[1:])
    except OSError as error:
        _fail(f"{argv[1]}: {error.strerror}")


def command(program: str, *args: str) -> list[str]:
    """The argv that runs ``program args...`` under the filter."""
    return [sys.executable, "-I", "-S", str(Path(__file__).resolve()), program, *args]


if __name__ == "__main__":
    main(sys.argv)
