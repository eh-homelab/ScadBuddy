"""An audit row's actor names the flow run the agent's call was made from (#4252, orders
spec §7.4), from the author headers, never a body."""

from scadbuddy.core.authorship import AgentAuthor, authored_as
from scadbuddy.orders.actor import current_actor
from scadbuddy.orders.models import Actor


def test_an_agent_call_from_a_flow_run_names_the_run() -> None:
    with authored_as(AgentAuthor("token:t1", "s1", flow_run="run-1")):
        assert current_actor() == Actor(
            kind="agent", principal="token:t1", session="s1", run_id="run-1"
        )
    with authored_as(AgentAuthor("token:t1", "s1")):
        assert current_actor().run_id is None
    assert current_actor() == Actor(kind="browser")
