"""`NativeSliceKeys`: a run's reuse lookup builds each plan's presets once (plan
2026-10-16 Task 13 review M4), however many plates share them."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from scadbuddy.bambuddy.dispatch import SlicePlan
from scadbuddy.slicing import start
from scadbuddy.slicing.presets import PresetFiles, native_preset_key
from scadbuddy.slicing.profiles import Profiles


async def test_plates_that_share_a_plan_build_its_presets_once(
    plan: SlicePlan, profiles: Profiles, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[SlicePlan] = []

    async def build(plan: SlicePlan, profiles: Profiles, client: Any) -> PresetFiles:
        built.append(plan)
        await asyncio.sleep(0.01)
        return PresetFiles(machine=b"", process=b"", filaments=[], bambuddy_process="p")

    monkeypatch.setattr(start, "build_presets", build)
    keys = start.NativeSliceKeys(SimpleNamespace(profiles=profiles), None)  # type: ignore[arg-type]
    other = plan.model_copy(update={"bed_type": "Cool Plate"})

    found = await asyncio.gather(keys.key(plan, 1), keys.key(plan, 2), keys.key(other, 3))

    assert built == [plan, other]
    assert list(found) == [
        native_preset_key(plan, 1, profiles.version, "p"),
        native_preset_key(plan, 2, profiles.version, "p"),
        native_preset_key(other, 3, profiles.version, "p"),
    ]
