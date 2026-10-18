use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use sb_slicer::models::{Arrange, SliceProgress};
use sb_slicer::runner::{KEEPALIVE, RunError, RunOutcome, RunRequest, command_line, run};
use tokio_util::sync::CancellationToken;

fn fake(name: &str) -> PathBuf {
    let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests");
    match name {
        "" => dir.join("fake-bambu-studio.sh"),
        mode => dir.join("fake").join(format!("{mode}.sh")),
    }
}

struct Paths {
    _dir: tempfile::TempDir,
    input: PathBuf,
    machine: PathBuf,
    process: PathBuf,
    filaments: Vec<PathBuf>,
    out: PathBuf,
}

fn paths() -> Paths {
    let dir = tempfile::tempdir().unwrap();
    let p = |n: &str| dir.path().join(n);
    Paths {
        input: p("input.3mf"),
        machine: p("machine.json"),
        process: p("process.json"),
        filaments: vec![p("filament_1.json"), p("filament_2.json")],
        out: p("sliced.3mf"),
        _dir: dir,
    }
}

fn request<'a>(cli: &'a Path, p: &'a Paths) -> RunRequest<'a> {
    RunRequest {
        cli,
        input_3mf: &p.input,
        machine: &p.machine,
        process: &p.process,
        filaments: &p.filaments,
        out_3mf: &p.out,
        slicedata_dir: None,
        plate: 1,
        arrange: Arrange::No,
        allow_newer_file: true,
        debug: 1,
        keepalive: KEEPALIVE,
    }
}

async fn run_fake(mode: &str) -> (Result<RunOutcome, RunError>, Vec<SliceProgress>, Paths) {
    let cli = fake(mode);
    let p = paths();
    let mut seen = Vec::new();
    let result = run(
        request(&cli, &p),
        |progress| seen.push(progress),
        CancellationToken::new(),
    )
    .await;
    (result, seen, p)
}

#[tokio::test]
async fn a_slice_reports_progress_warnings_and_its_output() {
    let (result, seen, p) = run_fake("").await;
    let outcome = result.unwrap();
    assert_eq!(outcome.exit_code, 0);
    assert_eq!(outcome.warnings, vec!["thin wall".to_owned()]);
    assert_eq!(seen.last().unwrap().total_percent, 100);
    assert_eq!(seen.last().unwrap().message, "Done");
    assert!(p.out.exists());
    assert!(outcome.stderr_tail.contains("fake stderr line"));
}

#[tokio::test]
async fn a_progress_line_split_across_reads_is_read_whole() {
    let (result, seen, _p) = run_fake("split").await;
    let outcome = result.unwrap();
    assert_eq!(outcome.exit_code, 0);
    assert_eq!(outcome.warnings, vec!["thin wall".to_owned()]);
    let percents: Vec<u32> = seen.iter().map(|s| s.total_percent).collect();
    assert_eq!(percents, vec![40, 100]);
}

#[tokio::test]
async fn a_cli_that_never_opens_the_pipe_still_returns() {
    let started = Instant::now();
    let (result, seen, p) = tokio::time::timeout(Duration::from_secs(5), run_fake("silent-192"))
        .await
        .expect("returns within 5 s");
    let outcome = result.unwrap();
    assert!(started.elapsed() < Duration::from_secs(5));
    assert_eq!(outcome.exit_code, 192);
    assert!(seen.is_empty());
    assert!(!p.out.exists());
}

#[tokio::test]
async fn a_cancelled_slice_leaves_no_process_behind() {
    let cli = fake("hang");
    let p = paths();
    let cancel = CancellationToken::new();
    let trigger = cancel.clone();
    tokio::spawn(async move {
        tokio::time::sleep(Duration::from_millis(200)).await;
        trigger.cancel();
    });
    let result = tokio::time::timeout(
        Duration::from_secs(2),
        run(request(&cli, &p), |_| {}, cancel),
    )
    .await
    .expect("returns within 2 s");
    let Err(RunError::Cancelled { pgid }) = result else {
        panic!("expected Cancelled, got {result:?}");
    };
    assert_group_gone(pgid).await;
}

