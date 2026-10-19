# scadbuddy-render-tools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The render worker's two CPU-bound mesh stages, the plate cover rasteriser (`thumbnail`) and the OpenSCAD 3MF split by material (`split`), run in a Rust CLI, `scadbuddy-render-tools`, that the Python render activities start as a subprocess beside `openscad`. Each stage is chosen per stage by the live setting `render_rust_stages` and falls back to the Python code when the binary is absent, refuses the input, fails or overruns. No workflow, activity signature or queue changes.

**Architecture:** A library crate `workers/rust/crates/sb-render` (3MF split, mesh files, rasteriser, PNG) and a binary `workers/rust/bins/scadbuddy-render-tools` with subcommands `split`, `thumbnail` and `version`. The binary reads files from the piece's work directory, writes files there, and prints one JSON document on stdout whose shape is a pydantic model in `backend/scadbuddy/render/tools_models.py`, pinned across the two languages by `workers/rust/vectors/render_tools.json`. The Python side (`backend/scadbuddy/render/tools.py`) finds the binary, runs it in its own process group under the stage's budget, parses the result and decides the fallback. A Dockerfile stage `render-tools-build` builds it; the `app` stage copies it to `/usr/local/bin/` beside `openscad`, so the render worker, the in-process worker and the `test` image all carry it.

