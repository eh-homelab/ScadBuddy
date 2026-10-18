//! The `slicer` queue's worker (spec 2026-10-16 §7). With no arguments it serves
//! `slice_plate` until SIGTERM; `--version`, and `--check-cli`, which the image build runs
//! against the pinned Bambu Studio.

use std::path::{Path, PathBuf};
use std::process::{Command, ExitCode, Stdio};
use std::sync::Arc;
use std::sync::atomic::AtomicBool;
use std::time::{Duration, Instant};

use sb_slicer::activity::SlicerActivities;
use sb_temporal::{Stages, WorkerEnv};
use sb_transfer::Transfer;
use scadbuddy_slicer::WorkerSettings;
use tokio::signal::unix::{SignalKind, signal};

/// The extracted AppImage's entry point (the Dockerfile's `slicer` stage).
const CLI: &str = "/opt/bambu-studio/AppRun";

/// Overrides [`CLI`], for running the worker outside the image.
const CLI_VAR: &str = "BAMBU_STUDIO_CLI";

/// Seconds running slices may go on after SIGTERM before they are cancelled.
const GRACE_VAR: &str = "SCADBUDDY_SLICER_SHUTDOWN_GRACE_SECONDS";
/// Below Kubernetes' default `terminationGracePeriodSeconds` (30), so cancelled slices
/// are killed and reported before the pod is.
const DEFAULT_GRACE: Duration = Duration::from_secs(20);

/// The Bambu Studio version the image pins, set by the Dockerfile.
const VERSION_VAR: &str = "BAMBU_STUDIO_VERSION";

/// How long `--help` may take before the check gives up, so a CLI that waits on
/// something (a display, a lock) fails the build instead of hanging it.
const HELP_TIMEOUT: Duration = Duration::from_secs(60);

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match args
        .iter()
        .map(String::as_str)
        .collect::<Vec<_>>()
        .as_slice()
    {
        [] => serve(),
        ["--version"] => {
            println!("scadbuddy-slicer {}", env!("CARGO_PKG_VERSION"));
            ExitCode::SUCCESS
        }
        ["--check-cli"] => match check_cli(Path::new(CLI)) {
            Ok(version) => {
                println!("bambu studio {version}: --help names --slice and --export-3mf");
                ExitCode::SUCCESS
            }
            Err(problem) => {
                eprintln!("scadbuddy-slicer --check-cli: {problem}");
                ExitCode::FAILURE
            }
        },
        _ => {
            eprintln!("usage: scadbuddy-slicer [--version | --check-cli]");
            ExitCode::from(2)
        }
    }
}

/// The worker: its environment, `/healthz` and `/metrics`, the Temporal connection, then
/// [`scadbuddy_slicer::run_worker`] until SIGTERM. A failure is logged (a value goes in a
/// field, never the message: the `sb_temporal` telemetry rule) and exits non-zero; one
/// before the log subscriber exists goes to stderr.
fn serve() -> ExitCode {
    let runtime = match tokio::runtime::Runtime::new() {
        Ok(r) => r,
        Err(e) => {
            eprintln!("scadbuddy-slicer: cannot start the async runtime: {e}");
            return ExitCode::FAILURE;
        }
    };
    runtime.block_on(async {
        let tracer = match sb_temporal::init_tracing() {
            Ok(t) => t,
            Err(e) => {
                eprintln!("scadbuddy-slicer: cannot start logging: {e:#}");
                return ExitCode::FAILURE;
            }
        };
        let code = match worker().await {
            Ok(()) => ExitCode::SUCCESS,
            Err(e) => {
                tracing::error!(error = format!("{e:#}"), "the slicer worker failed");
                ExitCode::FAILURE
            }
        };
        if let Some(provider) = tracer
            && let Err(e) = provider.shutdown()
        {
            tracing::warn!(error = %e, "could not flush the trace exporter");
        }
        code
    })
}

