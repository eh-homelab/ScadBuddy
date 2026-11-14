"""The check's launcher (#6133, spec 2026-11-13 §7):
``python -I -S confine.py [--strict] <hide> -- <program> [args...]``.

It makes itself non-dumpable (``PR_SET_DUMPABLE`` 0, lifted only for the three writes of
its id maps, which a non-dumpable process cannot open), confines itself, then forks the
parser as PID 1 of a PID namespace of its own and reaps it, exiting with its code (128 plus
the signal when one killed it). Both stay in the runner's process group, which a kill
reaches whole:

1. ``unshare(CLONE_NEWUSER | CLONE_NEWNET | CLONE_NEWNS | CLONE_NEWPID)``, the process's own
   uid and gid mapped to themselves
   ([user_namespaces(7)](https://man7.org/linux/man-pages/man7/user_namespaces.7.html)): a
   network namespace with only a loopback device that is down
   ([network_namespaces(7)](https://man7.org/linux/man-pages/man7/network_namespaces.7.html)),
   and a mount namespace where ``/`` is made private and an empty tmpfs (mode 0) covers
   ``hide`` (the spool). Its working directory, its own job's box, is bind-mounted onto
   ``<hide>/box`` inside that tmpfs and the process moves there, so ``..`` leads into the
   tmpfs, never back down the real spool to another job: a working directory left below
   the covered spool would still reach its siblings by ``..``
   ([mount_namespaces(7)](https://man7.org/linux/man-pages/man7/mount_namespaces.7.html);
   measured by `tests/intake_worker/test_runner.py`
   `test_a_confined_child_reaches_only_its_own_box`). The forked parser is PID 1 of its
   own PID namespace, and an empty tmpfs covers ``/proc``, so it sees no process, not even
   its launcher
   ([pid_namespaces(7)](https://man7.org/linux/man-pages/man7/pid_namespaces.7.html)). No
   fresh ``/proc`` is mounted: a container's masked ``/proc`` paths make the kernel refuse
   one in a user namespace, and the parser needs none.
2. ``prctl(PR_SET_NO_NEW_PRIVS, 1)``
   ([prctl(2)](https://man7.org/linux/man-pages/man2/prctl.2.html)).
3. A seccomp filter that refuses (``EPERM``) every call that could undo step 1 or reach
   another process: ``mount``, ``umount2``, ``pivot_root``, ``chroot``, the new mount API,
   ``unshare``, ``setns``, ``clone`` with a namespace flag, ``ptrace``,
   ``process_vm_readv``, ``process_vm_writev``, ``kcmp``, ``pidfd_open``, ``pidfd_getfd``,
   ``pidfd_send_signal``, ``keyctl``, ``add_key``, ``request_key``, ``bpf``,
   ``perf_event_open`` and ``userfaultfd``; ``clone3``
   and ``io_uring_setup`` are ``ENOSYS`` (``clone3``'s flags sit in memory a filter cannot
   read, and libc falls back to ``clone``), and a process of another ABI is killed
   ([seccomp(2)](https://man7.org/linux/man-pages/man2/seccomp.2.html)).
4. ``execv`` of the parser, in the forked PID 1 (the launcher takes steps 2 and 3 too).
   The creator of a user namespace holds every capability in it,
   enough to unmount the tmpfs; an ``execve`` by a uid that is not 0 in the namespace
   clears them all (capabilities(7), "Capabilities and execution of programs by root" and
   "Effect of user ID changes on capabilities":
   <https://man7.org/linux/man-pages/man7/capabilities.7.html>), so the parser runs with
   none. The uid is the caller's own, never 0: ``--strict`` refuses root.

With ``--strict`` (the sandbox container's) any refused step exits 126 without running the
program: no parse runs unconfined. Without it (``unsafe-local``, development and tests) a
refused step is passed to the program as ``SCADBUDDY_INTAKE_ISOLATION`` and it runs anyway.

Standard library only, run as a script with ``-I -S``: no ``scadbuddy`` import, so the
process is single-threaded at ``unshare(CLONE_NEWUSER)``, which requires it. The filter
exists for the image's two architectures only; elsewhere ``--strict`` refuses to run.
"""

from __future__ import annotations

import ctypes
import os
import platform
import struct
import sys
from pathlib import Path
from typing import NoReturn

CLONE_NEWNS = 0x00020000
CLONE_NEWCGROUP = 0x02000000
CLONE_NEWUTS = 0x04000000
CLONE_NEWIPC = 0x08000000
CLONE_NEWUSER = 0x10000000
CLONE_NEWPID = 0x20000000
CLONE_NEWNET = 0x40000000
_NAMESPACE_FLAGS = (
    CLONE_NEWNS
    | CLONE_NEWCGROUP
    | CLONE_NEWUTS
    | CLONE_NEWIPC
    | CLONE_NEWUSER
    | CLONE_NEWPID
    | CLONE_NEWNET
)
MS_NOSUID = 2
MS_NODEV = 4
MS_NOEXEC = 8
MS_BIND = 4096
MS_REC = 0x4000
MS_PRIVATE = 1 << 18

