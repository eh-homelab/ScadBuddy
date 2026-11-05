# Rust workers — notes for agents

These notes cover `workers/rust/`; paths are repo-relative; the root `CLAUDE.md` has the repo-wide rules.

## Commands
Rust workers (`workers/rust/`, one Cargo workspace; the `rust` CI job):

```bash
cd workers/rust
cargo fmt --all --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace
```

`rust-toolchain.toml` pins the toolchain (rustup installs it). The Temporal Core SDK's
build needs `protoc` on `PATH` (`apt-get install protobuf-compiler`). `Cargo.lock` is
committed; CI runs clippy and the tests `--locked`, with the same Temporal CLI as the
`agent` job in `SCADBUDDY_TEST_TEMPORAL_DEV_SERVER`.


## Layout
- `workers/rust/` — Rust Temporal workers and tools (spec 2026-10-16 §7):
  `crates/sb-slicer` the `slice_plate` activity, `crates/sb-transfer` GET/PUT of
  `TransferUrl`s (each body's sha256 reported, never checked: a `TransferUrl` carries
  none), `crates/sb-temporal` the worker bootstrap (slicer only),
  `bins/scadbuddy-slicer` the `slicer` queue's worker; `vectors/` pins payloads against
  the pydantic models (`UPDATE_SLICE_VECTORS=1` in `backend/tests/slicing/test_models.py`).

## See also

- Python side of the slicer: see `backend/CLAUDE.md` (`slicing/`). Toolchain pin job: root "CI and caching rules".
