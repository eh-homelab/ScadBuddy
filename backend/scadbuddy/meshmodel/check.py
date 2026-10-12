# backend/scadbuddy/meshmodel/check.py
"""`python -m scadbuddy.meshmodel.check <model dir>`: a bundled mesh model's model.scad
is what its scene compiles to, and the scene's recorded hash is that text's (#2146).
Exit 1 naming the first differing line."""

from __future__ import annotations

import sys
from pathlib import Path

from scadbuddy.meshmodel.compile import compile_scene
from scadbuddy.meshmodel.scene import SCENE_NAME, load_scene, text_sha256


def problems(directory: Path) -> list[str]:
    scene = load_scene((directory / SCENE_NAME).read_text(encoding="utf-8"))
    text = (directory / "model.scad").read_text(encoding="utf-8")
    found = []
    if text_sha256(text) != scene.generated_sha256:
        found.append("scene.json's generated_sha256 is not model.scad's")
    expected = compile_scene(scene)
    if text != expected:
        for number, (have, want) in enumerate(
            zip(text.splitlines(), expected.splitlines(), strict=False), 1
        ):
            if have != want:
                found.append(f"model.scad line {number}: {have!r}, the scene compiles {want!r}")
                break
        else:
            found.append("model.scad and the compiled scene differ in length")
    return found


def main(argv: list[str]) -> int:
    found = problems(Path(argv[1]))
    for problem in found:
        print(problem, file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