#: The variable that carries a refused step to the program (non-strict only).
ISOLATION_ENV = "SCADBUDDY_INTAKE_ISOLATION"

# The asm-generic numbers past 403 are the same on both architectures.
_NEW_MOUNT_API = (428, 429, 430, 431, 432, 433, 442)  # open_tree … fspick, mount_setattr
_PIDFD = (424, 434, 438)  # pidfd_send_signal, pidfd_open, pidfd_getfd
_NR_IO_URING_SETUP = 425
_NR_CLONE3 = 435

#: ``platform.machine()`` → (``AUDIT_ARCH_*``, ``__NR_clone``, the refused calls: mount,
#: umount2, pivot_root, chroot, unshare, setns, ptrace, keyctl, add_key, request_key, bpf,
#: perf_event_open, userfaultfd, process_vm_readv, process_vm_writev, kcmp).
ARCHES: dict[str, tuple[int, int, tuple[int, ...]]] = {
    "x86_64": (
        0xC000003E,
        56,
        (165, 166, 155, 161, 272, 308, 101, 250, 248, 249, 321, 298, 323, 310, 311, 312),
    ),
    "aarch64": (
        0xC00000B7,
        220,
        (40, 39, 41, 51, 97, 268, 117, 219, 217, 218, 280, 241, 282, 270, 271, 272),
    ),
}
_X32_SYSCALL_BIT = 0x40000000
_EPERM = 1
_ENOSYS = 38

# Classic BPF over `struct seccomp_data` (nr at 0, arch at 4, args[0] at 16).
_LD_W_ABS = 0x20
_JEQ_K = 0x15
_JGE_K = 0x35
_AND_K = 0x54
_RET_K = 0x06
_RET_ALLOW = 0x7FFF0000
_RET_KILL_PROCESS = 0x80000000
_RET_ERRNO = 0x00050000
_PR_SET_DUMPABLE = 4
_PR_SET_NO_NEW_PRIVS = 38
_PR_SET_SECCOMP = 22
_SECCOMP_MODE_FILTER = 2


def _op(code: int, k: int, jt: int = 0, jf: int = 0) -> bytes:
    return struct.pack("=HBBI", code, jt, jf, k)


def program(arch: int, nr_clone: int, refused: tuple[int, ...]) -> list[bytes]:
    """The filter: wrong ABI → kill; x32, ``clone3`` and ``io_uring_setup`` → ``ENOSYS``;
    each refused call → ``EPERM``; ``clone`` with a namespace flag in ``flags`` (``args[0]``
    on both architectures, its low 32 bits at offset 16 on a little-endian machine, where
    every namespace flag sits) → ``EPERM``; anything else → allowed."""
    ops = [
        _op(_LD_W_ABS, 4),
        _op(_JEQ_K, arch, jt=1),
        _op(_RET_K, _RET_KILL_PROCESS),
        _op(_LD_W_ABS, 0),
        _op(_JGE_K, _X32_SYSCALL_BIT, jf=1),
        _op(_RET_K, _RET_ERRNO | _ENOSYS),
    ]
    for nr in (_NR_IO_URING_SETUP, _NR_CLONE3):
        ops += [_op(_JEQ_K, nr, jf=1), _op(_RET_K, _RET_ERRNO | _ENOSYS)]
    for nr in (*refused, *_NEW_MOUNT_API, *_PIDFD):
        ops += [_op(_JEQ_K, nr, jf=1), _op(_RET_K, _RET_ERRNO | _EPERM)]
    ops += [
        _op(_JEQ_K, nr_clone, jt=1),
        _op(_RET_K, _RET_ALLOW),
        _op(_LD_W_ABS, 16),
        _op(_AND_K, _NAMESPACE_FLAGS),
        _op(_JEQ_K, 0, jf=1),
        _op(_RET_K, _RET_ALLOW),
        _op(_RET_K, _RET_ERRNO | _EPERM),
    ]
    return ops


class _SockFprog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.c_void_p)]


def _libc() -> ctypes.CDLL:
    libc = ctypes.CDLL(None, use_errno=True)
    libc.mount.argtypes = [
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_ulong,
        ctypes.c_char_p,
    ]
    libc.prctl.argtypes = [
        ctypes.c_int,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
    ]
    return libc


def _refused(call: str) -> str:
    return f"{call}: {os.strerror(ctypes.get_errno())}"


def _write(path: str, text: str) -> None:
    with open(path, "w") as out:
        out.write(text)


