import pytest

from scadbuddy.activity.kinds import AgentKeyCollisionError, discover_kinds, load_agent_kinds
from scadbuddy.activity.models import ActivityKind
from tests.conftest import PgPool


def test_operations_yield_kinds_and_agent_rows_are_data() -> None:
    reg = discover_kinds(
        ["model_import"],
        [
            {
                "key": "agent.plugin_package_install",
                "label": "Install plugin package",
                "facets": [{"name": "package", "type": "keyword", "label": "Package"}],
                "retired_at": None,
            },
        ],
    )
    assert reg.get("operation.model_import") is not None
    agent = reg.get("agent.plugin_package_install")
    assert agent is not None and agent.label == "Install plugin package"
    assert reg.facet("agent.plugin_package_install", "package") is not None
    assert reg.facet("agent.plugin_package_install", "nope") is None
    assert reg.get("missing") is None


def test_render_kind_is_discovered() -> None:
    assert discover_kinds([], []).get("render") is not None


def test_code_key_with_agent_prefix_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    import scadbuddy.activity.kinds as kinds

    monkeypatch.setattr(
        kinds, "feature_exports", lambda module, attr: [(ActivityKind(key="agent.x", label="x"),)]
    )
    with pytest.raises(AgentKeyCollisionError):
        discover_kinds([], [])


def test_feature_declared_operation_kind_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    import scadbuddy.activity.kinds as kinds

    own = ActivityKind(key="operation.model_import", label="Import a model")
    monkeypatch.setattr(kinds, "feature_exports", lambda module, attr: [(own,)])
    reg = discover_kinds(["model_import"], [])
    assert reg.get("operation.model_import") is own
    assert len(reg.all()) == 1


def test_duplicate_code_keys_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    import scadbuddy.activity.kinds as kinds

    k = ActivityKind(key="x", label="x")
    monkeypatch.setattr(kinds, "feature_exports", lambda module, attr: [(k,), (k,)])
    with pytest.raises(ValueError, match="two activity kinds"):
        discover_kinds([], [])


def test_agent_rows_outside_prefix_are_ignored() -> None:
    reg = discover_kinds([], [{"key": "other.x", "label": "x", "facets": [], "retired_at": None}])
    assert reg.get("other.x") is None


def test_load_agent_kinds_without_the_agents_tables(pg_pool: PgPool) -> None:
    with pg_pool.connection() as conn:
        assert load_agent_kinds(conn) == []


def test_load_agent_kinds_reads_rows(pg_pool: PgPool) -> None:
    with pg_pool.connection() as conn:
        conn.execute(
            "CREATE TABLE ai_activity_kinds (key text PRIMARY KEY, label text NOT NULL,"
            " facets jsonb NOT NULL, retired_at timestamptz)"
        )
        conn.execute(
            "INSERT INTO ai_activity_kinds VALUES ('agent.b','B','[]',now()),"
            ' (\'agent.a\',\'A\',\'[{"name":"p","type":"keyword","label":"P"}]\',NULL)'
        )
        rows = load_agent_kinds(conn)
    assert [r["key"] for r in rows] == ["agent.a", "agent.b"]
    assert rows[0]["facets"][0]["name"] == "p"
    assert rows[1]["retired_at"] is not None
