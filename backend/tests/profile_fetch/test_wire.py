"""The profile-fetch wire module (#6625): shared by the worker and the CRUD routes, so it
holds no database code."""

from __future__ import annotations

import subprocess
import sys

import pytest

from scadbuddy.profile_fetch import wire


def test_wire_imports_no_database_code() -> None:
    code = (
        "import sys, scadbuddy.profile_fetch.wire; sys.exit(1 if 'psycopg' in sys.modules else 0)"
    )
    done = subprocess.run([sys.executable, "-c", code], check=False)
    assert done.returncode == 0


def test_the_workflow_id_names_the_run() -> None:
    assert wire.fetch_workflow_id("abc") == "profile-fetch:abc"


def test_an_export_failure_reason_is_capped() -> None:
    with pytest.raises(ValueError, match="300"):
        wire.ExportFailure(printer_model="H2C", nozzle="0.4", reason="x" * 301)
    with pytest.raises(ValueError, match="500"):
        wire.FailedAsk(reason="x" * 501)
