//! Runs the Bambu Studio CLI once, reading its `--pipe` progress (spec 2026-10-16 §5.2).

use std::ffi::OsString;
use std::os::unix::process::ExitStatusExt;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use nix::sys::signal::{Signal, killpg};
use nix::sys::stat::Mode;
use nix::unistd::{AccessFlags, Pid, access, mkfifo};
use tokio::io::{AsyncRead, AsyncReadExt};
use tokio::net::unix::pipe;
use tokio::process::Command;
use tokio_util::sync::CancellationToken;

use crate::models::{Arrange, SliceProgress};
use crate::progress::{Line, LineBuffer};

/// How much of the CLI's stderr, and of its stdout, is kept: their last lines say why
/// it failed.
const STDERR_TAIL: usize = 8 * 1024;
/// How many of the CLI's own `[error]` log lines a failure reports, the last ones.
const ERROR_LINES: usize = 3;
/// At most one progress callback a second; the last line seen is always delivered.
const PROGRESS_EVERY: Duration = Duration::from_secs(1);
/// The longest the callback goes without a call while the CLI runs: a silent phase
/// repeats the last progress (zeroed before the first line), because Temporal delivers
/// cancellation only on a heartbeat and fails an activity silent past its heartbeat timeout.
pub const KEEPALIVE: Duration = Duration::from_secs(10);
/// How long stderr may stay open after the CLI exits (a straggler holding it).
pub const STDERR_GRACE: Duration = Duration::from_secs(1);
/// The worker's variables the CLI sees, when set, besides `HOME` ([`cli_env`]). AppRun
/// sets `LD_LIBRARY_PATH` and `LC_ALL` itself; it needs nothing else.
const PASSED_VARS: &[&str] = &["PATH", "LANG", "LC_ALL", "TZ", "LD_LIBRARY_PATH", "APPDIR"];

pub struct RunRequest<'a> {
    pub cli: &'a Path,
    pub input_3mf: &'a Path,
    pub machine: &'a Path,
    pub process: &'a Path,
    pub filaments: &'a [PathBuf],
    pub out_3mf: &'a Path,
    pub slicedata_dir: Option<&'a Path>,
    pub plate: u32,
    pub arrange: Arrange,
    pub allow_newer_file: bool,
    pub debug: u8,
    /// Each slot's colour, in slot order; none leaves `--filament-colour` out.
    pub filament_colours: &'a [String],
    /// [`KEEPALIVE`] outside tests.
    pub keepalive: Duration,
}

#[derive(Debug)]
pub struct RunOutcome {
    /// The CLI's exit code, or 128 + the signal that ended it.
    pub exit_code: i32,
    pub warnings: Vec<String>,
    pub stderr_tail: String,
    /// The CLI's `[error]` log lines from its stdout ([`cli_errors`]): where it says why a
    /// plate failed when stderr is empty, as for an empty plate (exit 206, #4638).
    pub errors: String,
    pub elapsed: Duration,
}

#[derive(Debug)]
pub enum RunError {
    /// `cancel` fired; the CLI's process group `pgid` was killed.
    Cancelled {
        pgid: i32,
    },
    Io(std::io::Error),
}

impl std::fmt::Display for RunError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Cancelled { .. } => write!(f, "slice cancelled"),
            Self::Io(e) => write!(f, "io: {e}"),
        }
    }
}

impl std::error::Error for RunError {}

impl From<std::io::Error> for RunError {
    fn from(e: std::io::Error) -> Self {
        Self::Io(e)
    }
}

impl From<nix::Error> for RunError {
    fn from(e: nix::Error) -> Self {
        Self::Io(e.into())
    }
}

/// The argv after the CLI's path, in the order spec §5.2 fixes; the input is last.
pub fn command_line(req: &RunRequest<'_>, fifo: &Path) -> Vec<OsString> {
    let mut argv: Vec<OsString> = vec!["--pipe".into(), fifo.into()];
    argv.extend(["--debug".into(), req.debug.to_string().into()]);
    argv.push("--load-settings".into());
    argv.push(joined([req.machine, req.process]));
    argv.push("--load-filaments".into());
    // `BambuStudio.cpp` splits both lists on ';'.
    argv.push(joined(req.filaments.iter().map(PathBuf::as_path)));
    if !req.filament_colours.is_empty() {
        // Without it, slots that all load a preset of one name lose the prime tower
        // (`disable_wipe_tower_after_mapping`, #3962); with two or more distinct colours
        // the CLI keeps it, while one is passed but changes nothing there. The same
        // `;`-separated list (`models.rs` takes only `#RRGGBB`).
        argv.push("--filament-colour".into());
        argv.push(req.filament_colours.join(";").into());
    }
    if req.allow_newer_file {
        argv.push("--allow-newer-file".into());
    }
    let arrange = match req.arrange {
        Arrange::No => "0",
        Arrange::Yes => "1",
        Arrange::Auto => "auto",
    };
    argv.extend(["--arrange".into(), arrange.into()]);
    argv.extend(["--slice".into(), req.plate.to_string().into()]);
    if let Some(dir) = req.slicedata_dir {
        argv.extend(["--export-slicedata".into(), dir.into()]);
    }
    argv.extend(["--export-3mf".into(), req.out_3mf.into()]);
    argv.push(req.input_3mf.into());
    argv
}

