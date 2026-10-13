"""A flow's `render`, `save_output`, `queue_print` and `arrange` (#1057, plan
2026-10-09-durable-phase-6-flows.md Task C3, Ruling 5): each calls ScadBuddy's own
route, here a fake of the routes' shapes; `queue_print` and `arrange` wait for a person's
approval first, and are denied when the run's approval timeout passes."""

import os
import re
from typing import Any

import pytest
from temporal_agent_harness.harness.agent_client import AgentClient

from scadbuddy.render.projection import JobProjection
from scadbuddy.workflows.flow_routes import route_key
from scadbuddy.workflows.flow_tools import outward_activity_id
from tests.flows.fake_api import FakeApi, Outward, outward_worker
from tests.flows.flows_support import script
from tests.flows.test_project_replay import HISTORIES

pytestmark = [pytest.mark.requires_temporal, pytest.mark.requires_postgres]

PLAN: dict[str, Any] = {"filament_plan": {"slots": []}, "choices": {}}


async def test_render_follows_its_job_until_it_settles(outward: Outward, api: FakeApi) -> None:
    run_id = await outward.run(script("r = await render('box', {'width': 20})", "return r"))
    run = await outward.finished(run_id)
    assert run.status == "succeeded", run.result
    assert "job-box" in (run.result or "") and "'done'" in (run.result or "")
    [(body, key)] = api.to("/api/v1/models/box/render")
    assert body == {"params": {"width": 20}}
    assert key is not None and re.fullmatch(r"[0-9a-f]{32}", key)
    assert [(s.fn, s.outward, s.status) for s in run.steps] == [("render", False, "succeeded")]


async def test_a_refusal_reaches_the_script_in_the_routes_words(
    outward: Outward, api: FakeApi
) -> None:
    run_id = await outward.run(
        script(
            "try:",
            "    await render('missing', {})",
            "except Exception as e:",
            "    return str(e)",
        )
    )
    run = await outward.finished(run_id)
    assert "no model 'missing'" in (run.result or "")
    assert [(s.fn, s.status, s.error) for s in run.steps] == [
        ("render", "failed", "RouteRefusedError")
    ]


async def test_save_output_follows_an_operation(outward: Outward) -> None:
    run_id = await outward.run(
        script(
            "a = await save_output('box', 'j1', 'A')",
            "b = await save_output('slow', 'j2', 'B')",
            "return [a, b]",
        )
    )
    run = await outward.finished(run_id)
    assert run.result == "result: ['out-j1', 'o-9']"


async def test_print_waits_for_approval_then_runs_once_under_its_key(
    outward: Outward, api: FakeApi
) -> None:
    api.accepting = 1
    run_id = await outward.run(
        script(f"o = await queue_print({{'output_id': 'o1'}}, {PLAN!r})", "return o['status']")
    )
    call_id = await outward.approval(run_id)
    assert api.sent == []
    await outward.decide(run_id, approved=True)
    run = await outward.finished(run_id)
    assert run.result == "result: 'succeeded'"
    assert run.waiting_on == []
    sends = api.to("/api/v1/print/outputs/o1/run")
    # Re-sent once past `command-still-accepting`, with the same key and request id.
    assert len(sends) == 2 and sends[0] == sends[1]
    body, key = sends[0]
    expected = route_key(run_id, run.workflow_run_id, call_id)
    assert (body["request_id"], key) == (expected, expected)
    assert list(api.runs) == [f"run-{expected}"]
    history = await outward.client.get_workflow_handle(f"flow-{run_id}").fetch_history()
    scheduled = [
        e.activity_task_scheduled_event_attributes.activity_id
        for e in history.events
        if e.HasField("activity_task_scheduled_event_attributes")
    ]
    # The one thing a Reset preview can read of an outward send (Ruling 14).
    assert scheduled.count(outward_activity_id("queue_print", call_id)) == 1
    assert [(s.fn, s.outward, s.status) for s in run.steps] == [("queue_print", True, "succeeded")]


async def test_a_print_refusal_fails_its_step(outward: Outward) -> None:
    run_id = await outward.run(
        script(
            "try:",
            "    await queue_print({'output_id': 'o1'}, {'choices': {}})",
            "except Exception as e:",
            "    return str(e)",
        )
    )
    await outward.decide(run_id, approved=True)
    run = await outward.finished(run_id)
    assert "filament_plan: Field required" in (run.result or "")
    assert [(s.fn, s.status) for s in run.steps] == [("queue_print", "failed")]