async fn worker() -> anyhow::Result<()> {
    let env = WorkerEnv::from_env(
        "SCADBUDDY_TEMPORAL_TASK_QUEUE_SLICER",
        "slicer",
        "SCADBUDDY_SLICER_CONCURRENCY",
    )?;
    let cli = std::env::var_os(CLI_VAR)
        .filter(|v| !v.is_empty())
        .map_or_else(|| PathBuf::from(CLI), PathBuf::from);
    let settings = WorkerSettings {
        task_queue: env.task_queue.clone(),
        concurrency: env.concurrency,
        shutdown_grace: shutdown_grace(std::env::var(GRACE_VAR).ok().as_deref())?,
    };
    let mut sigterm = signal(SignalKind::terminate())?;
    let registry = prometheus::Registry::new();
    let stages = Stages::register(&registry);
    let ready = Arc::new(AtomicBool::new(false));
    sb_temporal::serve_health(env.metrics_port, ready.clone(), registry);
    let runtime = temporalio_sdk::Runtime::from_current_tokio(Default::default())?;
    let client = sb_temporal::connect(&env, &runtime).await?;
    tracing::info!(
        task_queue = env.task_queue,
        concurrency = env.concurrency,
        shutdown_grace_seconds = settings.shutdown_grace.as_secs(),
        "serving slice_plate"
    );
    let activities = SlicerActivities {
        cli,
        transfer: Transfer::new(),
        stages,
    };
    let stop = async move {
        sigterm.recv().await;
    };
    scadbuddy_slicer::run_worker(&runtime, client, settings, activities, ready, stop).await
}

/// [`GRACE_VAR`]'s value, read as `WorkerEnv` reads the concurrency: unset or blank is
/// the default, anything else must be a whole number of seconds.
fn shutdown_grace(raw: Option<&str>) -> anyhow::Result<Duration> {
    match raw.map(str::trim).filter(|v| !v.is_empty()) {
        None => Ok(DEFAULT_GRACE),
        Some(v) => v
            .parse::<u64>()
            .map(Duration::from_secs)
            .map_err(|_| anyhow::anyhow!("{GRACE_VAR} must be a whole number of seconds")),
    }
}

/// Runs `<cli> --help` and checks its output with [`check_help`]. Never prints the CLI's
/// own output (telemetry rule: no CLI stderr in a log line); only what it lacked.
fn check_cli(cli: &Path) -> Result<String, String> {
    let version = std::env::var(VERSION_VAR)
        .ok()
        .filter(|v| !v.is_empty())
        .ok_or_else(|| format!("{VERSION_VAR} is not set"))?;
    let output = run_help(cli)?;
    check_help(&output, &version)?;
    Ok(version)
}

fn run_help(cli: &Path) -> Result<String, String> {
    let dir = tempdir()?;
    let mut child = Command::new(cli)
        .arg("--help")
        .current_dir(&dir)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| format!("cannot start the Bambu Studio CLI: {}", e.kind()))?;
    // Both pipes drained on threads, so a full one cannot stall the child.
    let stdout = child.stdout.take().map(drain);
    let stderr = child.stderr.take().map(drain);
    let started = Instant::now();
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break status,
            Ok(None) if started.elapsed() > HELP_TIMEOUT => {
                let _ = child.kill();
                let _ = child.wait();
                let _ = std::fs::remove_dir_all(&dir);
                return Err(format!(
                    "--help did not exit within {}s",
                    HELP_TIMEOUT.as_secs()
                ));
            }
            Ok(None) => std::thread::sleep(Duration::from_millis(100)),
            Err(e) => return Err(format!("waiting for --help failed: {}", e.kind())),
        }
    };
    let _ = std::fs::remove_dir_all(&dir);
    let mut text = String::new();
    for pipe in [stdout, stderr].into_iter().flatten() {
        text.push_str(&pipe.join().unwrap_or_default());
    }
    // Bambu Studio's --help exits 0; anything else is a CLI that did not start.
    if !status.success() {
        return Err(format!("--help exited with {status}"));
    }
    Ok(text)
}