/// The CLI's whole environment: [`PASSED_VARS`] as `var` has them, and `HOME`, or
/// `scratch` when `HOME` is unset or not writable. Nothing else of the worker's (a
/// collector credential in `OTEL_EXPORTER_OTLP_HEADERS`, whatever a Deployment adds)
/// reaches a third-party binary, as `template_process.py` allowlists a template's.
pub fn cli_env(
    var: impl Fn(&str) -> Option<OsString>,
    scratch: &Path,
) -> Vec<(OsString, OsString)> {
    let mut env: Vec<(OsString, OsString)> = PASSED_VARS
        .iter()
        .filter_map(|name| var(name).map(|value| ((*name).into(), value)))
        .collect();
    let home = var("HOME")
        .filter(|home| access(Path::new(home), AccessFlags::W_OK).is_ok())
        .unwrap_or_else(|| scratch.into());
    env.push(("HOME".into(), home));
    env
}

fn joined<'p>(paths: impl IntoIterator<Item = &'p Path>) -> OsString {
    let mut out = OsString::new();
    for (i, p) in paths.into_iter().enumerate() {
        if i > 0 {
            out.push(";");
        }
        out.push(p);
    }
    out
}

/// Runs the CLI in its own process group; calls `on_progress` for each progress line
/// (coalesced to at most one a second, the last one always delivered); kills the group
/// when `cancel` fires or on any error (or when this future is dropped); never blocks on
/// the fifo.
pub async fn run(
    req: RunRequest<'_>,
    mut on_progress: impl FnMut(SliceProgress),
    cancel: CancellationToken,
) -> Result<RunOutcome, RunError> {
    let dir = tempfile::Builder::new().prefix("sb-slice-").tempdir()?;
    let fifo = dir.path().join("progress.fifo");
    mkfifo(&fifo, Mode::S_IRWXU)?;
    // O_RDWR | O_NONBLOCK: the open never waits for a writer, and our own write side means
    // a read never sees EOF, so a CLI that exits before opening the pipe (a validation
    // failure) or between its writes cannot end or hang the reader; the child's exit does.
    let pipe = pipe::OpenOptions::new()
        .read_write(true)
        .open_receiver(&fifo)?;

    let started = Instant::now();
    let mut child = Command::new(req.cli)
        .args(command_line(&req, &fifo))
        .env_clear()
        .envs(cli_env(|name| std::env::var_os(name), dir.path()))
        .process_group(0)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true)
        .spawn()?;
    let pgid = Pid::from_raw(
        child
            .id()
            .and_then(|id| i32::try_from(id).ok())
            .ok_or_else(|| std::io::Error::other("the CLI exited before it was seen"))?,
    );
    let mut group = Group(Some(pgid));

    let stderr = Arc::new(Mutex::new(Vec::new()));
    let mut stderr_task = tokio::spawn(keep_tail(child.stderr.take(), stderr.clone()));
    let stdout = Arc::new(Mutex::new(Vec::new()));
    let mut stdout_task = tokio::spawn(keep_tail(child.stdout.take(), stdout.clone()));

    let mut lines = LineBuffer::default();
    let mut progress = Coalesce::new(req.keepalive);
    let mut warnings = Vec::new();
    let mut buf = vec![0u8; 4096];

    let status = loop {
        let due = progress.due();
        let keepalive = progress.keepalive_due();
        tokio::select! {
            status = child.wait() => break status?,
            () = cancel.cancelled() => {
                group.kill();
                let _ = child.wait().await;
                stderr_task.abort();
                stdout_task.abort();
                return Err(RunError::Cancelled { pgid: pgid.as_raw() });
            }
            ready = pipe.readable() => {
                ready?;
                let read = read_available(&pipe, &mut buf, &mut lines)?;
                take(read, &mut progress, &mut warnings, &mut on_progress);
            }
            () = sleep_until(due) => progress.flush(&mut on_progress),
            () = tokio::time::sleep_until(keepalive.into()) => progress.repeat(&mut on_progress),
        }
    };
    // Whatever the CLI left in its group dies now, once (`kill` clears the guard, so the
    // group is never signalled again). The leader is reaped, but the kernel hands out a pid
    // only when no process group has it as its id: if stragglers exist, the id is still
    // their group's and this reaches only them; if none do, killpg fails with ESRCH. Only
    // a process given the freed pid in the microseconds since `wait` returned that also
    // made itself a group leader in them could be hit, and only by this one signal.
    group.kill();
    let elapsed = started.elapsed();

    // Everything the CLI wrote before it exited is in the pipe's buffer.
    let read = read_available(&pipe, &mut buf, &mut lines)?;
    take(read, &mut progress, &mut warnings, &mut on_progress);
    progress.flush(&mut on_progress);

    // A stream still open past the grace is held by something the kill could not reach
    // (outside the group); its reader is stopped, not left running.
    let _ = tokio::time::timeout(STDERR_GRACE, async {
        let _ = (&mut stderr_task).await;
        let _ = (&mut stdout_task).await;
    })
    .await;
    stderr_task.abort();
    stdout_task.abort();
    let stderr_tail = String::from_utf8_lossy(&stderr.lock().expect("stderr lock")).into_owned();
    let errors = cli_errors(&String::from_utf8_lossy(
        &stdout.lock().expect("stdout lock"),
    ));

    let exit_code = status
        .code()
        .unwrap_or_else(|| 128 + status.signal().unwrap_or(0));
    Ok(RunOutcome {
        exit_code,
        warnings,
        stderr_tail,
        errors,
        elapsed,
    })
}

