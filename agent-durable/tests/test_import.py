import importlib.metadata


def test_the_plugin_resolves_from_temporals_repository() -> None:
    dist = importlib.metadata.distribution("temporalio-claude-agent-sdk")
    direct_url = dist.read_text("direct_url.json") or ""
    assert "github.com/temporalio/ai-integrations" in direct_url
    assert "b1cf3848b15ad5cd1f009bd19524e3f751140439" in direct_url


def test_the_python_sdk_is_the_pinned_one() -> None:
    assert importlib.metadata.version("claude-agent-sdk") == "0.2.164"