#[test]
fn command_line_is_the_argv_the_spec_fixes() {
    let cli = fake("");
    let p = paths();
    let slicedata = p.out.with_file_name("slicedata");
    let mut req = request(&cli, &p);
    req.arrange = Arrange::Yes;
    req.slicedata_dir = Some(&slicedata);
    req.debug = 2;
    let fifo = Path::new("/tmp/x/progress.fifo");
    let argv: Vec<String> = command_line(&req, fifo)
        .into_iter()
        .map(|a| a.into_string().unwrap())
        .collect();
    let s = |p: &Path| p.to_str().unwrap().to_owned();
    assert_eq!(
        argv,
        vec![
            "--pipe".to_owned(),
            s(fifo),
            "--debug".into(),
            "2".into(),
            "--load-settings".into(),
            format!("{};{}", s(&p.machine), s(&p.process)),
            "--load-filaments".into(),
            format!("{};{}", s(&p.filaments[0]), s(&p.filaments[1])),
            "--allow-newer-file".into(),
            "--arrange".into(),
            "1".into(),
            "--slice".into(),
            "1".into(),
            "--export-slicedata".into(),
            s(&slicedata),
            "--export-3mf".into(),
            s(&p.out),
            s(&p.input),
        ]
    );

    req.allow_newer_file = false;
    req.slicedata_dir = None;
    req.arrange = Arrange::Auto;
    let argv: Vec<String> = command_line(&req, fifo)
        .into_iter()
        .map(|a| a.into_string().unwrap())
        .collect();
    assert!(!argv.contains(&"--allow-newer-file".to_owned()));
    assert!(!argv.contains(&"--export-slicedata".to_owned()));
    let i = argv.iter().position(|a| a == "--arrange").unwrap();
    assert_eq!(argv[i + 1], "auto");
}

/// Waits until no process is left in `pgid`; the killed `sleep` is reaped by init a
/// moment after the kill.
async fn assert_group_gone(pgid: i32) {
    let deadline = Instant::now() + Duration::from_secs(2);
    loop {
        let out = std::process::Command::new("pgrep")
            .args(["-g", &pgid.to_string()])
            .output()
            .unwrap();
        if out.stdout.is_empty() {
            return;
        }
        assert!(
            Instant::now() < deadline,
            "group {pgid} still has {}",
            String::from_utf8_lossy(&out.stdout)
        );
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
}

#[tokio::test]
async fn a_silent_cli_still_heartbeats() {
    let cli = fake("quiet");
    let p = paths();
    let mut req = request(&cli, &p);
    req.keepalive = Duration::from_millis(800);
    let mut seen = Vec::new();
    let outcome = run(
        req,
        |progress| seen.push(progress),
        CancellationToken::new(),
    )
    .await
    .unwrap();
    assert_eq!(outcome.exit_code, 0);
    // 2.5 s of silence at an 800 ms keepalive: zeroed progress before the first line.
    let before_first = seen.iter().take_while(|s| s.total_percent == 0).count();
    assert!(
        before_first >= 2,
        "only {before_first} keepalives in {seen:?}"
    );
    assert_eq!(seen.last().unwrap().total_percent, 100);
}

#[tokio::test]
async fn a_slice_cancelled_while_silent_leaves_no_process_behind() {
    let cli = fake("quiet");
    let p = paths();
    let mut req = request(&cli, &p);
    req.keepalive = Duration::from_millis(300);
    let cancel = CancellationToken::new();
    let trigger = cancel.clone();
    tokio::spawn(async move {
        tokio::time::sleep(Duration::from_millis(1000)).await;
        trigger.cancel();
    });
    let mut beats = 0;
    let started = Instant::now();
    let result = run(req, |_| beats += 1, cancel).await;
    assert!(started.elapsed() < Duration::from_millis(1500));
    assert!(beats >= 2, "only {beats} keepalives");
    let Err(RunError::Cancelled { pgid }) = result else {
        panic!("expected Cancelled, got {result:?}");
    };
    assert_group_gone(pgid).await;
}
