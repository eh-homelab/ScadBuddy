"""Build-time assertion, run by the Dockerfile's `agent-durable` stage.

    python -m scadbuddy_durable.check_cli_version

Fails when the Python claude-agent-sdk's bundled Claude Code binary is missing for
this platform, prints an unrecognised --version, differs from the version the SDK
declares, or is older than the plugin's floor (ai-integrations#33 README: 2.1.273).
The version is pinned once, by `claude-agent-sdk==` in pyproject.toml, as the agent
pins its own (agent/src/check-cli-version.ts, #1540).
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

import claude_agent_sdk
from claude_agent_sdk._cli_version import __cli_version__

MINIMUM = (2, 1, 273)
_LINE = re.compile(r"^(\d+\.\d+\.\d+\S*) \(Claude Code\)$", re.MULTILINE)


def declared_cli_version() -> str:
    return str(__cli_version__)


def bundled_cli_path() -> Path:
    return Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"


def parse_cli_version(stdout: str) -> str:
    match = _LINE.search(stdout.strip())
    if not match:
        raise ValueError(f"unrecognised claude --version output: {stdout!r}")
    return match.group(1)


def main() -> int:
    declared = declared_cli_version()
    path = bundled_cli_path()
    if not path.is_file():
        print(f"ERROR: no bundled Claude Code at {path} (declared {declared}).", file=sys.stderr)
        return 1
    with tempfile.TemporaryDirectory(prefix="claude-version-") as config_dir:
        out = subprocess.run(
            [str(path), "--version"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            env={"CLAUDE_CONFIG_DIR": config_dir, "HOME": config_dir, "PATH": "/usr/bin:/bin"},
        )
    try:
        actual = parse_cli_version(out.stdout)
    except ValueError as err:
        print(f"ERROR: {err} (declared {declared}).", file=sys.stderr)
        return 1
    if actual != declared:
        print(
            f"ERROR: claude-agent-sdk declares Claude Code {declared!r} but its binary reports"
            f" {actual!r}. Reinstall from agent-durable/uv.lock for this platform.",
            file=sys.stderr,
        )
        return 1
    if tuple(int(p) for p in actual.split(".")[:3]) < MINIMUM:
        print(
            f"ERROR: Claude Code {actual} is older than the plugin's floor {MINIMUM}.",
            file=sys.stderr,
        )
        return 1
    print(f"Claude Code {actual} (bundled by claude-agent-sdk) is the version the SDK declares.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
