"""#1807: job, print-run and operation ids are one id format, so one pattern."""

import typing
import uuid

from scadbuddy.api import deps


def _pattern(alias: object) -> str:
    field = typing.get_args(alias)[1]
    pattern: str = next(m.pattern for m in field.metadata if getattr(m, "pattern", None))
    return pattern


def test_job_run_and_operation_ids_share_one_pattern() -> None:
    aliases = (deps.JobIdPath, deps.RunIdPath, deps.OperationIdPath)
    assert {_pattern(a) for a in aliases} == {deps.HEX_ID_PATTERN}
    for name in ("JOB_ID_PATTERN", "RUN_ID_PATTERN", "OPERATION_ID_PATTERN"):
        assert not hasattr(deps, name), f"{name} would drift from HEX_ID_PATTERN"


def test_the_pattern_accepts_a_minted_id() -> None:
    import re

    assert re.fullmatch(deps.HEX_ID_PATTERN, uuid.uuid4().hex)
    assert not re.fullmatch(deps.HEX_ID_PATTERN, uuid.uuid4().hex.upper())
