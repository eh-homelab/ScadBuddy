"""Spool providers are found as features export them (#3818 tier A, plan 2026-10-18 D1),
never listed by hand."""

from __future__ import annotations

import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import cast

import pytest

from scadbuddy.core.components import Components, Core
from scadbuddy.spools.models import clean_key
from scadbuddy.spools.providers import DuplicateProviderError, discover_providers

PROVIDER = """
from scadbuddy.spools.models import SpoolSnapshot


class Provider:
    def __init__(self, name):
        self.name = name
        self.label = name.title()

    async def configured(self):
        return True

    async def records(self):
        return [SpoolSnapshot(record_id="1")]


SPOOL_PROVIDERS = [{builds}]
"""


def _package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, features: dict[str, str]
) -> ModuleType:
    root = tmp_path / "spoolpkg"
    root.mkdir()
    (root / "__init__.py").write_text("", encoding="utf-8")
    for feature, builds in features.items():
        (root / feature).mkdir()
        (root / feature / "__init__.py").write_text("", encoding="utf-8")
        (root / feature / "spool_providers.py").write_text(
            textwrap.dedent(PROVIDER).replace("{builds}", builds), encoding="utf-8"
        )
    monkeypatch.syspath_prepend(str(tmp_path))
    import spoolpkg  # type: ignore[import-not-found]

    return cast(ModuleType, spoolpkg)


@pytest.fixture
def forget_package() -> Iterator[None]:
    yield
    for name in [name for name in sys.modules if name.split(".")[0] == "spoolpkg"]:
        del sys.modules[name]


CORE = cast(Core, SimpleNamespace())
COMPONENTS = cast(Components, SimpleNamespace())


@pytest.mark.usefixtures("forget_package")
def test_every_features_providers_are_found_by_feature_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = _package(
        tmp_path,
        monkeypatch,
        {
            "beta": "lambda core, components: Provider('beta')",
            "alpha": "lambda core, components: Provider('alpha'), lambda core, components: None",
        },
    )
    found = discover_providers(CORE, COMPONENTS, package)
    assert [provider.name for provider in found] == ["alpha", "beta"]


@pytest.mark.usefixtures("forget_package")
def test_two_providers_named_alike_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = _package(
        tmp_path,
        monkeypatch,
        {
            "alpha": "lambda core, components: Provider('same')",
            "beta": "lambda core, components: Provider('same')",
        },
    )
    with pytest.raises(DuplicateProviderError, match="same"):
        discover_providers(CORE, COMPONENTS, package)


@pytest.mark.parametrize(
    ("raw", "key"),
    [
        (None, None),
        ("", None),
        ("  ", None),
        ("0" * 16, None),
        ("0" * 32, None),
        ("1A2B3C4D5E6F7081", "1A2B3C4D5E6F7081"),
        (" 1a2b3c4d5e6f7081 ", "1A2B3C4D5E6F7081"),
    ],
)
def test_clean_key(raw: str | None, key: str | None) -> None:
    assert clean_key(raw) == key