**Tech Stack:** Rust (the workspace's pinned toolchain, `rust-toolchain.toml`, 1.99.1), `zip` 8 (workspace), `quick-xml` (streaming XML), `png` (encoder), `serde`/`serde_json`, `thiserror`, `anyhow`; no Temporal crate and no `protoc`. Python 3.12 backend (pydantic 2, numpy, trimesh), `prometheus_client`.

**Spec:** `docs/superpowers/specs/2026-10-16-own-store-slicer-rust-workers-design.md` §7.1 (layout, vectors, the stage histogram), §7.2 (a CLI beside the Python activity, no Temporal SDK, no change to the pinned `RenderPiece` history), §7.3 (migration order), §8 (`render_rust_stages`); §2 for the prod trace this answers. Issue #3824, epic #3818.

## Measured baselines (2026-10-18)

Measured for this plan, on a WSL2 workstation (x86_64, 22 logical CPUs, Python 3.12.3, numpy 2.5.4, the backend's `uv sync --frozen` environment at `origin/main` afb2cf7f2). The scripts are not committed; each is reproduced by Task 0's `backend/tests/support/render_bench.py`, which this plan adds so the numbers can be re-measured with one command.

**Thumbnail and split, Python.** Each MakerWorld fixture (`test-fixtures/makerworld/`, Git LFS) is read with `render/objects3mf.read_objects(…, ReadBudget.widest())`, every non-negative part of every object is taken as one plate (placements ignored: the cost is per triangle), and `render/thumbnail.render_plate_thumbnails(parts)` is timed (best of two). For the split, the same parts are written as an OpenSCAD-shaped 3MF (one object, `<basematerials>`, per-triangle `pid`/`p1`, coordinates `%.6g`, which is what `openscad --backend=Manifold -o x.3mf` emits, spec 09-22 §3) and `render/split.split_by_material` is timed (best of two), then `render/glb.write_glb` once.

| Fixture | Parts (painted) | Triangles | Raw 3MF | `render_plate_thumbnails` | of which `painted_pieces` | one lit 1024² view | pick 512² view | `split_by_material` | `write_glb` |
|---|---|---|---|---|---|---|---|---|---|
| 411-broccoli | 5 (0) | 27,920 | 0.3 MB | 2.25 s | 0.00 s | 0.35 s | 0.33 s | 0.22 s | 0.02 s |
| 2069-fidget-cubes-mc-edition | 10 (8) | 47,466 | 0.5 MB | 4.09 s | 0.09 s | 2.33 s | 0.40 s | 0.45 s | 0.03 s |
| 448-articulated-pumpkin | 2 (0) | 758,026 | 8.3 MB | 11.67 s | 0.00 s | 5.14 s | 2.04 s | 7.17 s | 0.39 s |
| 2464-ghost-campfire-light | 16 (6) | 6,051,414 | 66.9 MB | 58.67 s | 12.59 s | 15.22 s | 10.83 s | 62.34 s | 3.28 s |

`cProfile` of the broccoli run: 1.89 s of 2.44 s in `_rasterise` (1.10 s in `_block`, the barycentric broadcast; 0.59 s in `_Canvas.draw`'s `lexsort`), 0.45 s in `_downsample`, 0.09 s in `zlib.compress`. The cost is numpy per-batch overhead plus the sort, not pixels and not PNG encoding.

Read against the prod trace (§2: 24 s `render.thumbnail`, 7 s split on an 89 s render of a 15 MB model): the pumpkin's 7.17 s split matches prod's 7 s, and prod's thumbnail (24 s) sits between the pumpkin and the ghost here, on a slower, shared prod core. `PlateLayout.load` in `finish_piece` (`render/jobs.py`) runs `split_by_material` again on every split part's file and `solid_mesh` (a split) on every solid's file, outside any timed stage: a second split cost the dashboards do not show today (Task 7 times it as stage `load`).

**Pack and unpack, Python.** `store/archive.pack_dir_to` of a 39 MB piece directory (the pumpkin's raw 3MF twice, its GLB, and the 10 MB fixture as `model.3mf`): 2.30 s, 32.3 MB archive; `unpack_dir` of it 0.31 s. Deflating 3MFs that are already deflated zips is most of the pack.

**Solids parallelism.** The bounded pool spec §7.3 step 3 asks for already exists: `render_solids` (`render/solids.py`, #282) runs `config.solid_slots()` wrapper renders at once. Prod's 14 s is serial because of how that resolves on the render worker: `scadbuddy-render` in namespace `bambuddy` has `limits.cpu: 4` and `SCADBUDDY_RENDER_CONCURRENCY=2` (read with `kubectl -n bambuddy get deploy scadbuddy-render -o jsonpath=…`, 2026-10-18), `solid_concurrency` is 0 (default; `GET /settings` through the ScadBuddy MCP `get_settings`, 2026-10-18), and the worker's `check_concurrency` is the default 1, so `default_solid_concurrency(4, 2, 1)` = `max(1, min(8, (4 - 1) // 2))` = **1**. Measured in the pinned image (`docker run --rm --cpus 4 openscad/openscad:dev.2026-10-12`, the wrapper from `solids.WRAPPER_PRELUDE`, one `openscad --backend=Manifold` per colour through `xargs -P <slots>`):

| Model (defaults) | Colours | Main render | solids, 1 slot | 2 slots | 3 slots | 4 slots |
|---|---|---|---|---|---|---|
| `models/fidget-slider-board` | 9 | 3.0 s | 23.8 s | 15.9 s | 9.8 s | 8.7 s |
| `models/shape-sorter-puzzle` | 8 | 3.1 s | 3.9 s | 3.3 s | 2.9 s | 3.1 s |

It pays where a colour's render is heavy (2.4x at 3 slots) and costs nothing where it is light. The fix is Python and settings, not Rust (Task 13), and `solids` is not a member of `render_rust_stages` (see Decisions).

**Pixel tolerance for thumbnail parity.** The Rust rasteriser is a port of the same algorithm in f64, so its pixels differ from numpy's only where float evaluation order flips an edge or depth test. The noise floor of that was measured on the Python rasteriser against itself: vertices round-tripped through float32 (a relative perturbation of about 1e-7) and the four PNGs decoded and compared per pixel (max channel difference):

| Fixture | Image | Pixels differing | of covered pixels | differing by > 8 | by > 32 | max |
|---|---|---|---|---|---|---|
| 411-broccoli | plate 512² | 1 | 0.0027% | 1 | 0 | 13 |
| 411-broccoli | top 512² | 4 | 0.0028% | 1 | 0 | 14 |
| 2069-fidget-cubes | plate 512² | 1 | 0.0012% | 1 | 1 | 48 |
| 448-articulated-pumpkin | plate 512² | 48 | 0.1087% | 1 | 1 | 107 |
| 448-articulated-pumpkin | plate_small 128² | 4 | 0.1366% | 0 | 0 | 4 |
| 448-articulated-pumpkin | top 512² | 101 | 0.1290% | 3 | 1 | 56 |
| every fixture | pick 512² | 0 | 0% | 0 | 0 | 0 |

So a max-difference bound is useless (one flipped edge pixel is up to 107), and a mean is too forgiving. The parity check (Task 10) is, per image: identical dimensions; alpha coverage (count of `alpha > 0`) within 0.5% of Python's; pixels differing at all at most **0.5%** of Python's covered pixels (3.7x the worst measured, 0.137%); pixels differing by more than 32 at most **0.05%** of covered (at least 8 pixels, so a small image is not held to zero); the `pick` image differing in at most **0.05%** of covered pixels (measured 0, but its edges can flip like any other). Task 10 records the Rust-vs-Python numbers it actually sees on every fixture in the test's docstring and tightens a bound that is more than 10x the observed.

## Decisions (my calls; the ones marked **user** need the user)

1. **`render_rust_stages` is a subset of `thumbnail`, `split`, `store`**, not spec §8's `thumbnail, split, solids, store`: solids' parallelism is a Python pool that exists and is gated by `solid_concurrency`, a setting already live (above). **User:** Task 13 either sets prod's `solid_concurrency` to 2 (a stored setting, live, no deploy) or gives `Config.solid_slots` a `checks_here: bool` so that a process that runs no editor checks (the render worker) passes `check_concurrency=0` to `default_solid_concurrency`, which itself stays unchanged, which gives 2 on prod's 4 CPUs. The plan does the code change and recommends the setting as the immediate step; either answers §7.3 step 3.
2. **The thumbnail CLI refuses a painted layout** (exit 3, outcome `unsupported`, Python draws it). Painting (`split.painted_pieces`, `render/paint.pieces`, a mesh model's carried `.paint.json`) is 560 lines of Python with its own tests; no `.scad` render has paint (OpenSCAD writes no `paint_color`), so every template render takes the Rust path. Porting it is follow-up issue work, filed in Task 15.
3. **Rust's PNG bytes are not Python's.** Cover images are compared decoded; the 3MF a Rust stage writes differs in its PNG entries' bytes from a Python one. Nothing keys on those bytes (`piece_key` and `render_key` are inputs, not outputs).
4. **`split` hands meshes to Python as raw little-endian arrays**, not as a format trimesh parses: `<n>.vertices` (f64 x 3) and `<n>.faces` (u32 x 3), read with `numpy.fromfile` into `trimesh.Trimesh(process=False)`, exactly the arrays `split_by_material` builds. Parity is exact equality (both languages parse decimal to the nearest f64).
5. **The store stage (pack/unpack/put/get) is gated on a measurement and on `TransferUrl`** (Tasks 16, 17). Pack is 2.3 s on a 39 MB piece, most of it deflating zips inside a zip; storing `.3mf` entries uncompressed in Python may take most of that win without Rust. `put`/`get` need the worker to hand the CLI URLs, not credentials (spec §5.2), and `slicing/transfer.py` (native slicer plan Task 10) is on `feat/native-slicer-caller`, not `main`. **User:** whether Tasks 16–17 stay in this plan's PR series or move to an issue of their own once the slicer branch merges.
6. **Metrics:** `scadbuddy_render_stage_seconds` keeps its name and buckets and gains a label `impl` (`python`|`rust`): the implementation that produced the stage's result. The dashboards' `sum by (le, stage)` keeps working. A counter `scadbuddy_render_tools_runs_total{stage,outcome}` counts each CLI attempt.
7. **The binary's location is a bootstrap setting**, `SCADBUDDY_RENDER_TOOLS` (default `scadbuddy-render-tools`), not a stored one, for `openscad`'s reason (a form choosing a binary is a form choosing what runs).

## Global Constraints

- No change to any workflow, activity signature, activity input or output model, task queue or `workflow.patched` set. `tests/fixtures/*histories*` replay unchanged; the plan touches nothing under `backend/scadbuddy/workflows/` except `activities.py`'s reading of the stage set from `WorkerDeps`, which crosses no history.
- `render_rust_stages` empty (the default) runs exactly today's Python code: same calls, same warnings, same files. Every Task's tests include that case.
- A Rust stage never fails a render that Python would have finished: absent binary, non-zero exit, unparsable stdout, a result naming a file that is not there, or an overrun all fall back to the Python stage, with one warning log per process per (stage, outcome) and the counter incremented. The one exception is the thumbnail's budget (Task 10): an overrun there is today's `THUMBNAIL_TIMEOUT_WARNING`, not a second, slower Python attempt.
- The CLI reads only paths under the directory it is given and writes only there; it opens no socket, reads no environment variable but `SCADBUDDY_RENDER_TOOLS_THREADS` (Task 8), and never prints a parameter value, a file's content or a mesh coordinate. Its stderr is never put in a span, a metric label or a warning a person sees; at most its last 2 KB in a log record at `warning`.
- The child runs with `start_new_session=True` and is killed with `os.killpg(pid, SIGKILL)` on timeout and on cancellation, as `workflows/template_process.py` `_kill_group` does; a cancel returns only once the child is reaped (#867's rule).
- Cross-language shapes live in `render/tools_models.py` and nowhere else; `workers/rust/vectors/render_tools.json` is written only with `UPDATE_RENDER_TOOLS_VECTORS=1`.
- No base64 image in JSON (`lint-base64-images.sh`): images move as files the result names.
- Rust: `cargo fmt --all --check`, `cargo clippy --workspace --all-targets --locked -- -D warnings`, `cargo test --workspace --locked` (the `rust` job). No `unsafe`. No new workspace dependency without an exact-major entry in `[workspace.dependencies]`.
- Python: `ruff check`, `ruff format --check`, `mypy` (strict), `pytest -n auto`.
- Commits: conventional titles; never amend; last line `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. **Exact split parity.** `split_by_material`'s grouping key is `(triangle pid or object pid, triangle p1 or object pindex)`, parts come out `sorted()` by that key (a string then an int), each object's mesh keeps only referenced vertices in their original order (`remove_unreferenced_vertices`), and an object's pieces of one key are concatenated in document order with vertex offsets. One different rule gives a mesh that looks right and fails parity, or worse, gives the solids a different extruder order. Test in Tasks 3 and 7.
2. **Rounding.** numpy's `rint` rounds half to even; Rust's `f64::round` rounds half away from zero. The rasteriser's colour quantisation and `_downsample` use `rint`; the port must use `round_ties_even`. Test in Task 8 (a colour that lands on .5).
3. **The tie rule.** Of equally deep fragments the one drawn last wins (`depth <= buffer`), in part order then triangle order. A port that draws in another order (per tile, in threads) must keep it. Test in Task 8.
4. **A child that outlives its stage.** A timed-out or cancelled `thumbnail` must leave no process and no half-written PNG the 3MF writer then reads. Test in Task 5.
5. **The live setting reaching a standalone worker.** The render worker reads settings only through the `render_settings` view (#601); a new name not added to the view reads as the default forever and the test against an in-process worker still passes. Test in Task 6 renders as role `scadbuddy_render` (`tests/test_render_worker_role.py`'s pattern).

---

### Task 0: The benchmark helper this plan's numbers came from

**Files:**
- Create: `backend/tests/support/openscad3mf.py`, `backend/tests/support/render_bench.py`
- Test: `backend/tests/test_openscad3mf_support.py`

**Interfaces:**
- Produces:
  ```python
  def write_openscad_3mf(parts: Sequence[ColourPart], path: Path) -> int:
      """``parts`` as OpenSCAD's Manifold export writes them: one object, one <basematerials id="1">
      with a <base displaycolor="#RRGGBB00"> per part, per-triangle pid="1" p1=<part index>, coordinates %.6g.
      Returns the triangle count."""
  # render_bench.py: python -m tests.support.render_bench <fixtures dir> <out dir> <file>...  (prints one JSON line per file:
  # parts, painted_parts, triangles, raw_3mf_mb, thumbnail_s, painted_pieces_s, lit_view_s, pick_view_s, split_s, glb_s,
  # and with --rust also rust_thumbnail_s, rust_split_s)
  ```

- [ ] **Step 1: Write the failing test** — `write_openscad_3mf` of two cubes in two colours, then `split_by_material` of the file gives two parts whose colours, material indices and face counts are the inputs'.
- [ ] **Step 2: Run** `cd backend && uv run --frozen pytest tests/test_openscad3mf_support.py -v` — Expected: FAIL (`ModuleNotFoundError`).
- [ ] **Step 3: Implement** both modules (the measurement section's procedure; `render_bench` is a `__main__` module, not a test).
- [ ] **Step 4: Run** the test, `mypy`, `ruff check .`, `ruff format --check .` — Expected: PASS.
- [ ] **Step 5: Commit**

```bash
git add backend/tests/support/openscad3mf.py backend/tests/support/render_bench.py backend/tests/test_openscad3mf_support.py
git commit -m "test(render): OpenSCAD-shaped 3MF writer and the stage benchmark (#3824)"
```

---

### Task 1: The CLI's result models and their vectors

**Files:**
- Create: `backend/scadbuddy/render/tools_models.py`
- Create: `workers/rust/vectors/render_tools.json` (written by the test)
- Test: `backend/tests/test_render_tools_models.py`

**Interfaces:**
- Produces:
  ```python
  RustStage = Literal["thumbnail", "split", "store"]
  RUST_STAGES: tuple[RustStage, ...] = get_args(RustStage)
  ToolOutcome = Literal["ok", "absent", "unsupported", "failed", "timeout"]

  #: Exit codes: 0 the result on stdout; 2 usage; 3 the input is outside what the tool draws
  #: (stdout a ToolRefusal); 1 anything else (stdout a ToolRefusal when it got that far).
  EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_UNSUPPORTED = 0, 1, 2, 3

  class ToolRefusal(BaseModel):
      error: Literal["unsupported", "unreadable", "io", "internal"]
      message: str                       # fixed text from the tool; never a value from the input

  class SplitPartFile(BaseModel):
      group: str                         # the basematerials id ("" when none)
      material_index: int
      name: str
      colour: str                        # normalise_colour's "#RRGGBB"
      vertices: str                      # file name, relative to the out dir: <n>.vertices, f64 LE x3
      faces: str                         # <n>.faces, u32 LE x3
      vertex_count: int
      face_count: int
      paint_codes: str | None = None     # <n>.paint: one line per face, "" for none; None when no face is painted
      palette: list[str] = Field(default_factory=list)   # the group's colours in index order, when painted

  class SplitResult(BaseModel):
      parts: list[SplitPartFile]
      elapsed_seconds: float

  class PlateImages(BaseModel):
      plate: str                         # file names in the out dir, as write_plates_3mf's entries
      plate_small: str
      top: str
      pick: str

  class ThumbnailResult(BaseModel):
      plates: list[PlateImages]          # layout order
      triangles: int
      elapsed_seconds: float

  class VersionResult(BaseModel):
      version: str                       # CARGO_PKG_VERSION
      stages: list[RustStage]            # what this build implements
  ```
- Vector file `{"refusal": [...], "split": [...], "thumbnail": [...], "version": [...]}`, each a `model_dump(mode="json")`, compared after a round trip; written with `UPDATE_RENDER_TOOLS_VECTORS=1`, as `slice_plate.json` is. Without it the test regenerates the cases and asserts the committed file equals them (`json.loads(VECTORS.read_text()) == dumped`, as `backend/tests/slicing/test_models.py:95` does), so a hand edit of `render_tools.json` fails the backend shards (Task 12) as well as the Rust round trip.

- [ ] **Step 1: Write the failing test** (the `slice_plate` vectors test's shape, `backend/tests/slicing/test_models.py`): cases covering a painted and an unpainted `SplitPartFile`, a two-plate `ThumbnailResult`, every `ToolRefusal.error`, a `VersionResult`.
- [ ] **Step 2: Run** `cd backend && uv run --frozen pytest tests/test_render_tools_models.py -v` — Expected: FAIL (`ModuleNotFoundError: scadbuddy.render.tools_models`).
- [ ] **Step 3: Implement** the models.
- [ ] **Step 4: Write the vectors and re-run:** `UPDATE_RENDER_TOOLS_VECTORS=1 uv run --frozen pytest tests/test_render_tools_models.py && uv run --frozen pytest tests/test_render_tools_models.py && uv run --frozen mypy && uv run --frozen ruff check . && uv run --frozen ruff format --check .` — Expected: PASS twice.
- [ ] **Step 5: Commit**

```bash
git add backend/scadbuddy/render/tools_models.py backend/tests/test_render_tools_models.py workers/rust/vectors/render_tools.json
git commit -m "feat(render): scadbuddy-render-tools result models and their vectors (#3824)"
```

---

### Task 2: `sb-render` and the `scadbuddy-render-tools` binary skeleton

**Files:**
- Modify: `workers/rust/Cargo.toml` (members; `quick-xml`, `png` in `[workspace.dependencies]`), `workers/rust/Cargo.lock`
- Create: `workers/rust/crates/sb-render/Cargo.toml`, `src/lib.rs`, `src/models.rs`, `src/error.rs`
- Create: `workers/rust/bins/scadbuddy-render-tools/Cargo.toml`, `src/main.rs`, `src/cli.rs`
- Test: `workers/rust/crates/sb-render/tests/vectors.rs`, `workers/rust/bins/scadbuddy-render-tools/tests/cli.rs`
- Modify: `workers/rust/README.md` (the two new members; drop "do not exist yet")

**Interfaces:**
- `sb_render::models`: serde mirrors of Task 1, field for field, no `deny_unknown_fields`, `#[serde(default)]` where Python defaults, no `skip_serializing_if` (Python dumps every field; `Option` serialises as `null`).
- `sb_render::error::ToolError { Unsupported(&'static str), Unreadable(&'static str), Io(std::io::Error), Internal(&'static str) }` → `(exit code, ToolRefusal)`. Messages are `&'static str` so no input value can reach them.
- CLI (hand-parsed `std::env::args`, no argument crate):
  ```
  scadbuddy-render-tools version
  scadbuddy-render-tools split <model.3mf> <out dir>
  scadbuddy-render-tools thumbnail <layout.json> <out dir>
  ```
  Every path is canonicalised (`std::fs::canonicalize`: absolute, symlinks resolved). The root is the canonical `<out dir>`'s parent (the piece's work dir); `split`'s input, `thumbnail`'s layout and every file the layout names are accepted only if their canonical path is under that root, compared by path components (`Path::starts_with`, not a string prefix), else exit 2. So `<work>/../x`, and a symlink in the work dir pointing out of it, are refused (Task 9's test). Exactly one JSON document on stdout, newline-terminated.
- `scadbuddy-render-tools` depends on `sb-render` only (not `sb-temporal`): `cargo build -p scadbuddy-render-tools` compiles no Temporal crate and needs no `protoc`.

- [ ] **Step 1: Write the failing tests** — `vectors.rs` round-trips every entry of `vectors/render_tools.json` (the `slice_plate` vectors test's shape); `cli.rs` runs the built binary (`env!("CARGO_BIN_EXE_scadbuddy-render-tools")`): `version` prints a `VersionResult` with `stages == []` at this task; no arguments exits 2; `split` of a missing file exits 1 with a `ToolRefusal{error:"io"}` on stdout.
- [ ] **Step 2: Run** `cd workers/rust && cargo test -p sb-render -p scadbuddy-render-tools` — Expected: FAIL to compile.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `cd workers/rust && cargo fmt --all --check && cargo clippy --workspace --all-targets --locked -- -D warnings && cargo test --workspace --locked` — Expected: PASS.
- [ ] **Step 5: Commit**

```bash
git add workers/rust
git commit -m "feat(rust): sb-render and the scadbuddy-render-tools CLI skeleton (#3824)"
```

---

### Task 3: `split`: the OpenSCAD 3MF split by material, streaming

**Files:**
- Create: `workers/rust/crates/sb-render/src/split.rs`, `src/meshfile.rs`
- Modify: `workers/rust/bins/scadbuddy-render-tools/src/cli.rs` (`split`; `version` lists it)
- Test: `workers/rust/crates/sb-render/tests/split.rs`, `tests/data/*.3mf` (small, hand-written, under 20 KB each)

**Interfaces:**
- ```rust
  pub struct Mesh { pub vertices: Vec<[f64; 3]>, pub faces: Vec<[u32; 3]> }
  pub struct Part { pub group: String, pub material_index: u32, pub name: String, pub colour: String,
                    pub mesh: Mesh, pub paint: Option<Vec<String>>, pub palette: Vec<String> }
  /// `3D/3dmodel.model` of `path`, streamed through quick-xml from the zip entry: never the whole XML in memory.
  pub fn split_by_material(path: &Path) -> Result<Vec<Part>, ToolError>;
  pub fn normalise_colour(displaycolor: Option<&str>) -> String;   // split.py's
  /// Writes <n>.vertices, <n>.faces (and <n>.paint) for each part, n from 1, into `out`; returns Task 1's SplitResult.
  pub fn write_parts(parts: &[Part], out: &Path) -> Result<SplitResult, ToolError>;
  ```
- Rules, each a test, all from `render/split.py`: namespace `http://schemas.microsoft.com/3dmanufacturing/core/2015/02`; a `<base>` with no `name` is `Material <i>`; `displaycolor` → `#` + first six hex digits upper case, missing → `#FFFFFF` (the alpha byte ignored, spec 09-22 §3); a triangle without `pid`/`p1` takes the object's `pid`/`pindex` (0 when absent); missing `v1..v3` are 0, missing `x/y/z` are 0.0; an object with no `<mesh>` is skipped; a mesh with no `<vertices>` is `Unreadable("mesh has no vertices")`; a key not in the materials is `Unreadable("triangle references unknown material")`; per object and key, vertices are those referenced, in original order, faces renumbered; keys sorted `(group, index)` as Python sorts `(str, int)`; one key's pieces from several objects concatenated in document order; `paint_color` read per triangle; `paint` is `Some` only when some face has a code, and `palette` is the group's colours by index.
- Memory: vertices of one object (the f64 array) plus the parts; the XML is never buffered whole.

- [ ] **Step 1: Write the failing tests** — one per rule above, on the small files in `tests/data/`; plus a 200k-triangle file generated in the test, checking it runs in under 2 s in release (`#[ignore]` in debug; CI runs `cargo test --release -p sb-render -- --ignored` in Task 12).
- [ ] **Step 2: Run** `cd workers/rust && cargo test -p sb-render --test split` — Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the Rust commands of Task 2 Step 4 — Expected: PASS.
- [ ] **Step 5: Commit**

```bash
git add workers/rust
git commit -m "feat(rust): render-tools split, the 3MF split by per-triangle material (#3824)"
```

---

### Task 4: Metrics: `impl` on the stage histogram and the tool runs counter

**Files:**
- Modify: `backend/scadbuddy/core/metrics.py` (`stage_duration`'s labels; `RenderStage` gains `"load"`; `render_tools_runs`; `mark_stage_impl`)
- Test: `backend/tests/test_render_stage_metrics.py` (new; `test_tracing_render_stages.py` and `test_worker.py` read the histogram too and must still pass)

**Interfaces:**
- ```python
  StageImpl = Literal["python", "rust"]
  RenderStage = Literal["source", "render", "split", "solids", "paint", "thumbnail", "write", "recolour", "load"]
  # stage_duration: Histogram("scadbuddy_render_stage_seconds", …, ["stage", "impl"], buckets=RENDER_BUCKETS)
  # render_tools_runs: Counter("scadbuddy_render_tools_runs_total", "Each scadbuddy-render-tools attempt by stage and outcome.", ["stage", "outcome"])
  def mark_stage_impl(impl: StageImpl) -> None:
      """Inside a `Metrics.stage` block: the implementation that produced the stage's result. A ContextVar
      holding a one-element list set by `stage` on entry, so a mark made in a thread the stage started
      (`asyncio.to_thread` copies the context, the list is shared) still reaches it. Outside a stage, nothing."""
  ```
  Both histogram label pairs and every `(stage, outcome)` are pre-created at start, as `stage` alone is today (`metrics.py` line 431).
- `deploy/grafana/*.json` queries already aggregate `sum by (le, stage)`; nothing there changes in this task. `lint-dashboard.sh` reads declared series from `core/metrics.py`, so the counter is declarable now and used in Task 14.

- [ ] **Step 1: Write the failing tests** — a stage with no mark observes `impl="python"`; a mark inside `asyncio.to_thread` inside the stage observes `impl="rust"`; `mark_stage_impl` outside a stage does nothing; the counter's pre-created series exist at 0.
- [ ] **Step 2: Run** `cd backend && uv run --frozen pytest tests/test_render_stage_metrics.py tests/test_tracing_render_stages.py tests/test_worker.py -v` — Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** pytest on the file, then `bash .github/scripts/lint-dashboard.sh` (with `KUSTOMIZE` per CLAUDE.md), `mypy`, `ruff` — Expected: PASS.
- [ ] **Step 5: Commit**

```bash
git add backend/scadbuddy/core/metrics.py backend/tests/test_render_stage_metrics.py
git commit -m "feat(metrics): label render stages by implementation; count render-tools runs (#3824)"
```

---

### Task 5: `render/tools.py`: find, run, parse, fall back

**Files:**
- Create: `backend/scadbuddy/render/tools.py`
- Modify: `backend/scadbuddy/core/settings.py` (bootstrap `render_tools: str = "scadbuddy-render-tools"`, its `bootstrap` reason, not in `APPLIES`), `backend/scadbuddy/core/config.py` (`Config.render_tools`)
- Test: `backend/tests/test_render_tools.py`, `backend/tests/support/fake_render_tools.py` (a Python script used as the binary: `FAKE_RT_MODE` = `ok`, `exit1`, `exit3`, `garbage`, `hang`, `missing-file`, `child` (forks a grandchild that sleeps and records its pid))

**Interfaces:**
- ```python
  @dataclass(frozen=True)
  class ToolRun[T: BaseModel]:
      outcome: ToolOutcome
      result: T | None                  # set only when outcome == "ok"

  class RenderTools:
      def __init__(self, binary: str, *, metrics: Metrics | None) -> None
      def available(self) -> bool       # shutil.which, cached; "absent" logged once per process
      async def run[T: BaseModel](self, stage: RustStage, args: Sequence[str], model: type[T], *,
                                  cwd: Path, timeout: float) -> ToolRun[T]:
          """Start the binary in its own session, wait up to ``timeout``, parse stdout as ``model`` on exit 0
          (``ToolRefusal`` otherwise). Kills the group on timeout and on cancel, and returns (or re-raises the
          cancel) only once the child is reaped. Never raises for the child's failures: every one is an outcome,
          counted in ``render_tools_runs`` and logged once per (stage, outcome) per process."""
  ```
- Outcomes: `FileNotFoundError` at exec or `available() is False` → `absent`; exit 3 → `unsupported` (logged at `info`, it is expected for painted layouts); exit 0 with stdout that is not the model, or naming a file not in `cwd` → `failed`; other exit → `failed`; overrun → `timeout`.

- [ ] **Step 1: Write the failing tests** — one per mode; `child` checks that after a timeout neither the child nor the grandchild pid exists (`os.kill(pid, 0)` raises); a cancelled `run` returns only after the child is reaped; each outcome increments its counter series; stderr never appears in a log record beyond its last 2 KB and never in a span attribute (`spans` fixture).
- [ ] **Step 2: Run** `cd backend && uv run --frozen pytest tests/test_render_tools.py -v` — Expected: FAIL.
- [ ] **Step 3: Implement** (`_kill_group` reused from `workflows/template_process.py`, moved to `core/` if importing it from `render/` would cycle).
- [ ] **Step 4: Run** the file, `mypy`, `ruff` — Expected: PASS.
- [ ] **Step 5: Commit**

```bash
git add backend/scadbuddy/render/tools.py backend/scadbuddy/core/settings.py backend/scadbuddy/core/config.py backend/tests/test_render_tools.py backend/tests/support/fake_render_tools.py
git commit -m "feat(render): run scadbuddy-render-tools in its own process group with a Python fallback (#3824)"
```

---

### Task 6: The `render_rust_stages` setting, live on every worker

**Files:**
- Modify: `backend/scadbuddy/core/settings.py` (field, validator, `APPLIES["render_rust_stages"] = "live"`), `backend/scadbuddy/core/config.py` (`Config.render_rust_stages: frozenset[RustStage]`)
- Modify: `backend/scadbuddy/library/settings_store.py` (`StoredSettings`/update model field; `RENDER_FIELDS`; `RenderStoreSettings.render_rust_stages`)
- Create: `backend/scadbuddy/migrations/<yyyymmdd>T<hhmm>Z_render_settings_rust_stages.sql` (`CREATE OR REPLACE VIEW render_settings …`: the latest view's body, `20261016T1759Z_render_settings_s3.sql`, with `'render_rust_stages'` added to the `IN` list; take the body from whatever file is newest on `main` when this lands)
- Modify: `backend/scadbuddy/workflows/activities.py` (`WorkerDeps.rust_stages: Callable[[], Awaitable[frozenset[RustStage]]]`, default from `config`), `backend/scadbuddy/worker.py` (the standalone worker's comes from `RenderSettingsSource.current()`, 30 s TTL; the in-process worker's from the live `Config`)
- Modify: `backend/scadbuddy/api/settings.py` (read and write model field), `frontend/src/pages/settings/fields.ts` (a three-checkbox field in the render section, beside `solid_concurrency`), `frontend/src/mocks/fixtures.ts`
- Test: `backend/tests/test_settings_render_rust_stages.py`, `backend/tests/test_render_worker_role.py` (a case), `frontend/src/pages/settings/*.test.tsx` (a case)

**Interfaces:**
- `SCADBUDDY_RENDER_RUST_STAGES` seeds it: a comma-separated list or a JSON list; blanks dropped; an unknown name is a `ValueError` naming the field and the name (`check_value`'s message). Stored as a sorted JSON list. Default empty.
- A worker that cannot re-read keeps the last good set (`RenderSettingsSource`'s rule); a first read that fails is the empty set, logged, not a start failure (the stage set never blocks a render).

- [ ] **Step 1: Write the failing tests** — env seeding (`"thumbnail, split"` → both; `"solids"` → error naming it); `APPLIES` says live; `PUT /settings` then `GET` round-trips; as role `scadbuddy_render`, `load_render_store_settings` reads a stored `["thumbnail"]` (fails without the migration: the view hides the row); the standalone worker's `rust_stages()` sees a change after the TTL with an injected clock.
- [ ] **Step 2: Run** `cd backend && SCADBUDDY_TEST_DATABASE_URL=… uv run --frozen pytest tests/test_settings_render_rust_stages.py tests/test_render_worker_role.py -v` — Expected: FAIL.
- [ ] **Step 3: Implement**; the migration file named with `date -u +%Y%m%dT%H%MZ`.
- [ ] **Step 4: Run** the backend files, `mypy`, `ruff`; `cd frontend && pnpm lint && pnpm typecheck && pnpm test` — Expected: PASS.
- [ ] **Step 5: Commit**

```bash
git add backend/scadbuddy backend/tests frontend/src
git commit -m "feat(settings): render_rust_stages, live on the API and the render workers (#3824)"
```

---

### Task 7: The split stage through the CLI, with exact parity

**Files:**
- Modify: `backend/scadbuddy/render/split.py` (`load_split(result: SplitResult, out: Path) -> list[ColourPart]`), `backend/scadbuddy/render/jobs.py` (`split_parts(path, *, tools, stages) -> list[ColourPart]`, used by `_render_solids`, `plate_layout`, `PlateLayout.load`), `backend/scadbuddy/render/solids.py` (`solid_mesh` takes the same splitter), `finish_piece_stage` (`PlateLayout.load` under `stage("load")`)
- Test: `backend/tests/test_split_parity.py`; cases in `backend/tests/test_jobs.py`

**Interfaces:**
- ```python
  async def split_parts(path: Path, *, tools: RenderTools | None, stages: frozenset[RustStage],
                        timeout: float) -> list[ColourPart]:
      """split_by_material's parts: from the CLI when "split" is in ``stages`` and it answers ok (marking the
      stage impl "rust"), else from split_by_material. The CLI writes into a fresh ``.split-<uuid>`` dir beside
      ``path`` (dot-named, so `pack_dir_to` never packs it), removed once the arrays are loaded."""
  ```
  `PlateLayout.load` becomes an async classmethod over `split_parts` (its one caller, `finish_piece_stage`, already awaits it in a thread; the thread moves inside `split_parts`' Python branch). `paint` from `<n>.paint` builds `Paint(codes, palette)` as `split_by_material` does.
- `timeout` is what is left of the job's `render_timeout` when the stage starts (the job's deadline, as Task 10's thumbnails take theirs), never a fresh full `render_timeout`, so the CLI never runs the job past its deadline. A split overrun falls back to Python, which runs unbounded as `split_by_material` does today: the stage must produce the parts (Global constraints: a Rust stage never fails a render Python would have finished), and the worst case is today's Python split after the deadline the job already had, not twice the split budget.

- [ ] **Step 1: Write the failing tests** — `test_split_parity.py`, marked `requires_render_tools` (new marker: skips unless `SCADBUDDY_RENDER_TOOLS` or the default resolves on `PATH`; with `SCADBUDDY_TEST_RENDER_TOOLS=required` it fails instead, as `requires_fixtures` does with `SCADBUDDY_TEST_FIXTURES`): for the fake openscad's `FAKE_3MF`, every small `tests/data` 3MF, and (also `requires_fixtures`) each MakerWorld fixture written by `write_openscad_3mf`, the CLI's parts equal `split_by_material`'s: same order, `material_index`, `name`, `colour`, `np.array_equal` vertices and faces, equal `paint`. A painted part (fixture 2069 re-written with its `paint_color` codes) included. `test_jobs.py`: with stages empty the CLI is never started (a `RenderTools` spy); with `{"split"}` and an absent binary the result equals the Python one and the counter says `absent`.
- [ ] **Step 2: Run** `cd backend && PATH=$PWD/../workers/rust/target/release:$PATH uv run --frozen pytest tests/test_split_parity.py tests/test_jobs.py -v` (after `cargo build --release -p scadbuddy-render-tools` in `workers/rust`) — Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** those, then the whole backend suite (`pytest -n auto`), `mypy`, `ruff` — Expected: PASS. Run `python -m tests.support.render_bench --rust …` on the four fixtures of the baseline table and record `rust_split_s` beside `split_s` in this plan's table (commit the edit with the code).
- [ ] **Step 5: Commit**

```bash
git add backend workers/rust docs/superpowers/plans/2026-10-18-render-tools.md
git commit -m "feat(render): split through scadbuddy-render-tools behind render_rust_stages (#3824)"
```

---

### Task 8: The rasteriser in Rust

**Files:**
- Create: `workers/rust/crates/sb-render/src/raster.rs` (camera basis, projection, z-buffer, shading), `src/image.rs` (premultiplied box downsample, PNG encode), `src/views.rs` (the plate cover's four images)
- Create: `workers/rust/vectors/thumbnail/` (written by Task 8's Python test: `<case>.layout.json`, its mesh files, and Python's four PNGs per case)
- Test: `workers/rust/crates/sb-render/tests/raster.rs`, `backend/tests/test_thumbnail_vectors.py` (writes the vectors with `UPDATE_RENDER_TOOLS_VECTORS=1`)

**Interfaces:**
- ```rust
  pub const PLATE_PNG_SIZE: usize = 512; pub const PLATE_SMALL_PNG_SIZE: usize = 128; pub const SUPERSAMPLE: usize = 2;
  pub const VIEW_POSITION: [f64; 3] = [0.45, -0.85, 1.1]; pub const MARGIN: f64 = 0.08;
  pub const AMBIENT: f64 = 0.42; pub const LIGHT_DIRECTION: [f64; 3] = [0.35, 0.45, 1.0];
  pub struct DrawPart<'a> { pub mesh: &'a Mesh, pub rgb: [u8; 3] }
  pub fn rasterise(parts: &[DrawPart], forward: [f64; 3], size: usize, shaded: bool) -> Rgba;   // _rasterise, look=None
  pub fn downsample(image: &Rgba, size: usize) -> Rgba;                                          // _downsample
  pub fn encode_png(image: &Rgba) -> Vec<u8>;
  pub struct Covers { pub plate: Vec<u8>, pub plate_small: Vec<u8>, pub top: Vec<u8>, pub pick: Vec<u8> }
  pub fn plate_covers(parts: &[DrawPart]) -> Covers;                                             // render_plate_thumbnails, unpainted
  ```
- Ported from `render/thumbnail.py` with its constants, each with the Python line it mirrors in a comment: `_basis` (world up Z; for `|ahead.z| > 0.999` up is `(0, 1 if ahead.z <= 0 else -1, 0)`), the AABB-corner span and `scale = size*(1-2*MARGIN)/span` (1.0 when span is 0), light in camera space `unit(LIGHT · [right, up, -ahead])`, flat normal `unit((b-a)×(c-a))` or zero for a degenerate face, `lit = AMBIENT + (1-AMBIENT)*clamp(n·light, 0, 1)`, screen `(size/2 + m·right*scale, size/2 - m·up*scale, m·ahead)`, pixel centres at `+0.5`, coverage by the two barycentric weights with `wc >= 0 && wb >= 0 && wc + wb <= 1`, a triangle skipped when its pixel box is empty or its signed area is 0, depth interpolated as Python does, `depth <= buffer` keeps the later fragment, colour `round_ties_even(clamp(c,0,1)*255)`; downsample premultiplied by alpha then divided back, `round_ties_even`; pick colours `(0, (i>>8)&0xFF, i&0xFF)` from 1, not supersampled, unshaded.
- Per triangle, the bounding-box scanline replaces Python's batching (it exists only to amortise numpy calls). Parallelism: the three views on three threads (`std::thread::scope`), each view drawn in part and triangle order so Review Focus 3 holds; `SCADBUDDY_RENDER_TOOLS_THREADS` (default 3, 1 for serial) caps it. No per-triangle threading.

- [ ] **Step 1: Write the Python vector test** — cases: a unit cube in one colour; two interpenetrating cubes in two colours (the tie rule); a cube whose colour is `#808080` with a face lit to exactly .5 before quantisation (Review Focus 2: pick the vertex so `lit*128/255*255` lands on n+.5; assert Python's value is the even one); a degenerate triangle among good ones; a part straight-down-only (the vertical-view basis). Writes each case's layout and Python's PNGs.
- [ ] **Step 2: Write the failing Rust test** — for each case, `plate_covers` decoded vs Python's PNGs decoded, under Task 10's tolerance function (`sb_render::compare::within_tolerance`, implemented here, mirrored in Python in Task 10); the `.5` case exact at that pixel.
- [ ] **Step 3: Run** `cd backend && UPDATE_RENDER_TOOLS_VECTORS=1 uv run --frozen pytest tests/test_thumbnail_vectors.py` then `cd workers/rust && cargo test -p sb-render --test raster` — Expected: Python PASS, Rust FAIL.
- [ ] **Step 4: Implement**, then the Rust commands of Task 2 Step 4 — Expected: PASS.
- [ ] **Step 5: Commit**

```bash
git add workers/rust backend/tests/test_thumbnail_vectors.py
git commit -m "feat(rust): render-tools rasteriser, a port of the plate cover renderer (#3824)"
```

---

### Task 9: `thumbnail`: every plate of a layout

**Files:**
- Create: `workers/rust/crates/sb-render/src/layout.rs`
- Modify: `workers/rust/bins/scadbuddy-render-tools/src/cli.rs` (`thumbnail`; `version` lists it)
- Test: `workers/rust/crates/sb-render/tests/layout.rs`

**Interfaces:**
- ```rust
  /// layout.json as `PlateLayout.save` writes it (render/jobs.py): plates[].parts[] with material_index, name,
  /// colour, file, solid, and optionally negative, paint, paint_reversed, object.
  pub fn read_layout(path: &Path) -> Result<Layout, ToolError>;
  /// Each plate's non-negative parts as meshes: a solid part's file split and its parts concatenated in order
  /// (solid_mesh), a split part the material of its file's split (each file split once); refuses with
  /// Unsupported("painted layout") when any part has `paint`, or its split carries paint codes.
  pub fn plate_meshes(layout: &Layout, work: &Path) -> Result<Vec<Vec<(Mesh, [u8; 3])>>, ToolError>;
  ```
  Output files `plate_<k>.png`, `plate_<k>_small.png`, `top_<k>.png`, `pick_<k>.png` (k from 1) in `<out dir>`, each written to `.part` then renamed; `ThumbnailResult.plates` names them.
- A solid file that holds nothing is `Unreadable("solid holds no geometry")`, as `PlateLayout.load` raises.

- [ ] **Step 1: Write the failing tests** — a two-plate layout over the Task 8 vectors' files; a negative part left out; a painted part → exit 3 and `ToolRefusal{error:"unsupported"}`; a `file` naming `../x` → exit 2.
- [ ] **Step 2: Run** `cd workers/rust && cargo test -p sb-render --test layout && cargo test -p scadbuddy-render-tools` — Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the Rust commands of Task 2 Step 4 — Expected: PASS.
- [ ] **Step 5: Commit**

```bash
git add workers/rust
git commit -m "feat(rust): render-tools thumbnail draws every plate of a layout (#3824)"
```

---

### Task 10: The thumbnail stage through the CLI, with measured parity

**Files:**
- Modify: `backend/scadbuddy/render/jobs.py` (`plates_thumbnails` takes `tools`, `stages`, `work`; `finish_piece_stage` passes them), `backend/scadbuddy/workflows/activities.py` (`finish_piece` passes `await d.rust_stages()` and the deps' `RenderTools`), `backend/scadbuddy/worker.py` (builds one `RenderTools` per process)
- Create: `backend/scadbuddy/render/image_compare.py` (`decode_png`, `within_tolerance`, the bounds of the measurement section; mirrors `sb_render::compare`)
- Test: `backend/tests/test_thumbnail_parity.py`, cases in `backend/tests/test_jobs.py`

**Interfaces:**
- ```python
  async def plates_thumbnails(plates, *, config, executor=None, tools: RenderTools | None = None,
                              stages: frozenset[RustStage] = frozenset(), layout_path: Path | None = None,
                              ) -> tuple[list[PlateThumbnails] | None, list[str]]:
      """As today; with "thumbnail" in ``stages`` and a layout path, the CLI first, under the same
      ``render_timeout`` budget: ok → its PNGs (impl "rust"); timeout → THUMBNAIL_TIMEOUT_WARNING and no covers,
      exactly today's degradation (no Python retry past the budget); absent, unsupported or failed → today's
      Python path with the budget that is left."""
  @dataclass(frozen=True)
  class Tolerance:
      coverage: float = 0.005; differing: float = 0.005; far: float = 0.0005; far_floor: int = 8
      far_delta: int = 32; pick: float = 0.0005
  def within_tolerance(python: np.ndarray, rust: np.ndarray, *, pick: bool, bounds: Tolerance = Tolerance()) -> list[str]:
      """The bounds broken, as sentences; [] when within."""
  ```
- The CLI writes into `<work>/.thumbnail-<uuid>/` (dot-named: never packed), read into `PlateThumbnails`, removed. The Rust timeout kills the child, which Python's thread budget never could (`plate_thumbnails`' docstring), so the orphan thread caveat no longer applies on this path.

- [ ] **Step 1: Write the failing tests** — `test_thumbnail_parity.py` (`requires_render_tools`; the fixtures part also `requires_fixtures`): for every unpainted fixture in the manifest (painted ones assert exit 3 and the Python fallback), and for each `models/*` render the fake openscad makes in `tests/api`, Rust's four images vs Python's under `within_tolerance`; the docstring records the worst observed per bound. `test_jobs.py`: stages empty → no CLI start; `{"thumbnail"}` with the fake binary in `hang` → `THUMBNAIL_TIMEOUT_WARNING`, no covers, no process left; with `exit1` → Python's covers and `failed` counted; painted layout → Python's covers and `unsupported`.
- [ ] **Step 2: Run** (binary on `PATH` as in Task 7) `uv run --frozen pytest tests/test_thumbnail_parity.py tests/test_jobs.py -v` — Expected: FAIL.
- [ ] **Step 3: Implement.** Then tighten: if any bound is more than 10x the worst observed, lower it to 5x and say so in the docstring.
- [ ] **Step 4: Run** the backend suite, `mypy`, `ruff`; `render_bench --rust` on the baseline fixtures, `rust_thumbnail_s` recorded in this plan's table — Expected: PASS.
- [ ] **Step 5: Commit**

```bash
git add backend docs/superpowers/plans/2026-10-18-render-tools.md
git commit -m "feat(render): plate covers through scadbuddy-render-tools behind render_rust_stages (#3824)"
```

---

### Task 11: Dockerfile: `render-tools-build`, and the binary in `app`

**Files:**
- Modify: `Dockerfile` (a stage before `app`; one `COPY` and one `RUN` in `app`)
- Modify: `README.md` ("Deploying": the setting and the rollout order)

**Interfaces:**
- ```dockerfile
  # ── render-tools-build: scadbuddy-render-tools (#3824, spec 2026-10-16 §7.2) ──
  # The CLI the render worker runs beside openscad. Same toolchain image as slicer-build
  # (rust-toolchain.toml's 1.99.1, bumped together); no protoc: the binary depends on no Temporal crate.
  # Built on bookworm (glibc 2.36); runs on base's trixie (2.41).
  FROM rust:1.99.1-bookworm@sha256:e12566d5abc17a0da741628782cdb504b68b8961f5bfcf677936bf3e8b32de29 AS render-tools-build
  COPY workers/rust /src
  WORKDIR /src
  RUN cargo build --release --locked -p scadbuddy-render-tools
  …
  # in `app`, beside openscad-lsp:
  COPY --from=render-tools-build /src/target/release/scadbuddy-render-tools /usr/local/bin/scadbuddy-render-tools
  RUN scadbuddy-render-tools version
  ```
  `test` and `runtime` inherit it from `app`. The `slicer` image does not get it.
- `.dockerignore` already excludes `workers/rust/target` (line 32), so the COPY carries no local build.

- [ ] **Step 1: Build** `docker build --target app -t scadbuddy:rt .` — Expected: the `RUN … version` line prints a `VersionResult` listing `split` and `thumbnail`.
- [ ] **Step 2: Run** `docker run --rm --entrypoint scadbuddy-render-tools scadbuddy:rt version` and `hadolint --config .hadolint.yaml Dockerfile` — Expected: PASS.
- [ ] **Step 3: Run** the parity tests in the image: `docker build --target test -t scadbuddy:test . && docker run --rm -e SCADBUDDY_TEST_RENDER_TOOLS=required -v "$PWD/test-fixtures:/app/test-fixtures:ro" -e SCADBUDDY_TEST_FIXTURES=/app/test-fixtures scadbuddy:test uv run --frozen --no-sync pytest tests/test_split_parity.py tests/test_thumbnail_parity.py` — Expected: PASS, with real openscad renders included.
- [ ] **Step 4: Commit**

```bash
git add Dockerfile README.md
git commit -m "build: scadbuddy-render-tools in the app image beside openscad (#3824)"
```

---

### Task 12: CI

**Files:**
- Modify: `.github/workflows/ci.yml`

**Changes:**
- `rust` job: add `cargo test --release -p sb-render --locked -- --ignored` (Task 3's timing test) after `Test`. No other change: the workspace commands already cover the new members.
- `backend-pytest` shards run in the `test` image, which carries the binary from Task 11: set `SCADBUDDY_TEST_RENDER_TOOLS=required` beside `SCADBUDDY_TEST_FIXTURES`, so a missing binary fails the parity tests instead of skipping them.
- Refresh `backend/.test_durations` (`uv run --frozen pytest -n auto --store-durations` in the test image) so the new parity tests do not unbalance the shards.

- [ ] **Step 1: Run** `actionlint` and `.github/scripts/*.test.sh` locally — Expected: PASS.
- [ ] **Step 2: Commit**

```bash
git add .github/workflows/ci.yml backend/.test_durations
git commit -m "ci: render-tools parity tests required in the backend shards (#3824)"
```

---

### Task 13: Solids: the pool prod already has, sized for the render worker (measured)

**Files:**
- Modify: `backend/scadbuddy/core/config.py` (`Config.solid_slots`, new `checks_here: bool`; `default_solid_concurrency` unchanged), `backend/scadbuddy/worker.py` (the standalone render worker calls `solid_slots(checks_here=False)`: it serves no editor checks)
- Test: `backend/tests/test_config.py` (cases)

**Interfaces:**
- `default_solid_concurrency(cpus, render_concurrency, check_concurrency)` unchanged; `Config.solid_slots()` takes `checks_here: bool` (False on the standalone worker). On prod's worker: `(4 - 0) // 2` = 2.

- [ ] **Step 1: Re-measure** in the pinned image with `--cpus 4`, the Measured baselines' `xargs -P` procedure, on `models/fidget-slider-board` and two more multi-colour models (`dollhouse-kit`, `bookmark`), at 1–4 slots, and with two such jobs at once at 2 slots each (prod's `render_concurrency` 2): record in this plan. Expected: 2 slots each beats 1 for the pair; if two jobs at 2 slots are slower than at 1 (CPU contention past 4), stop and report instead of changing the default.
- [ ] **Step 2: Write the failing test** — a standalone worker's `Config` on 4 CPUs, `render_concurrency` 2, resolves 2 solid slots; the API process's still subtracts its checks.
- [ ] **Step 3: Implement**; run `tests/test_config.py`, `tests/test_solids.py`, `mypy`, `ruff` — Expected: PASS.
- [ ] **Step 4: Commit**

```bash
git add backend/scadbuddy/core/config.py backend/scadbuddy/worker.py backend/tests/test_config.py docs/superpowers/plans/2026-10-18-render-tools.md
git commit -m "perf(render): size the render worker's solid pool without the API's editor checks (#3824)"
```

---

### Task 14: Dashboard, docs, agent view

**Files:**
- Modify: `deploy/grafana/scadbuddy-renders.json` (a panel "Render stages by implementation": p95 `sum by (le, stage, impl)`; a panel "render-tools outcomes": `sum by (stage, outcome) (increase(scadbuddy_render_tools_runs_total[$__range]))`)
- Modify: `CLAUDE.md` (Layout: `crates/sb-render`, `bins/scadbuddy-render-tools`, `render/tools.py`, the vector file; Commands: the `requires_render_tools` marker and `SCADBUDDY_TEST_RENDER_TOOLS`), `workers/rust/README.md` (vectors section: `render_tools.json`, `vectors/thumbnail/`)

- [ ] **Step 1: Run** `KUSTOMIZE=… bash .github/scripts/lint-dashboard.sh` — Expected: PASS (both series declared in `core/metrics.py`).
- [ ] **Step 2: Commit**

```bash
git add deploy/grafana/scadbuddy-renders.json CLAUDE.md workers/rust/README.md
git commit -m "docs(render): render-tools in the dashboard, CLAUDE.md and the workspace README (#3824)"
```

---

### Task 15: Rollout and the follow-ups

Not code. On prod, in order (spec §8): set `solid_concurrency` 2 now (Decision 1, live); after Tasks 1–12 deploy, add `split` to `render_rust_stages`, watch the outcomes panel and `scadbuddy_render_stage_seconds{stage="split",impl="rust"}` for a day; then `thumbnail`. File issues (sub-issues of #3818, `renderer` label, each citing this plan): painted covers in Rust (port `paint.pieces` and carried paint; Decision 2); `write_glb` in Rust if Task 7's numbers leave the split stage's GLB share above 1 s on the pumpkin; the `store` stage (Tasks 16–17) if Decision 5 moves it out.

---

### Task 16: `store` (part 1): pack and unpack — measure first

**Files:**
- Modify: `backend/scadbuddy/store/archive.py` (only if Step 1 says so), or Create: `workers/rust/crates/sb-render/src/archive.rs` and subcommands `pack <dir> <out.zip>` / `unpack <zip> <dir>`

- [ ] **Step 1: Measure** with `render_bench`'s pack mode on real pieces (Task 11's image, the pumpkin and ghost renders): Python `pack_dir_to` as is, Python with `ZIP_STORED` for `.3mf`/`.png` entries, and (if built) Rust's. Record in this plan. If stored-entries Python is within 20% of Rust, ship that (a Python change, no stage) and close this task.
- [ ] **Step 2 (Rust only): Write the failing tests** — the Rust pack is deterministic (same tree, same bytes, twice), skips dot-named and non-regular files as `pack_dir_to` does, and Python's `unpack_dir` opens it; the Rust unpack refuses an entry outside its directory (`../`, absolute) and swaps the directory in as `_swap_in` does, so `read_marker` semantics hold.
- [ ] **Step 3: Implement**, wired through `RenderTools` in `CachedBlobStore._publish`/`_fetch` behind `"store"`.
- [ ] **Step 4: Commit** `feat(store): pack and unpack pieces through scadbuddy-render-tools (#3824)`.

### Task 17: `store` (part 2): put and get over transfer URLs

Blocked until `slicing/transfer.py` (`TransferUrl`, native slicer plan Task 10) is on `main`. `put <file> <url.json>` / `get <url.json> <file>` reuse `sb-transfer` (the slicer's streaming GET/PUT with sha256); the worker mints the URLs it would otherwise use credentials for, so the CLI holds none (spec §5.2). This removes `ContentStore.read`'s whole-blob path for pieces (spec §4.1). Plan it in its own document when unblocked.

---

## Self-review notes

- **Spec coverage.** §7.1 layout (`crates/sb-render`, `bins/scadbuddy-render-tools`), vectors written by Python and opened by Rust, the stage histogram under its own name → Tasks 1–4, 8. §7.2 a CLI beside the activity, in the `app` image beside `openscad`, no Temporal SDK, no queue, no `RenderPiece` history change → Tasks 2, 5, 11, Global Constraints. §7.3 step 1 thumbnail with a measured tolerance → Measured baselines, Tasks 8–10; step 2 split → Tasks 3, 7; step 3 solids measured first → Measured baselines, Task 13; step 4 pack/unpack/put/get → Tasks 16–17 (gated); step 5 is not this plan's. §8 `render_rust_stages`, live, default empty → Task 6 (minus `solids`, Decision 1). Fallback → Global Constraints, Tasks 5, 7, 10. CI → Tasks 2 (existing `rust` job), 12.
- **Types.** `RustStage`, `ToolOutcome`, `SplitResult`, `ThumbnailResult`, `ToolRefusal` (Task 1) are the names in Tasks 2, 3, 5, 7, 9, 10; `RenderTools.run` (Task 5) in Tasks 7, 10, 16; `mark_stage_impl` (Task 4) in Tasks 7, 10; `split_parts` (Task 7) in Task 9's mirror and `PlateLayout.load`; `within_tolerance` (Task 8 Rust, Task 10 Python).
- **Review Focus.** 1 → Tasks 3, 7; 2, 3 → Task 8; 4 → Tasks 5, 10; 5 → Task 6.
- **Not measured here.** Rust's own times (no binary yet): Tasks 7 and 10 record them in the baseline table. Prod CPU speed relative to this workstation: the prod trace is the reference for the win, through `impl`.
