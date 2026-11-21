"""#887: every setting has a label and help, from one place.

A new setting with no entry in ``core/setting_docs.py`` fails here, so the Settings page
never shows a field without a label and help text.
"""

from __future__ import annotations

import pytest

from scadbuddy.api.settings import FIELD_DOCS, SettingsView
from scadbuddy.core.setting_docs import SETTING_DOCS
from scadbuddy.core.settings import Settings, env_var
from scadbuddy.library.settings_store import SettingsPatch

#: Every setting there is: the environment's and the ones only Settings stores.
SETTINGS = (set(Settings.model_fields) | set(SettingsPatch.model_fields)) - {"reset"}


def test_every_setting_has_a_doc_and_every_doc_a_setting() -> None:
    assert set(SETTING_DOCS) - SETTINGS == set(), "a doc names no setting"
    assert SETTINGS - set(SETTING_DOCS) == set(), "a setting has no doc"


@pytest.mark.parametrize("name", sorted(SETTING_DOCS))
def test_each_doc_has_a_title_and_a_description(name: str) -> None:
    doc = SETTING_DOCS[name]
    assert doc.title.strip() == doc.title and doc.title
    assert doc.description.strip() == doc.description and doc.description
    # Help is a sentence or a few, not a label repeated.
    assert doc.description.endswith((".", ")")), name
    assert doc.description != doc.title


def test_the_answer_names_each_env_var_and_only_those() -> None:
    for name, doc in FIELD_DOCS.items():
        assert doc.env_var == (env_var(name) if name in Settings.model_fields else None)


def test_the_spec_carries_the_docs() -> None:
    schema = SettingsView.model_json_schema()
    default = schema["properties"]["docs"]["default"]
    assert set(default) == set(SETTING_DOCS)
    assert default["bambuddy_render_api_key"]["title"] == "Render worker API key"
    assert default["bambuddy_render_api_key"]["env_var"] == "SCADBUDDY_BAMBUDDY_RENDER_API_KEY"
