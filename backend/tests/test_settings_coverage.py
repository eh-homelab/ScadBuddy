"""#322: every setting is either editable in the UI or on the bootstrap allowlist.

A new ``Settings`` field that is neither fails here, so an environment-only setting
cannot slip in: it has to join the env-seeded pattern (the patch, the view, and an
``APPLIES`` entry saying when a change takes effect) or say why the UI cannot set it.
"""

from __future__ import annotations

import pytest

from scadbuddy.api.settings import SettingsView
from scadbuddy.core.settings import (
    APPLIES,
    BOOTSTRAP_FIELDS,
    ENV_SEEDED,
    SECRET_FIELDS,
    Settings,
    check_value,
)
from scadbuddy.library.settings_store import SettingsPatch


def test_every_setting_is_env_seeded_or_bootstrap() -> None:
    assert set(Settings.model_fields) == set(ENV_SEEDED) | set(BOOTSTRAP_FIELDS)
    assert not set(ENV_SEEDED) & set(BOOTSTRAP_FIELDS)


def test_every_bootstrap_field_says_why() -> None:
    assert all(reason.strip() for reason in BOOTSTRAP_FIELDS.values())


def test_every_env_seeded_field_says_when_it_applies() -> None:
    assert set(APPLIES) == set(ENV_SEEDED)


def test_every_env_seeded_field_can_be_saved_and_no_bootstrap_one_can() -> None:
    assert set(ENV_SEEDED) <= set(SettingsPatch.model_fields)
    assert not set(BOOTSTRAP_FIELDS) & set(SettingsPatch.model_fields)


def test_every_env_seeded_field_is_shown_and_no_secret_is() -> None:
    shown = set(SettingsView.model_fields)
    for name in ENV_SEEDED:
        assert (f"has_{name.removeprefix('bambuddy_')}" if name in SECRET_FIELDS else name) in (
            shown
        ), name
    assert not SECRET_FIELDS & shown


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("render_concurrency", 0, "SCADBUDDY_RENDER_CONCURRENCY must be at least 1"),
        ("check_concurrency", 0, "SCADBUDDY_CHECK_CONCURRENCY must be at least 1"),
        ("render_timeout", 0, "SCADBUDDY_RENDER_TIMEOUT must be more than 0"),
        (
            "template_activity_max_timeout",
            0,
            "SCADBUDDY_TEMPLATE_ACTIVITY_MAX_TIMEOUT must be more than 0",
        ),
        ("job_ttl", -1, "SCADBUDDY_JOB_TTL must be more than 0"),
        ("media_upload_max_bytes", 0, "SCADBUDDY_MEDIA_UPLOAD_MAX_BYTES"),
        ("event_log_retention_rows", -1, "SCADBUDDY_EVENT_LOG_RETENTION_ROWS must be at least 0"),
        ("asset_sweep_grace", 60, "SCADBUDDY_ASSET_SWEEP_GRACE must be at least 3600"),
        ("log_level", "loud", "SCADBUDDY_LOG_LEVEL must be one of"),
        ("bambuddy_web_urls", "https://ok.example, bambuddy.lan", "not an http\\(s\\) URL"),
    ],
)
def test_a_value_out_of_bounds_is_refused_by_name(name: str, value: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        check_value(name, value)


def test_a_value_in_bounds_is_coerced() -> None:
    assert check_value("render_timeout", 5) == 5.0
    assert check_value("log_level", "debug") == "DEBUG"
    assert check_value("asset_max_count", 0) == 0
