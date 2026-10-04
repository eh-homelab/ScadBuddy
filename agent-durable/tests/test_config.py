import pytest

from scadbuddy_durable.config import ENV_VARS, ConfigError, load_config


def test_defaults_and_names() -> None:
    cfg = load_config({})
    assert cfg.database_url is None and cfg.temporal_address is None
    assert cfg.temporal_namespace == "scadbuddy"
    assert cfg.health_port == 8082
    assert set(ENV_VARS) == {
        "SCADBUDDY_DATABASE_URL",
        "SCADBUDDY_SECRET_KEY_FILE",
        "SCADBUDDY_SECRET_KEY_PREVIOUS_FILE",
        "SCADBUDDY_TEMPORAL_ADDRESS",
        "SCADBUDDY_TEMPORAL_NAMESPACE",
        "SCADBUDDY_AGENT_DURABLE_HEALTH_PORT",
    }


def test_blank_is_unset_and_bad_values_refused() -> None:
    assert load_config({"SCADBUDDY_TEMPORAL_ADDRESS": "  "}).temporal_address is None
    with pytest.raises(ConfigError):
        load_config({"SCADBUDDY_TEMPORAL_ADDRESS": "host :7233"})
    with pytest.raises(ConfigError):
        load_config({"SCADBUDDY_AGENT_DURABLE_HEALTH_PORT": "http"})