fn drain(mut pipe: impl std::io::Read + Send + 'static) -> std::thread::JoinHandle<String> {
    std::thread::spawn(move || {
        let mut bytes = Vec::new();
        let _ = pipe.read_to_end(&mut bytes);
        String::from_utf8_lossy(&bytes).into_owned()
    })
}

/// A scratch working directory, so whatever the CLI writes beside itself lands in `/tmp`.
fn tempdir() -> Result<std::path::PathBuf, String> {
    let dir = std::env::temp_dir().join(format!("scadbuddy-slicer-check-{}", std::process::id()));
    std::fs::create_dir_all(&dir)
        .map_err(|e| format!("cannot create a scratch directory: {}", e.kind()))?;
    Ok(dir)
}

/// The output must name `--slice` and `--export-3mf` as options and carry `version`.
fn check_help(output: &str, version: &str) -> Result<(), String> {
    let mut missing = Vec::new();
    for option in ["--slice", "--export-3mf"] {
        if !names_option(output, option) {
            missing.push(option);
        }
    }
    if !missing.is_empty() {
        return Err(format!("--help does not name {}", missing.join(" or ")));
    }
    if !output.contains(version) {
        return Err(format!("--help does not report version {version}"));
    }
    Ok(())
}

/// `option` appears as a whole token: `--slice` is not found in `--slicedata` alone.
fn names_option(text: &str, option: &str) -> bool {
    text.match_indices(option).any(|(at, _)| {
        let before = text[..at].chars().next_back();
        let after = text[at + option.len()..].chars().next();
        !before.is_some_and(is_option_char) && !after.is_some_and(is_option_char)
    })
}

fn is_option_char(c: char) -> bool {
    c.is_ascii_alphanumeric() || c == '-' || c == '_'
}

#[cfg(test)]
mod tests {
    use super::*;

    const HELP: &str = "BambuStudio-02.08.02.61:\nUsage: bambu-studio [ OPTIONS ] [ file.3mf/file.stl ... ]\n\n  --export-3mf  Export project as 3MF.\n  --slice Option  Slice the plates\n  --slicedata  Directory\n";

    #[test]
    fn shutdown_grace_defaults_to_20_s_and_takes_whole_seconds() {
        assert_eq!(shutdown_grace(None).unwrap(), Duration::from_secs(20));
        assert_eq!(shutdown_grace(Some(" ")).unwrap(), Duration::from_secs(20));
        assert_eq!(shutdown_grace(Some("5")).unwrap(), Duration::from_secs(5));
        assert_eq!(shutdown_grace(Some("0")).unwrap(), Duration::ZERO);
        for bad in ["-1", "1.5", "soon"] {
            let err = shutdown_grace(Some(bad)).unwrap_err().to_string();
            assert!(err.contains(GRACE_VAR), "{err}");
        }
    }

    #[test]
    fn accepts_the_pinned_help() {
        assert_eq!(check_help(HELP, "02.08.02.61"), Ok(()));
    }

    #[test]
    fn refuses_another_version() {
        let err = check_help(HELP, "02.07.00.00").unwrap_err();
        assert!(err.contains("02.07.00.00"), "{err}");
    }

    #[test]
    fn refuses_a_missing_option() {
        let help = HELP.replace("--export-3mf", "--export-stl");
        let err = check_help(&help, "02.08.02.61").unwrap_err();
        assert!(err.contains("--export-3mf"), "{err}");
    }

    #[test]
    fn a_longer_option_does_not_name_a_prefix() {
        let help = "BambuStudio-02.08.02.61\n  --export-3mf\n  --slicedata dir\n  --slice-all\n";
        let err = check_help(help, "02.08.02.61").unwrap_err();
        assert!(err.contains("--slice"), "{err}");
    }

    #[test]
    fn a_cli_that_cannot_start_is_reported_without_its_path() {
        let err = run_help(Path::new("/nonexistent/AppRun")).unwrap_err();
        assert!(err.contains("cannot start"), "{err}");
        assert!(!err.contains("/nonexistent"), "{err}");
    }
}
