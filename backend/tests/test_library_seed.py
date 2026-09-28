"""The libraries baked into the image, installed on the volume at boot (#169)."""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from scadbuddy.core import settings as settings_module
from scadbuddy.core.paths import DataPaths
from scadbuddy.core.settings import Settings
from scadbuddy.library.libraries import (
    CURATED,
    STAGING_PREFIX,
    CatalogueLibrary,
    LibraryStore,
    ModelLibrary,
    search_path,
)
from scadbuddy.library.library_seed import (
    SeedError,
    main,
    seed_libraries,
    seeded_checkouts,
    verify_seed,
)
from scadbuddy.main import create_app
from tests.conftest import UNUSED_DATABASE_URL
from tests.test_library_processes import _age

COMMIT = "f47030c41d88d0676bca73be1c6b7ba58564f9dd"
OTHER = "bd0a7ba3f042bfbced5ca1894b236cea08904e26"
BOSL2 = next(entry for entry in CURATED if entry.name == "BOSL2")


def _bake(seed: Path, name: str = "BOSL2", commit: str = COMMIT, body: str = "// std\n") -> Path:
    """A checkout laid out as the Dockerfile lays it out."""
    checkout = seed / name / commit / name
    (checkout / "sub").mkdir(parents=True)
    (checkout / "std.scad").write_text(body, encoding="utf-8")
    (checkout / "sub" / "part.scad").write_text("cube(1);\n", encoding="utf-8")
    (checkout / "link.scad").symlink_to("std.scad")
    return checkout


@pytest.fixture
def seed(tmp_path: Path) -> Path:
    directory = tmp_path / "image-libraries"
    _bake(directory)
    return directory


@pytest.fixture
def paths(tmp_path: Path) -> DataPaths:
    data = DataPaths(tmp_path / "data")
    data.ensure()
    return data


def _staging(paths: DataPaths) -> list[str]:
    return [p.name for p in paths.libraries.iterdir() if p.name.startswith(STAGING_PREFIX)]


def test_a_seed_installs_the_checkout_where_a_pin_finds_it(seed: Path, paths: DataPaths) -> None:
    assert seed_libraries(paths, seed) == [("BOSL2", COMMIT)]

    checkout = paths.libraries / "BOSL2" / COMMIT / "BOSL2"
    assert (checkout / "std.scad").read_text(encoding="utf-8") == "// std\n"
    assert (checkout / "sub" / "part.scad").is_file()
    assert (checkout / "link.scad").is_symlink()
    assert _staging(paths) == []
    assert LibraryStore(paths).installed() == [("BOSL2", COMMIT)]
    pin = ModelLibrary(name="BOSL2", url=BOSL2.url, ref=BOSL2.ref, commit=COMMIT)
    assert search_path(paths, [pin]) == (paths.libraries / "BOSL2" / COMMIT,)


def test_seeding_again_is_a_no_op(seed: Path, paths: DataPaths) -> None:
    seed_libraries(paths, seed)
    before = (paths.libraries / "BOSL2" / COMMIT / "BOSL2" / "std.scad").stat().st_mtime_ns

    assert seed_libraries(paths, seed) == []
    after = (paths.libraries / "BOSL2" / COMMIT / "BOSL2" / "std.scad").stat().st_mtime_ns
    assert after == before
    assert _staging(paths) == []


def test_a_checkout_already_on_the_volume_is_left_as_it_is(seed: Path, paths: DataPaths) -> None:
    cloned = paths.libraries / "BOSL2" / COMMIT / "BOSL2"
    cloned.mkdir(parents=True)
    (cloned / "std.scad").write_text("// cloned\n", encoding="utf-8")

    assert seed_libraries(paths, seed) == []
    assert (cloned / "std.scad").read_text(encoding="utf-8") == "// cloned\n"
    assert not (cloned / "sub").exists()


def test_another_commit_of_the_same_library_is_seeded_beside_it(
    seed: Path, paths: DataPaths
) -> None:
    (paths.libraries / "BOSL2" / OTHER / "BOSL2").mkdir(parents=True)

    assert seed_libraries(paths, seed) == [("BOSL2", COMMIT)]
    assert LibraryStore(paths).installed("BOSL2") == sorted([("BOSL2", COMMIT), ("BOSL2", OTHER)])