/// The messages of the last [`ERROR_LINES`] `[error]` lines of the CLI's log, one a line.
///
/// The CLI logs `[<time>] [<thread>] [error]   plate 2: Nothing to be sliced, ...` to
/// stdout, not stderr, so an empty plate's reason was lost (#4638); only the message
/// after the level is kept.
pub fn cli_errors(stdout: &str) -> String {
    const LEVEL: &str = "[error]";
    let messages: Vec<&str> = stdout
        .lines()
        .filter_map(|line| line.split_once(LEVEL).map(|(_, message)| message.trim()))
        .filter(|message| !message.is_empty())
        .collect();
    messages[messages.len().saturating_sub(ERROR_LINES)..].join("\n")
}

/// Kills the process group, at most once: on [`Group::kill`] or on drop.
struct Group(Option<Pid>);

impl Group {
    fn kill(&mut self) {
        if let Some(pgid) = self.0.take() {
            let _ = killpg(pgid, Signal::SIGKILL);
        }
    }
}

impl Drop for Group {
    fn drop(&mut self) {
        self.kill();
    }
}

struct Coalesce {
    /// When a progress line was last delivered.
    last: Option<Instant>,
    pending: Option<SliceProgress>,
    /// The last progress delivered, which a keepalive repeats.
    sent: Option<SliceProgress>,
    /// When the callback was last called, by a line or a keepalive.
    called: Instant,
    keepalive: Duration,
}

impl Coalesce {
    fn new(keepalive: Duration) -> Self {
        Self {
            last: None,
            pending: None,
            sent: None,
            called: Instant::now(),
            keepalive,
        }
    }

    fn deliver(&mut self, p: SliceProgress, on_progress: &mut impl FnMut(SliceProgress)) {
        let now = Instant::now();
        self.last = Some(now);
        self.called = now;
        self.sent = Some(p.clone());
        on_progress(p);
    }

    fn offer(&mut self, p: SliceProgress, on_progress: &mut impl FnMut(SliceProgress)) {
        if self.last.is_none_or(|t| t.elapsed() >= PROGRESS_EVERY) {
            self.pending = None;
            self.deliver(p, on_progress);
        } else {
            self.pending = Some(p);
        }
    }

    fn keepalive_due(&self) -> Instant {
        self.called + self.keepalive
    }

    fn repeat(&mut self, on_progress: &mut impl FnMut(SliceProgress)) {
        self.called = Instant::now();
        on_progress(self.sent.clone().unwrap_or_default());
    }

    fn due(&self) -> Option<Instant> {
        self.pending.as_ref()?;
        self.last.map(|t| t + PROGRESS_EVERY)
    }

    fn flush(&mut self, on_progress: &mut impl FnMut(SliceProgress)) {
        if let Some(p) = self.pending.take() {
            self.deliver(p, on_progress);
        }
    }
}

async fn sleep_until(due: Option<Instant>) {
    match due {
        Some(at) => tokio::time::sleep_until(at.into()).await,
        None => std::future::pending().await,
    }
}

