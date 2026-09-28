"""Libraries baked into the image, installed on the volume at boot (#169).

The Dockerfile clones the curated catalogue's common libraries at their pinned
refs into ``/app/libraries``, laid out exactly as the volume lays out checkouts
(``<name>/<commit>/<name>/``, see :mod:`scadbuddy.library.libraries`), without
``.git``. At boot, :func:`seed_libraries` copies each one the volume does not
have yet, so a fresh install renders a model pinned to the catalogue's BOSL2
without reaching the network. It records nothing: a checkout is a cache keyed by
commit, and pinning one to a model finds it there instead of cloning.

The copy follows :meth:`LibraryStore._clone`: into a staging directory beside the
checkouts, then renamed into place, so a render never reads a half-copied tree.
Replicas sharing ``/data`` may seed at once; each copies into its own staging, the
first rename wins, and the rest find the checkout there and discard theirs. A
staging copy a killed process left behind is collected by
:meth:`LibraryStore.sweep_staging`, whose age guard spares one still being copied.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import sys
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path

from scadbuddy.core.paths import DataPaths
from scadbuddy.library.libraries import (
    COMMIT_PATTERN,
    CURATED,
    NAME_PATTERN,
    STAGING_PREFIX,
    CatalogueLibrary,
)

logger = logging.getLogger(__name__)


class SeedError(RuntimeError):
    """The image's seed does not match the catalogue it was built for."""


def seeded_checkouts(seed_dir: Path) -> list[tuple[str, str]]:
    """``(name, commit)`` for every complete checkout under ``seed_dir``."""
    found: list[tuple[str, str]] = []
    for library in sorted(seed_dir.iterdir()):
        if not library.is_dir() or not re.fullmatch(NAME_PATTERN, library.name):
            continue
        found.extend(
            (library.name, checkout.name)
            for checkout in sorted(library.iterdir())
            if re.fullmatch(COMMIT_PATTERN, checkout.name) and (checkout / library.name).is_dir()
        )
    return found


def seed_checkout(paths: DataPaths, seed_dir: Path, name: str, commit: str) -> bool:
    """Copy ``name`` at ``commit`` from ``seed_dir`` onto the volume. ``False`` when
    the volume already has that checkout, or another replica put it there first."""
    destination = paths.libraries / name / commit
    if (destination / name).is_dir():
        return False
    paths.libraries.mkdir(parents=True, exist_ok=True)
    staging = paths.libraries / f"{STAGING_PREFIX}{uuid.uuid4().hex}"
    try:
        shutil.copytree(seed_dir / name / commit / name, staging / name, symlinks=True)
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(staging, destination)
        except OSError:
            # A concurrent seed or clone of the same commit got there first;
            # either copy is the same tree.
            if not (destination / name).is_dir():
                raise
            return False
        return True
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def seed_libraries(paths: DataPaths, seed_dir: Path) -> list[tuple[str, str]]:
    """Install every checkout in ``seed_dir`` the volume lacks; returns those
    installed. One that cannot be copied is logged and skipped, so it never stops
    the boot: pinning that library later clones it as usual."""
    installed: list[tuple[str, str]] = []
    for name, commit in seeded_checkouts(seed_dir):
        try:
            if seed_checkout(paths, seed_dir, name, commit):
                installed.append((name, commit))
        except OSError:
            logger.exception(
                "could not seed a library checkout", extra={"library": name, "commit": commit}
            )
    if installed:
        logger.info(
            "seeded library checkouts from the image",
            extra={"checkouts": [f"{name}@{commit[:7]}" for name, commit in installed]},
        )
    return installed


def verify_seed(
    seed_dir: Path,
    refs: Mapping[str, str],
    catalogue: Sequence[CatalogueLibrary] = CURATED,
) -> None:
    """Fail unless ``seed_dir`` holds one checkout of each library in ``refs`` and
    nothing else, and each ref is the catalogue's. Run by the image build, so a
    catalogue bump that leaves the baked-in seed behind breaks the build instead of
    shipping a seed nothing pins."""
    known = {entry.name: entry for entry in catalogue}
    problems: list[str] = []
    for name, ref in refs.items():
        entry = known.get(name)
        if entry is None:
            problems.append(f"{name!r} is not in the curated catalogue")
        elif entry.ref != ref:
            problems.append(f"{name!r} is seeded at {ref!r}, the catalogue pins {entry.ref!r}")
    checkouts = seeded_checkouts(seed_dir) if seed_dir.is_dir() else []
    names = [name for name, _ in checkouts]
    for name in refs:
        if names.count(name) != 1:
            problems.append(f"{name!r} has {names.count(name)} checkouts in {seed_dir}, not 1")
    problems.extend(
        f"{name!r} is in {seed_dir} but was not declared"
        for name in dict.fromkeys(names)
        if name not in refs
    )
    if problems:
        raise SeedError("; ".join(problems))


def main(argv: Sequence[str]) -> int:
    """``python -m scadbuddy.library.library_seed verify <dir> NAME=REF...``"""
    if len(argv) < 2 or argv[0] != "verify" or not all("=" in arg for arg in argv[2:]):
        print("usage: library_seed verify <seed dir> NAME=REF...", file=sys.stderr)
        return 2
    refs = dict(arg.split("=", 1) for arg in argv[2:])
    try:
        verify_seed(Path(argv[1]), refs)
    except SeedError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        print(
            "       Bump the library's *_REF and *_COMMIT build args to the catalogue's"
            " ref (scadbuddy/library/libraries.py CURATED).",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
