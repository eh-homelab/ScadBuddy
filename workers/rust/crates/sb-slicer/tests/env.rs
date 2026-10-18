//! The CLI gets only the allowlisted environment (`runner::cli_env`). Its own test
//! binary: it sets a variable in the process environment, which no other test may see.

use std::path::Path;

use sb_slicer::models::Arrange;
use sb_slicer::runner::{KEEPALIVE, RunRequest, run};
use tokio_util::sync::CancellationToken;

#[tokio::test]
async fn the_cli_never_sees_the_workers_other_variables() {
    // SAFETY: the only test in this binary, set before anything reads the environment.
    unsafe { std::env::set_var("OTEL_EXPORTER_OTLP_HEADERS", "authorization=Bearer s3cret") };
    let cli = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fake/env.sh");
    let dir = tempfile::tempdir().unwrap();
    let p = |n: &str| dir.path().join(n);
    let filaments = vec![p("filament_1.json")];
    let (input, machine, process, out) = (p("input.3mf"), p("m.json"), p("p.json"), p("o.3mf"));
    let req = RunRequest {
        cli: &cli,
        input_3mf: &input,
        machine: &machine,
        process: &process,
        filaments: &filaments,
        out_3mf: &out,
        slicedata_dir: None,
        plate: 1,
        arrange: Arrange::No,
        allow_newer_file: false,
        debug: 1,
        keepalive: KEEPALIVE,
    };
    let outcome = run(req, |_| {}, CancellationToken::new()).await.unwrap();
    assert_eq!(outcome.exit_code, 0, "{}", outcome.stderr_tail);
    let names: Vec<&str> = outcome
        .stderr_tail
        .lines()
        .filter_map(|l| l.split_once('=').map(|(k, _)| k))
        .collect();
    assert!(!names.contains(&"OTEL_EXPORTER_OTLP_HEADERS"), "{names:?}");
    assert!(!names.contains(&"CARGO_MANIFEST_DIR"), "{names:?}");
    assert!(names.contains(&"PATH"), "{names:?}");
    assert!(names.contains(&"HOME"), "{names:?}");
}