def isolate(libc: ctypes.CDLL, hide: str) -> str | None:
    """Step 1 but the parser's own ``/proc`` (`hide_proc`, in the forked PID 1); None when
    all of it took, else what the kernel refused."""
    uid, gid = os.getuid(), os.getgid()
    if libc.unshare(CLONE_NEWUSER | CLONE_NEWNET | CLONE_NEWNS | CLONE_NEWPID) != 0:
        return _refused("unshare(CLONE_NEWUSER | CLONE_NEWNET | CLONE_NEWNS | CLONE_NEWPID)")
    # A non-dumpable process's /proc files belong to root, which the new namespace does
    # not map, so the maps are written dumpable; nothing else of the process is open then.
    libc.prctl(_PR_SET_DUMPABLE, 1, None, 0, 0)
    try:
        _write("/proc/self/setgroups", "deny")
        _write("/proc/self/uid_map", f"{uid} {uid} 1")
        _write("/proc/self/gid_map", f"{gid} {gid} 1")
    except OSError as error:
        return f"the user namespace's id maps: {error.strerror}"
    finally:
        libc.prctl(_PR_SET_DUMPABLE, 0, None, 0, 0)
    if libc.mount(None, b"/", None, MS_REC | MS_PRIVATE, None) != 0:
        return _refused("mount(MS_REC | MS_PRIVATE)")
    flags = MS_NOSUID | MS_NODEV | MS_NOEXEC
    # Opened in the new mount namespace (unshare moved the working directory into it): a
    # bind mount's source must be a mount of the caller's own namespace.
    box = os.open(".", os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        if libc.mount(b"tmpfs", hide.encode(), b"tmpfs", flags, b"size=4k,mode=0") != 0:
            return _refused("mount(tmpfs over the spool)")
        inner = os.path.join(hide, "box")
        try:
            os.mkdir(inner, 0o700)
        except OSError as error:
            return f"the box's mount point: {error.strerror}"
        source = f"/proc/self/fd/{box}".encode()
        if libc.mount(source, inner.encode(), None, MS_BIND, None) != 0:
            return _refused("mount(the box, MS_BIND)")
        os.chdir(inner)
    finally:
        os.close(box)
    return None


def hide_proc(libc: ctypes.CDLL) -> str | None:
    """An empty tmpfs over ``/proc``: the parser sees no process."""
    flags = MS_NOSUID | MS_NODEV | MS_NOEXEC
    if libc.mount(b"tmpfs", b"/proc", b"tmpfs", flags, b"size=4k,mode=0") != 0:
        return _refused("mount(tmpfs over /proc)")
    return None


def forbid_dumping(libc: ctypes.CDLL) -> str | None:
    if libc.prctl(_PR_SET_DUMPABLE, 0, None, 0, 0) != 0:
        return _refused("prctl(PR_SET_DUMPABLE, 0)")
    return None


def restrict(libc: ctypes.CDLL) -> str | None:
    """Steps 2 and 3; None when both took, else what was refused."""
    if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, None, 0, 0) != 0:
        return _refused("prctl(PR_SET_NO_NEW_PRIVS)")
    if sys.platform != "linux" or platform.machine() not in ARCHES or sys.byteorder != "little":
        return f"no seccomp filter for {sys.platform}/{platform.machine()}"
    filters = program(*ARCHES[platform.machine()])
    code = ctypes.create_string_buffer(b"".join(filters), 8 * len(filters))
    fprog = _SockFprog(len(filters), ctypes.cast(code, ctypes.c_void_p))
    if libc.prctl(_PR_SET_SECCOMP, _SECCOMP_MODE_FILTER, ctypes.byref(fprog), 0, 0) != 0:
        return _refused("prctl(PR_SET_SECCOMP)")
    return None


def _fail(message: str) -> NoReturn:
    sys.stderr.write(f"confine: {message}\n")
    raise SystemExit(126)


def main(argv: list[str]) -> NoReturn:
    strict = argv[:1] == ["--strict"]
    rest = argv[1:] if strict else argv
    if len(rest) < 3 or rest[1] != "--":
        _fail("usage: confine.py [--strict] <hide> -- <program> [args...]")
    hide, command = rest[0], rest[2:]
    if strict and os.getuid() == 0:
        _fail("refusing to run the parser as root: its exec would keep its capabilities")
    libc = _libc()
    refused = forbid_dumping(libc) or isolate(libc, hide)
    if refused is not None:
        if strict:
            _fail(refused)
        # Development only: the parser runs unconfined but for what still applies.
        os.environ[ISOLATION_ENV] = refused
        restrict(libc)
        _exec(command)
    child = os.fork()
    if child == 0:
        # PID 1 of the new PID namespace.
        refused = hide_proc(libc) or restrict(libc)
        if refused is not None:
            if strict:
                _fail(refused)
            os.environ[ISOLATION_ENV] = refused
        _exec(command)
    # The launcher holds the namespaces' capabilities while it waits: it takes the same
    # filter, and the parser, in a PID namespace below it with no /proc, cannot see it.
    if (refused := restrict(libc)) is not None and strict:
        os.kill(child, 9)
        _fail(refused)
    _, status = os.waitpid(child, 0)
    code = os.waitstatus_to_exitcode(status)
    raise SystemExit(code if code >= 0 else 128 - code)


def _exec(command: list[str]) -> NoReturn:
    try:
        os.execv(command[0], command)
    except OSError as error:
        _fail(f"{command[0]}: {error.strerror}")


def command(hide: str, program: list[str], *, strict: bool) -> list[str]:
    """The argv that runs ``program`` confined."""
    return [
        sys.executable,
        "-I",
        "-S",
        str(Path(__file__).resolve()),
        *(["--strict"] if strict else []),
        hide,
        "--",
        *program,
    ]


if __name__ == "__main__":
    main(sys.argv[1:])