async def test_a_denied_print_sends_nothing(outward: Outward, api: FakeApi) -> None:
    run_id = await outward.run(
        script(
            "try:",
            f"    await queue_print({{'output_id': 'o1'}}, {PLAN!r})",
            "except Exception as e:",
            "    return str(e)",
        )
    )
    await outward.decide(run_id, approved=False)
    run = await outward.finished(run_id)
    assert (run.result or "").removeprefix("result: ").strip("'\"").startswith("ToolApprovalDenied")
    assert api.sent == []
    assert run.waiting_on == []


async def test_an_undecided_print_is_denied_at_the_runs_timeout(
    outward: Outward, api: FakeApi
) -> None:
    run_id = await outward.run(
        script(
            "try:",
            f"    await queue_print({{'output_id': 'o1'}}, {PLAN!r})",
            "except Exception as e:",
            "    return str(e)",
        ),
        approval_timeout_s=2,
    )
    await outward.approval(run_id)
    run = await outward.finished(run_id)
    assert "timed out" in (run.result or "")
    assert api.sent == []
    assert run.waiting_on == []
    status = await AgentClient(outward.client, f"flow-{run_id}").get_status()
    assert status.pending_approvals == []


async def test_an_approval_inside_the_timeout_is_not_undone(outward: Outward, api: FakeApi) -> None:
    run_id = await outward.run(
        script(
            f"o = await queue_print({{'output_id': 'o1'}}, {PLAN!r})",
            "await sleep(3)",
            "return o['status']",
        ),
        approval_timeout_s=2,
    )
    await outward.decide(run_id, approved=True)
    run = await outward.finished(run_id)
    assert run.result == "result: 'succeeded'"


async def test_arrange_waits_for_approval_and_saves_the_result(
    outward: Outward, api: FakeApi
) -> None:
    run_id = await outward.run(
        script(
            "a = await arrange({'objects': [{'output_id': 'o1'}], 'name': 'Plates'})",
            "return [a['status'], a['output_id']]",
        )
    )
    call_id = await outward.decide(run_id, approved=True)
    run = await outward.finished(run_id)
    assert run.result == "result: ['done', 'out-arr-1']"
    [(saved, save_key)] = api.to("/api/v1/models/box/outputs")
    assert saved == {"job_id": "arr-1", "name": "Plates"}
    [(_, arrange_key)] = api.to("/api/v1/outputs/arrange")
    assert arrange_key == route_key(run_id, run.workflow_run_id, call_id)
    assert save_key == route_key(run_id, run.workflow_run_id, call_id + ":save")


async def test_without_an_api_url_the_call_names_the_setting(
    temporal_address: str, jobs: JobProjection
) -> None:
    async for flows in outward_worker(temporal_address, jobs, None):
        run_id = await flows.run(
            script(
                "try:", "    await render('box', {})", "except Exception as e:", "    return str(e)"
            )
        )
        run = await flows.finished(run_id)
        assert "SCADBUDDY_API_INTERNAL_URL" in (run.result or "")


async def test_a_script_cannot_steer_a_call_to_another_route(
    outward: Outward, api: FakeApi
) -> None:
    """A slug or id from the script is one path segment: anything that would reach
    another route is refused before a send, and the rest arrives quoted, intact."""
    run_id = await outward.run(
        script(
            "out = []",
            "for slug in ['x/../../jobs/j', '..', 'a\\\\b', '']:",
            "    try:",
            "        await render(slug, {})",
            "    except Exception as e:",
            "        out.append(type(e).__name__)",
            "try:",
            "    await queue_print({'output_id': '../x'}, {})",
            "except Exception as e:",
            "    out.append(type(e).__name__)",
            "r = await render('a?b#c%2Fd', {})",
            "out.append(r['job_id'])",
            "return out",
        )
    )
    run = await outward.finished(run_id)
    # The render route got the slug whole (its job id carries it), and that id came
    # back through the job route intact too.
    assert run.result == "result: " + repr(["ValueError"] * 5 + ["job-a?b#c%2Fd"])
    assert len(api.sent) == 1


async def test_printed(outward: Outward, api: FakeApi) -> None:
    """Records `project_workflow_histories/printed.json` (test_project_replay.py): a
    render, its output saved, and a print approved inside its approval timeout."""
    run_id = await outward.run(
        script(
            "r = await render('box', {'width': 20})",
            "o = await save_output('box', r['job_id'], 'Box')",
            "p = await queue_print({'output_id': o}, " + repr(PLAN) + ")",
            "return p['status']",
        ),
        approval_timeout_s=600,
    )
    await outward.decide(run_id, approved=True)
    run = await outward.finished(run_id)
    assert run.result == "result: 'succeeded'"
    if os.environ.get("SCADBUDDY_RECORD_HISTORIES") == "1":
        handle = outward.client.get_workflow_handle(f"flow-{run_id}")
        (HISTORIES / "printed.json").write_text((await handle.fetch_history()).to_json())