def test_the_copy_is_staged_then_renamed_into_place(
    seed: Path, paths: DataPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = paths.libraries / "BOSL2" / COMMIT
    copies: list[Path] = []
    renames: list[tuple[Path, Path]] = []
    real_copytree, real_replace = shutil.copytree, os.replace

    def copytree(src: Path, dst: Path, *args: Any, **kwargs: Any) -> Any:
        # Nothing is at the path a render reads while the copy runs.
        assert not destination.exists()
        copies.append(Path(dst))
        return real_copytree(src, dst, *args, **kwargs)

    def replace(src: Path, dst: Path) -> None:
        renames.append((Path(src), Path(dst)))
        real_replace(src, dst)

    monkeypatch.setattr(shutil, "copytree", copytree)
    monkeypatch.setattr(os, "replace", replace)

    seed_libraries(paths, seed)

    [(staged, moved_to)] = renames
    assert staged.parent == paths.libraries
    assert staged.name.startswith(STAGING_PREFIX)
    # The first call; the rest are copytree's own recursion into subdirectories.
    assert copies[0] == staged / "BOSL2"
    assert moved_to == destination


def test_a_replica_that_loses_the_race_keeps_the_winners_checkout(
    seed: Path, paths: DataPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_replace = os.replace

    def replace(src: Path, dst: Path) -> None:
        # Another replica sharing /data renames its copy into place first.
        winner = Path(dst) / "BOSL2"
        winner.mkdir(parents=True)
        (winner / "std.scad").write_text("// winner\n", encoding="utf-8")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", replace)

    assert seed_libraries(paths, seed) == []
    checkout = paths.libraries / "BOSL2" / COMMIT / "BOSL2"
    assert (checkout / "std.scad").read_text(encoding="utf-8") == "// winner\n"
    assert _staging(paths) == []


def test_one_that_cannot_be_copied_is_logged_and_the_rest_are_seeded(
    seed: Path, paths: DataPaths, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _bake(seed, "Round-Anything", OTHER)
    real_copytree = shutil.copytree

    def copytree(src: Path, dst: Path, *args: Any, **kwargs: Any) -> Any:
        if Path(src).name == "BOSL2":
            raise OSError("disk full")
        return real_copytree(src, dst, *args, **kwargs)

    monkeypatch.setattr(shutil, "copytree", copytree)

    assert seed_libraries(paths, seed) == [("Round-Anything", OTHER)]
    assert not (paths.libraries / "BOSL2" / COMMIT).exists()
    assert _staging(paths) == []
    assert "could not seed a library checkout" in caplog.text


def test_only_complete_checkouts_with_usable_names_are_seeded(tmp_path: Path) -> None:
    seed = tmp_path / "seed"
    _bake(seed)
    (seed / "BOSL2" / "not-a-commit" / "BOSL2").mkdir(parents=True)
    (seed / "BOSL2" / OTHER).mkdir()  # no checkout inside
    (seed / ".hidden" / COMMIT / ".hidden").mkdir(parents=True)
    (seed / "README").write_text("", encoding="utf-8")

    assert seeded_checkouts(seed) == [("BOSL2", COMMIT)]


def test_the_seed_stays_within_the_staging_sweeps_reach(seed: Path, paths: DataPaths) -> None:
    # A copy a killed boot left behind is a staging directory like a killed
    # clone's, which the boot sweep collects once it is old enough.
    leftover = paths.libraries / f"{STAGING_PREFIX}dead"
    (leftover / "BOSL2").mkdir(parents=True)
    os.utime(leftover, (0, 0))

    seed_libraries(paths, seed)

    assert LibraryStore(paths).sweep_staging() == [leftover.name]
    assert LibraryStore(paths).installed() == [("BOSL2", COMMIT)]


# ── the build-time check ──────────────────────────────────────────────────────


def test_verify_accepts_a_seed_at_the_catalogues_ref(seed: Path) -> None:
    verify_seed(seed, {"BOSL2": BOSL2.ref})


@pytest.mark.parametrize(
    ("refs", "message"),
    [
        ({"BOSL2": "v0.0.1"}, "the catalogue pins"),
        ({"BOSL2": BOSL2.ref, "Nope": "v1"}, "not in the curated catalogue"),
        ({}, "was not declared"),
    ],
)
def test_verify_refuses_a_seed_that_drifted_from_the_catalogue(
    seed: Path, refs: dict[str, str], message: str
) -> None:
    with pytest.raises(SeedError, match=message):
        verify_seed(seed, refs)


def test_verify_refuses_a_library_with_no_checkout_or_two(tmp_path: Path) -> None:
    catalogue = (
        CatalogueLibrary(
            name="BOSL2", url="https://x.invalid/b", ref="v1", licence="", homepage=""
        ),
    )
    with pytest.raises(SeedError, match="0 checkouts"):
        verify_seed(tmp_path / "missing", {"BOSL2": "v1"}, catalogue)
    _bake(tmp_path / "two")
    _bake(tmp_path / "two", commit=OTHER)
    with pytest.raises(SeedError, match="2 checkouts"):
        verify_seed(tmp_path / "two", {"BOSL2": "v1"}, catalogue)


def _notices(ref: str = BOSL2.ref, commit: str = COMMIT) -> str:
    return (
        "# Third-party notices\n\n## BOSL2\n\n"
        f"- Pinned ref: `{ref}` (commit `{commit}`)\n\n## Other\n\nv9 {OTHER}\n"
    )


def test_verify_accepts_notices_that_name_the_seeded_ref_and_commit(seed: Path) -> None:
    verify_seed(seed, {"BOSL2": BOSL2.ref}, notices=_notices())


@pytest.mark.parametrize(
    ("notices", "message"),
    [
        (_notices(ref="v0.0.1"), f"does not name ref '{BOSL2.ref}'"),
        (_notices(commit=OTHER), f"does not name commit '{COMMIT}'"),
        # A longer ref that starts with the seeded one does not count.
        (_notices(ref=BOSL2.ref + "1"), "does not name ref"),
        # The right values in another library's section do not count either.
        (f"## Other\n\n{BOSL2.ref} {COMMIT}\n", "no '## BOSL2' section"),
    ],
)
def test_verify_refuses_stale_notices(seed: Path, notices: str, message: str) -> None:
    with pytest.raises(SeedError, match=message):
        verify_seed(seed, {"BOSL2": BOSL2.ref}, notices=notices)


def test_the_shipped_notices_match_the_catalogue(seed: Path) -> None:
    notices = Path(__file__).parents[2] / "THIRD_PARTY_NOTICES.md"
    verify_seed(seed, {"BOSL2": BOSL2.ref}, notices=notices.read_text(encoding="utf-8"))


def test_the_build_check_exits_non_zero_on_drift(
    seed: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    notices = tmp_path / "NOTICES.md"
    notices.write_text(_notices(), encoding="utf-8")
    assert main(["verify", str(seed), str(notices), f"BOSL2={BOSL2.ref}"]) == 0
    assert main(["verify", str(seed), str(notices), "BOSL2=v0.0.1"]) == 1
    assert "the catalogue pins" in capsys.readouterr().err
    notices.write_text(_notices(ref="v0.0.1"), encoding="utf-8")
    assert main(["verify", str(seed), str(notices), f"BOSL2={BOSL2.ref}"]) == 1
    assert "does not name ref" in capsys.readouterr().err
    assert main(["verify"]) == 2
    assert main(["verify", str(seed)]) == 2
    assert main(["verify", str(seed), str(tmp_path / "missing.md"), f"BOSL2={BOSL2.ref}"]) == 2


@pytest.mark.parametrize("pair", ["BOSL2", "=v1", "BOSL2=", "="])
def test_the_build_check_refuses_a_malformed_pair(
    seed: Path, tmp_path: Path, pair: str, capsys: pytest.CaptureFixture[str]
) -> None:
    notices = tmp_path / "NOTICES.md"
    notices.write_text(_notices(), encoding="utf-8")
    assert main(["verify", str(seed), str(notices), f"BOSL2={BOSL2.ref}", pair]) == 2
    assert f"{pair!r} is not NAME=REF" in capsys.readouterr().err


# ── boot ──────────────────────────────────────────────────────────────────────


def test_boot_seeds_the_volume_from_the_image(seed: Path, tmp_path: Path, pg_conninfo: str) -> None:
    models = tmp_path / "models"
    models.mkdir()
    settings = Settings(
        openscad="definitely-not-installed",
        data_dir=tmp_path / "data",
        seed_models_dir=models,
        seed_libraries_dir=seed,
        frontend_dir=Path("/nonexistent"),
        database_url=pg_conninfo,
    )
    with TestClient(create_app(settings)):
        pass

    assert (tmp_path / "data" / "libraries" / "BOSL2" / COMMIT / "BOSL2" / "std.scad").is_file()


@pytest.mark.requires_git
def test_the_boot_checkout_sweep_keeps_the_seed_nothing_pins(
    seed: Path, tmp_path: Path, pg_conninfo: str
) -> None:
    """Otherwise every later boot would delete it and the next copy it back."""
    models = tmp_path / "models"
    models.mkdir()
    settings = Settings(
        openscad="definitely-not-installed",
        data_dir=tmp_path / "data",
        seed_models_dir=models,
        seed_libraries_dir=seed,
        frontend_dir=Path("/nonexistent"),
        database_url=pg_conninfo,
    )
    libraries = tmp_path / "data" / "libraries"
    with TestClient(create_app(settings)):
        pass
    seeded = libraries / "BOSL2" / COMMIT
    unpinned = libraries / "BOSL2" / OTHER
    (unpinned / "BOSL2").mkdir(parents=True)
    _age(seeded)
    _age(unpinned)

    with TestClient(create_app(settings)):
        pass

    assert (seeded / "BOSL2" / "std.scad").is_file()
    assert os.stat(seeded).st_mtime < time.time() - 60  # kept, not copied back
    assert not unpinned.exists()


def test_the_container_seed_is_used_when_it_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings_module, "CONTAINER_SEED_LIBRARIES_DIR", tmp_path)
    assert Settings(database_url=UNUSED_DATABASE_URL).resolve_seed_libraries_dir() == tmp_path
    monkeypatch.setattr(settings_module, "CONTAINER_SEED_LIBRARIES_DIR", tmp_path / "gone")
    assert Settings(database_url=UNUSED_DATABASE_URL).resolve_seed_libraries_dir() is None
    assert (
        Settings(
            seed_libraries_dir=tmp_path, database_url=UNUSED_DATABASE_URL
        ).resolve_seed_libraries_dir()
        == tmp_path
    )