/// Reads whatever the pipe holds now, without waiting.
fn read_available(
    pipe: &pipe::Receiver,
    buf: &mut [u8],
    lines: &mut LineBuffer,
) -> Result<Vec<Line>, RunError> {
    let mut out = Vec::new();
    loop {
        match pipe.try_read(buf) {
            Ok(0) => break,
            Ok(n) => out.extend(lines.push(&buf[..n])),
            Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => break,
            Err(e) if e.kind() == std::io::ErrorKind::Interrupted => {}
            Err(e) => return Err(e.into()),
        }
    }
    Ok(out)
}

fn take(
    read: Vec<Line>,
    progress: &mut Coalesce,
    warnings: &mut Vec<String>,
    on_progress: &mut impl FnMut(SliceProgress),
) {
    for line in read {
        match line {
            Line::Progress(p) => progress.offer(p, on_progress),
            Line::Warning(w) => warnings.push(w),
            Line::Other => {}
        }
    }
}

/// Reads `stream` to its end, keeping its last `STDERR_TAIL` bytes in `tail`.
async fn keep_tail(stream: Option<impl AsyncRead + Unpin>, tail: Arc<Mutex<Vec<u8>>>) {
    let Some(mut stream) = stream else { return };
    let mut buf = vec![0u8; 4096];
    while let Ok(n) = stream.read(&mut buf).await {
        if n == 0 {
            break;
        }
        let mut tail = tail.lock().expect("tail lock");
        tail.extend_from_slice(&buf[..n]);
        let excess = tail.len().saturating_sub(STDERR_TAIL);
        tail.drain(..excess);
    }
}

#[cfg(test)]
mod tests {
    use std::collections::HashMap;
    use std::ffi::OsStr;
    use std::os::unix::fs::PermissionsExt as _;

    use super::*;

    fn env_of(vars: &[(&str, &OsString)], scratch: &Path) -> HashMap<OsString, OsString> {
        let vars: HashMap<&str, OsString> = vars.iter().map(|(k, v)| (*k, (*v).clone())).collect();
        cli_env(|name| vars.get(name).cloned(), scratch)
            .into_iter()
            .collect()
    }

    #[test]
    fn the_clis_error_lines_are_its_messages_the_last_few() {
        let stdout = "[2026-10-24 20:00:29.080527] [0x7751] [trace]   Initializing StaticPrintConfigs\n\
            [2026-10-24 20:00:29.080527] [0x7751] [error]   plate 2: Nothing to be sliced, \
            Either the print is empty or no object is fully inside the print volume before apply.\n\
            \n\
            run found error, return -50, exit...\n";
        assert_eq!(
            cli_errors(stdout),
            "plate 2: Nothing to be sliced, Either the print is empty or no object is fully \
             inside the print volume before apply."
        );
        let many = (1..=5)
            .map(|n| format!("[t] [x] [error]   e{n}\n"))
            .collect::<String>();
        assert_eq!(cli_errors(&many), "e3\ne4\ne5");
        assert_eq!(cli_errors("[t] [x] [info]    fine\n"), "");
    }

    #[test]
    fn only_the_allowlist_passes() {
        let scratch = tempfile::tempdir().unwrap();
        let home = tempfile::tempdir().unwrap();
        let home_path = home.path().as_os_str().to_owned();
        let path = OsString::from("/usr/bin:/bin");
        let secret = OsString::from("authorization=Bearer s3cret");
        let env = env_of(
            &[
                ("PATH", &path),
                ("HOME", &home_path),
                ("OTEL_EXPORTER_OTLP_HEADERS", &secret),
            ],
            scratch.path(),
        );
        let expected: HashMap<OsString, OsString> =
            [("PATH".into(), path), ("HOME".into(), home_path)].into();
        assert_eq!(env, expected);
    }

    #[test]
    fn home_falls_back_to_scratch_when_unset_or_unwritable() {
        let scratch = tempfile::tempdir().unwrap();
        let fallback = Some(scratch.path().as_os_str());
        let env = env_of(&[], scratch.path());
        assert_eq!(
            env.get(OsStr::new("HOME")).map(OsString::as_os_str),
            fallback
        );

        let missing = OsString::from("/nonexistent/home");
        let env = env_of(&[("HOME", &missing)], scratch.path());
        assert_eq!(
            env.get(OsStr::new("HOME")).map(OsString::as_os_str),
            fallback
        );

        // Root may write anywhere, so a read-only directory means nothing to it.
        let ro = tempfile::tempdir().unwrap();
        if std::os::unix::fs::MetadataExt::uid(&ro.path().metadata().unwrap()) != 0 {
            std::fs::set_permissions(ro.path(), std::fs::Permissions::from_mode(0o555)).unwrap();
            let ro_path = ro.path().as_os_str().to_owned();
            let env = env_of(&[("HOME", &ro_path)], scratch.path());
            assert_eq!(
                env.get(OsStr::new("HOME")).map(OsString::as_os_str),
                fallback
            );
        }
    }
}
