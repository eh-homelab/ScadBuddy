"""Build-time assertion, run by the Dockerfile's `agent-durable` stage:

    python scripts/check_cli_version.py "$CLAUDE_CODE_VERSION"

Fails when the Claude Code version the Python SDK declares, or the bundled binary's own
`--version`, differs from the pin (the Python twin of agent/src/check-cli-version.ts).
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

import claude_agent_sdk
from claude_agent_sdk._cli_version import __cli_version__


def bundled_version() -> str:
    binary = Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"
    with tempfile.TemporaryDirectory(prefix="claude-version-") as config_dir:
        out = subprocess.run(
            [str(binary), "--version"],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
            env={"PATH": "/usr/bin:/bin", "HOME": config_dir, "CLAUDE_CONFIG_DIR": config_dir},
        )
    return out.stdout.strip()


def main(argv: Sequence[str]) -> int:
    if len(argv) != 1 or not argv[0]:
        print("usage: check_cli_version.py <expected Claude Code version>", file=sys.stderr)
        return 2
    expected = argv[0]
    actual = bundled_version()
    if __cli_version__ != expected or not actual.startswith(expected):
        print(
            f"ERROR: the Agent SDK declares Claude Code '{__cli_version__}' and its binary reports "
            f"'{actual}'; this build pins '{expected}'.\n"
            "       Re-verify the harness facts in the AI design spec, then bump the pin.",
            file=sys.stderr,
        )
        return 1
    print(f"Claude Code {actual} (bundled by the Agent SDK) matches the pin.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
