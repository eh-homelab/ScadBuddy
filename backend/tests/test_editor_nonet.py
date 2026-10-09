from __future__ import annotations

import json
import platform
import subprocess
import sys
from pathlib import Path

import pytest

from scadbuddy.editor import nonet

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or platform.machine() not in nonet.ARCHES,
    reason="the filter is Linux x86_64/aarch64 only",
)

PROBE = """
import errno, json, socket, sys
results = {}
for name, family, kind in (
    ("unix", socket.AF_UNIX, socket.SOCK_STREAM),
    ("inet", socket.AF_INET, socket.SOCK_STREAM),
    ("inet_udp", socket.AF_INET, socket.SOCK_DGRAM),
    ("inet6", socket.AF_INET6, socket.SOCK_STREAM),
    ("netlink", socket.AF_NETLINK, socket.SOCK_RAW),
    ("packet", socket.AF_PACKET, socket.SOCK_RAW),
):
    try:
        socket.socket(family, kind).close()
        results[name] = "open"
    except OSError as error:
        results[name] = errno.errorcode.get(error.errno, str(error.errno))
print(json.dumps(results))
"""


def test_only_unix_sockets_open_under_the_filter(tmp_path: Path) -> None:
    probe = tmp_path / "probe.py"
    probe.write_text(PROBE, encoding="utf-8")
    done = subprocess.run(
        nonet.command(sys.executable, "-I", str(probe)),
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    assert json.loads(done.stdout) == {
        "unix": "open",
        "inet": "EAFNOSUPPORT",
        "inet_udp": "EAFNOSUPPORT",
        "inet6": "EAFNOSUPPORT",
        "netlink": "EAFNOSUPPORT",
        "packet": "EAFNOSUPPORT",
    }


def test_the_same_probe_opens_them_without_the_filter(tmp_path: Path) -> None:
    """The control: the denials above are the filter's, not this machine's."""
    probe = tmp_path / "probe.py"
    probe.write_text(PROBE, encoding="utf-8")
    done = subprocess.run(
        [sys.executable, "-I", str(probe)], capture_output=True, text=True, check=True, timeout=60
    )
    assert json.loads(done.stdout)["inet"] == "open"


def test_the_program_keeps_its_arguments_and_exit_status() -> None:
    done = subprocess.run(
        nonet.command(sys.executable, "-I", "-c", "import sys; sys.exit(len(sys.argv))", "a", "b"),
        timeout=60,
        check=False,
    )
    assert done.returncode == 3


def test_an_unknown_architecture_runs_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform, "machine", lambda: "riscv64")
    with pytest.raises(SystemExit) as stopped:
        nonet.main(["nonet", "/bin/true"])
    assert stopped.value.code != 0


def test_no_program_runs_nothing() -> None:
    with pytest.raises(SystemExit) as stopped:
        nonet.main(["nonet"])
    assert stopped.value.code != 0
