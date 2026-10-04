from pathlib import Path

PIN = "b1cf3848b15ad5cd1f009bd19524e3f751140439"


def test_the_plugin_is_pinned_to_temporals_repository() -> None:
    lock = (Path(__file__).parents[1] / "uv.lock").read_text()
    repo = "https://github.com/temporalio/ai-integrations?subdirectory=python%2Fclaude_agent_sdk"
    assert f'source = {{ git = "{repo}&rev={PIN}#{PIN}" }}' in lock
    assert 'name = "claude-agent-sdk"\nversion = "0.2.160"' in lock
