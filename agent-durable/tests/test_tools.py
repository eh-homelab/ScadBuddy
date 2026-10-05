from pathlib import Path

from temporalio import activity

from scadbuddy_durable.tools import durable_tools, load_manifest

SAMPLE = Path(__file__).parent / "fixtures/tools.json"


def test_each_tool_is_a_named_stub_on_agent_tools() -> None:
    entries = load_manifest(str(SAMPLE))
    tools = {t.name: t for t in durable_tools(entries)}
    assert set(tools) == {e.name for e in entries}
    for e in entries:
        t = tools[e.name]
        assert t.task_queue == "agent-tools"
        assert t.description == e.description and t.input_schema == e.input_schema
        assert t.needs_approval is (e.tier == "outward")
        defn = activity._Definition.from_callable(t.activity)  # pyright: ignore[reportPrivateUsage]
        assert defn is not None and defn.name == e.name


def test_stubs_are_distinct_callables() -> None:
    tools = durable_tools(load_manifest(str(SAMPLE)))
    assert len({id(t.activity) for t in tools}) == len(tools)
