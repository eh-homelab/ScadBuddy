# Rust workers

ScadBuddy's Rust Temporal workers and tools (spec
`docs/superpowers/specs/2026-10-16-own-store-slicer-rust-workers-design.md` §7).

```
Cargo.toml                    # the workspace
crates/sb-transfer/           # TransferUrl GET/PUT streaming with sha256
crates/sb-temporal/           # worker bootstrap: connect, versioning, OTel, /healthz + /metrics (slicer only)
crates/sb-slicer/             # slice_plate: its models (src/models.rs) and the activity
bins/scadbuddy-slicer/        # the `slicer` queue's worker
vectors/                      # payloads pinned against the pydantic models
```

`crates/sb-render/` and `bins/scadbuddy-render-tools/` (spec §7.3, a CLI the Python
render worker runs beside openscad, not a Temporal worker) come with the render-tools
plan and do not exist yet.

## Commands (what CI's `rust` job runs)

```bash
cd workers/rust
cargo fmt --all --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace
```

`rust-toolchain.toml` pins the toolchain; rustup installs it on first use. The
Temporal Core SDK's build needs `protoc` on `PATH` (`apt-get install
protobuf-compiler`, or a release from github.com/protocolbuffers/protobuf).
`Cargo.lock` is committed: the binaries build `--locked`.

## Vectors

Each `vectors/<activity>.json` is written by the Python tests and opened by the Rust
tests, which round-trip every entry and compare it as JSON. `slice_plate.json` comes
from `backend/tests/slicing/test_models.py`; regenerate it with
`UPDATE_SLICE_VECTORS=1 uv run --frozen pytest tests/slicing/test_models.py` in
`backend/`. A field added on one side only fails the Rust round trip.
