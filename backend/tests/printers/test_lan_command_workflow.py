"""``LanCommand`` and `execute_lan_commands` (#7276, plan 2026-11-25 Task 2, P5, P9, P20):
the commands run in order, the first failure stops the rest, each failure is the route's
problem, and a key sent again reads the first run's outcome. ``lan_command`` is a stub
here; its own tests are `test_lan_command_activity.py`. No printer is contacted."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable
from datetime import timedelta

import pytest
from fastapi import status
from temporalio import activity
from temporalio.client import Client
from temporalio.exceptions import ApplicationError
from temporalio.worker import Worker

from scadbuddy.bambuddy.runs import PrintRunError
from scadbuddy.core.problems import ApiError
from scadbuddy.printers.lan import MQTT_TIMEOUT
from scadbuddy.printers.lan_command_models import (
    LanCommandInput,
    LanCommandReply,
    LanCommandRun,
    command_timeout,
    run_timeout,
)
from scadbuddy.workflows.client import sandboxed_runner
from scadbuddy.workflows.lan_command import (
    LanCommandWorkflow,
    execute_lan_commands,
    lan_command_id,
)
from scadbuddy.workflows.print_models import (
    FAILED,
    LAN_COMMAND_ACTIVITY,
    LAN_NO_ANSWER,
    LAN_REFUSED,
    LAN_UNSURE,
)
from tests.support.temporal import temporal_client

PRINTER = "printer-1"


def command(name: str, kind: str = "write", wait_s: float = 0.1) -> LanCommandInput:
    return LanCommandInput.model_validate(
        {
            "printer_id": PRINTER,
            "name": name,
            "payload": {"print": {"command": name, "sequence_id": "0"}},
            "wait_s": wait_s,
            "kind": kind,
        }
    )


@pytest.fixture
async def client() -> AsyncIterator[Client]:
    async with temporal_client() as client:
        yield client


Answer = Callable[[LanCommandInput], LanCommandReply]


async def _run(
    client: Client, answer: Answer, run: LanCommandRun, *, key: str | None = None, times: int = 1
) -> tuple[list[LanCommandReply | ApiError], list[str]]:
    sent: list[str] = []

    @activity.defn(name=LAN_COMMAND_ACTIVITY)
    async def lan_command(input: LanCommandInput) -> LanCommandReply:
        sent.append(input.name)
        return answer(input)

    queue = f"lan-command-{uuid.uuid4().hex[:8]}"
    outcomes: list[LanCommandReply | ApiError] = []
    async with Worker(
        client,
        task_queue=queue,
        workflows=[LanCommandWorkflow],
        activities=[lan_command],
        workflow_runner=sandboxed_runner(),
    ):
        for _ in range(times):
            try:
                outcome = await execute_lan_commands(client, queue, PRINTER, run, key=key)
                outcomes.extend(outcome.replies)
            except ApiError as error:
                outcomes.append(error)
    return outcomes, sent


def _raises(error: ApplicationError) -> Answer:
    def answer(_: LanCommandInput) -> LanCommandReply:
        raise error

    return answer


def test_a_commands_timeout_is_connect_wait_and_margin() -> None:
    write = command("extrusion_cali_set", wait_s=6.0)
    assert command_timeout(write) == timedelta(seconds=MQTT_TIMEOUT + 6.0 + 2.0)
    read = command("extrusion_cali_get", kind="read", wait_s=5.0)
    # Three tries, each its own connection, and two pauses of 0.5 s.
    assert command_timeout(read) == timedelta(seconds=3 * (MQTT_TIMEOUT + 5.0) + 1.0 + 2.0)
    run = LanCommandRun(commands=[write, read])
    assert run_timeout(run) == command_timeout(write) + command_timeout(read)


def test_the_id_names_the_printer_the_commands_and_the_key() -> None:
    run = LanCommandRun(commands=[command("ams_filament_setting"), command("extrusion_cali_sel")])
    assert (
        lan_command_id(PRINTER, run, "k1")
        == "lan-command-printer-1-ams_filament_setting+extrusion_cali_sel-k1"
    )
    other = LanCommandRun(commands=[command("ams_reset")])
    assert lan_command_id(PRINTER, other, "k1") != lan_command_id(PRINTER, run, "k1")
    assert lan_command_id(PRINTER, run, None) != lan_command_id(PRINTER, run, None)


@pytest.mark.requires_temporal
async def test_the_commands_run_in_order(client: Client) -> None:
    run = LanCommandRun(commands=[command("ams_filament_setting"), command("extrusion_cali_sel")])
    outcomes, sent = await _run(
        client, lambda input: LanCommandReply(confirmed=input.name == "ams_filament_setting"), run
    )
    assert sent == ["ams_filament_setting", "extrusion_cali_sel"]
    assert [o.confirmed for o in outcomes if isinstance(o, LanCommandReply)] == [True, False]


@pytest.mark.requires_temporal
async def test_a_refusal_stops_the_rest_and_is_409(client: Client) -> None:
    run = LanCommandRun(commands=[command("ams_filament_setting"), command("extrusion_cali_sel")])
    refused = ApplicationError(
        "H2C refused ams_filament_setting: ERROR STATE.", type=LAN_REFUSED, non_retryable=True
    )
    [outcome], sent = await _run(client, _raises(refused), run)
    assert sent == ["ams_filament_setting"]
    assert isinstance(outcome, ApiError)
    assert outcome.status == status.HTTP_409_CONFLICT
    assert outcome.extensions["code"] == "printer_refused"
    assert "ERROR STATE" in outcome.detail


@pytest.mark.requires_temporal
@pytest.mark.parametrize(
    ("kind", "code"), [(LAN_NO_ANSWER, "printer_no_answer"), (LAN_UNSURE, "printer_unreachable")]
)
async def test_no_answer_and_unreachable_are_502(client: Client, kind: str, code: str) -> None:
    run = LanCommandRun(commands=[command("extrusion_cali_get", kind="read")])
    failed = ApplicationError("H2C did not answer.", type=kind, non_retryable=True)
    [outcome], _ = await _run(client, _raises(failed), run)
    assert isinstance(outcome, ApiError)
    assert outcome.status == status.HTTP_502_BAD_GATEWAY
    assert outcome.extensions["code"] == code


@pytest.mark.requires_temporal
@pytest.mark.parametrize("problem_status", [409, 503])
async def test_the_printer_rows_refusals_keep_their_status(
    client: Client, problem_status: int
) -> None:
    detail = "This printer is no longer linked over the LAN. Link it again in Settings > Printers."
    problem = PrintRunError(status=problem_status, title="Conflict", detail=detail)
    failed = ApplicationError(detail, problem, type=FAILED, non_retryable=True)
    [outcome], _ = await _run(client, _raises(failed), LanCommandRun(commands=[command("x")]))
    assert isinstance(outcome, ApiError)
    assert outcome.status == problem_status
    assert outcome.detail == detail
    if problem_status == 409:
        assert outcome.extensions["code"] == "not_lan_linked"


@pytest.mark.requires_temporal
async def test_a_resent_key_publishes_once_and_answers_the_first_outcome(client: Client) -> None:
    run = LanCommandRun(commands=[command("extrusion_cali_set")])
    outcomes, sent = await _run(
        client, lambda _: LanCommandReply(confirmed=True), run, key="resend-1", times=2
    )
    assert sent == ["extrusion_cali_set"]
    assert [o.confirmed for o in outcomes if isinstance(o, LanCommandReply)] == [True, True]


@pytest.mark.requires_temporal
async def test_a_resent_key_of_a_failed_run_answers_its_failure(client: Client) -> None:
    run = LanCommandRun(commands=[command("extrusion_cali_set")])
    refused = ApplicationError("refused: busy.", type=LAN_REFUSED, non_retryable=True)
    outcomes, sent = await _run(client, _raises(refused), run, key="resend-2", times=2)
    assert sent == ["extrusion_cali_set"]
    assert [o.extensions["code"] for o in outcomes if isinstance(o, ApiError)] == [
        "printer_refused",
        "printer_refused",
    ]
