#!/usr/bin/env bash
#
# `camera-inputs.sh <part> [--files]`: the sha256 of what the camera-<part>
# image (relay, recorder or edge) runs, from the repo at the current directory
# (#8191). The build stores it as the image label `io.scadbuddy.camera.inputs`,
# and deploy.reusable.yml keeps a part's pins when the new image's label equals
# the pinned image's: the image copies all of backend/scadbuddy and carries
# build labels, so its digest moves with every build, but a restart of the
# recorders or the edge is a gap in the recording or the live view.
#
# The inputs:
# - every scadbuddy module that Python loads when it imports the three
#   processes (scadbuddy.camera.relay, .recorder and .edge), found statically with `ast`: each module-level
#   import is followed (an `if TYPE_CHECKING:` block is not run, so it is not),
#   with the parent packages' __init__.py. An import inside a function adds its
#   target module's file only, not what that module imports (inside
#   scadbuddy.camera it is followed in full): the two outside it are
#   (core/metrics.py's pg_pool, intake_worker/child.py's check) never run in a
#   camera image, which has no psycopg, and following them would pull in most
#   of the backend (measured 2026-11-27: 418 files against the 30 the three
#   entry modules load);
# - backend/pyproject.toml and backend/uv.lock (the image's dependencies);
# - deploy/camera/mediamtx-<part>.yml (the part's MediaMTX configuration);
# - the Dockerfile's stages the image is built from: `uv`, `mediamtx`,
#   `camera-deps`, `camera` and `camera-<part>`.
#
# Not an input: the Debian packages the `camera` and `camera-deps` stages
# install with apt (curl, tini, ca-certificates, git) are not pinned. So while a
# part's pins are kept, it gets no Debian security update to them until a
# hashed input changes (#8202 review).
#
# With --files, it prints the input paths (Dockerfile stages as
# `Dockerfile#<stage>`), one per line, instead of the hash.
set -euo pipefail

part=${1:-}
case "$part" in
  relay|recorder|edge) ;;
  *) echo "usage: camera-inputs.sh relay|recorder|edge [--files]" >&2; exit 2 ;;
esac
mode=${2:-hash}

python3 -I - "$part" "$mode" <<'PY'
import ast
import hashlib
import pathlib
import re
import sys

part, mode = sys.argv[1], sys.argv[2]
root = pathlib.Path.cwd()
backend = root / "backend"
package = backend / "scadbuddy"

def module_file(name: str) -> pathlib.Path | None:
    """The file of scadbuddy module ``name``, or None when it is not one."""
    if name != "scadbuddy" and not name.startswith("scadbuddy."):
        return None
    base = backend.joinpath(*name.split("."))
    for candidate in (base / "__init__.py", base.with_suffix(".py")):
        if candidate.is_file():
            return candidate
    return None


def with_parents(name: str) -> list[str]:
    parts = name.split(".")
    return [".".join(parts[: i + 1]) for i in range(len(parts))]


def imported(path: pathlib.Path, name: str) -> tuple[set[str], set[str]]:
    """The modules ``path`` imports at module level, and those it imports in functions."""
    package = name if path.name == "__init__.py" else name.rpartition(".")[0]
    eager: set[str] = set()
    lazy: set[str] = set()

    def targets(node: ast.Import | ast.ImportFrom) -> set[str]:
        found: set[str] = set()
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.update(with_parents(alias.name))
            return found
        if node.level:
            anchor = package.split(".")[: len(package.split(".")) - node.level + 1]
            origin = ".".join(anchor + ([node.module] if node.module else []))
        else:
            origin = node.module or ""
        found.update(with_parents(origin))
        for alias in node.names:
            if module_file(f"{origin}.{alias.name}"):
                found.add(f"{origin}.{alias.name}")
        return found

    def visit(nodes: list[ast.stmt], into: set[str]) -> None:
        for node in nodes:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                into.update(targets(node))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for sub in ast.walk(node):
                    if isinstance(sub, (ast.Import, ast.ImportFrom)):
                        lazy.update(targets(sub))
            elif isinstance(node, ast.If) and "TYPE_CHECKING" in ast.unparse(node.test):
                visit(node.orelse, into)
            elif isinstance(node, ast.Match):
                visit([s for case in node.cases for s in case.body], into)
            else:
                for field in ("body", "orelse", "finalbody", "handlers"):
                    block = getattr(node, field, None)
                    if isinstance(block, list):
                        visit([n for n in block if isinstance(n, ast.stmt)] + [
                            s for h in block if isinstance(h, ast.ExceptHandler) for s in h.body
                        ], into)

    visit(ast.parse(path.read_text(), str(path)).body, eager)
    return eager, lazy


# The three processes the images run (the Dockerfile's `camera` stage imports
# the same three in its check).
entries = ["scadbuddy.camera.relay", "scadbuddy.camera.recorder", "scadbuddy.camera.edge"]
seen: set[str] = set()
lazy_only: set[str] = set()
queue = [n for entry in entries for n in with_parents(entry)]
while queue:
    name = queue.pop()
    if name in seen:
        continue
    path = module_file(name)
    if path is None:
        continue
    seen.add(name)
    eager, lazy = imported(path, name)
    queue.extend(eager)
    # The camera's own lazy imports are camera code: follow them in full.
    queue.extend(n for n in lazy if n == "scadbuddy.camera" or n.startswith("scadbuddy.camera."))
    lazy_only.update(lazy)
loaded = {module_file(n) for n in seen} | {module_file(n) for n in lazy_only}
modules = {p.relative_to(root).as_posix() for p in loaded if p is not None}

files = sorted(modules | {
    "backend/pyproject.toml",
    "backend/uv.lock",
    f"deploy/camera/mediamtx-{part}.yml",
})

stages = {"uv", "mediamtx", "camera-deps", "camera", f"camera-{part}"}
blocks: dict[str, list[str]] = {}
current = None
for line in (root / "Dockerfile").read_text().splitlines():
    match = re.match(r"^FROM\s+\S+\s+AS\s+(\S+)\s*$", line, re.IGNORECASE)
    if match:
        current = match.group(1).lower()
        if current in stages:
            blocks[current] = []
    if current in blocks:
        blocks[current].append(line)
missing = stages - blocks.keys()
if missing:
    sys.exit(f"camera-inputs: the Dockerfile has no stage {', '.join(sorted(missing))}")

if mode == "--files":
    print("\n".join(files + [f"Dockerfile#{name}" for name in sorted(blocks)]))
    sys.exit(0)
if mode != "hash":
    sys.exit("usage: camera-inputs.sh relay|recorder|edge [--files]")

digest = hashlib.sha256()
for path in files:
    content = (root / path).read_bytes()
    digest.update(f"{path}\0{hashlib.sha256(content).hexdigest()}\n".encode())
for name in sorted(blocks):
    text = "\n".join(blocks[name]).rstrip("\n").encode()
    digest.update(f"Dockerfile#{name}\0{hashlib.sha256(text).hexdigest()}\n".encode())
print(digest.hexdigest())
PY
