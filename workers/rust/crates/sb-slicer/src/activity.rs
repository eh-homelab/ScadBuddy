//! The `slice_plate` activity (spec 2026-10-16 §5.2): download the inputs, run the CLI,
//! upload the output, heartbeating throughout: the CLI's progress as it comes, and the
//! last progress again at least every `keepalive`, transfers included.

use std::future::Future;
use std::io::Write as _;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

use sb_temporal::Stages;
use sb_transfer::{Transfer, TransferError, Transferred};
use temporalio_sdk::activities::{ActivityContext, ActivityError, activities};
use tokio_util::sync::CancellationToken;
use tracing::Instrument as _;
use tracing::field::Empty;

use crate::error;
use crate::models::{SlicePlateInput, SlicePlateOutput, SliceProgress, TransferUrl};
use crate::runner::{self, RunError, RunRequest};

/// The activity's type name, what callers start it by (spec §5.2).
pub const SLICE_PLATE: &str = "slice_plate";
/// The `ApplicationError` type of a slice the CLI failed; its details are always
/// `{exit_code, stderr_tail, errors}`.
pub const SLICER_ERROR: &str = "SlicerError";
/// The `ApplicationError` type of the worker's own failure (a temp dir, starting the
/// CLI): retryable, a message and no details.
pub const SLICER_WORKER_ERROR: &str = "SlicerWorkerError";
/// The CLI's exit code for a validation failure (spec §2).
pub const VALIDATION_EXIT: i32 = 192;

pub struct SlicerActivities {
    pub cli: PathBuf,
    pub transfer: Transfer,
    pub stages: Stages,
    /// The longest the activity goes without a heartbeat ([`runner::KEEPALIVE`] outside
    /// tests): Temporal fails an activity silent past its heartbeat timeout and delivers
    /// cancellation only on a heartbeat, so a slow transfer must beat too.
    pub keepalive: Duration,
}

#[activities]
impl SlicerActivities {
    // The macro would name it `SlicerActivities::slice_plate`.
    #[activity(name = "slice_plate")]
    pub async fn slice_plate(
        self: Arc<Self>,
        ctx: ActivityContext,
        input: SlicePlateInput,
    ) -> Result<SlicePlateOutput, ActivityError> {
        let span = tracing::info_span!(
            "slice.plate",
            plate = input.plate,
            exit_code = Empty,
            cancelled_pgid = Empty
        );
        let cancel = CancellationToken::new();
        let watcher = tokio::spawn({
            let ctx = ctx.clone();
            let cancel = cancel.clone();
            async move {
                ctx.cancelled().await;
                cancel.cancel();
            }
        });
        let (progress, rx) = tokio::sync::mpsc::unbounded_channel();
        let beats = tokio::spawn(heartbeat(ctx.clone(), rx, self.keepalive));
        let result = slice(&self, input, &progress, &cancel, &span)
            .instrument(span.clone())
            .await;
        watcher.abort();
        beats.abort();
        result
    }
}

/// Heartbeats at once, then each progress `rx` delivers as it comes, and the last one
/// again (zeroed before the first) whenever `keepalive` passes without one, until every
/// sender is dropped or the task is aborted.
async fn heartbeat(
    ctx: ActivityContext,
    mut rx: tokio::sync::mpsc::UnboundedReceiver<SliceProgress>,
    keepalive: Duration,
) {
    let mut last = SliceProgress::default();
    loop {
        // Only a payload conversion can fail here, and a missed beat is harmless.
        let _ = ctx.record_heartbeat(last.clone()).await;
        match tokio::time::timeout(keepalive, rx.recv()).await {
            Ok(Some(progress)) => last = progress,
            Ok(None) => return,
            Err(_) => {}
        }
    }
}

async fn slice(
    acts: &SlicerActivities,
    input: SlicePlateInput,
    progress: &tokio::sync::mpsc::UnboundedSender<SliceProgress>,
    cancel: &CancellationToken,
    span: &tracing::Span,
) -> Result<SlicePlateOutput, ActivityError> {
    // One per attempt, removed with everything in it when this returns.
    let dir = tempfile::Builder::new()
        .prefix("sb-slice-plate-")
        .tempdir()
        .map_err(error::local)?;
    let at = |name: &str| dir.path().join(name);
    let input_3mf = at("input.3mf");
    let machine = at("machine.json");
    let process = at("process.json");
    let filaments: Vec<PathBuf> = (1..=input.filament_presets.len())
        .map(|i| at(&format!("filament_{i}.json")))
        .collect();

    let get = |url: &TransferUrl, dest: &Path| {
        let (url, dest) = (url.clone(), dest.to_owned());
        async move { acts.transfer.get(&url, &dest).await }
    };
    unless_cancelled(cancel, get(&input.input_3mf, &input_3mf)).await?;
    unless_cancelled(cancel, get(&input.machine_preset, &machine)).await?;
    unless_cancelled(cancel, get(&input.process_preset, &process)).await?;
    for (url, dest) in input.filament_presets.iter().zip(&filaments) {
        unless_cancelled(cancel, get(url, dest)).await?;
    }

    let slicedata_dir = match input.slicedata {
        Some(_) => {
            let d = at("slicedata");
            tokio::fs::create_dir(&d).await.map_err(error::local)?;
            Some(d)
        }
        None => None,
    };
    let out_3mf = at("sliced.3mf");
    let req = RunRequest {
        cli: &acts.cli,
        input_3mf: &input_3mf,
        machine: &machine,
        process: &process,
        filaments: &filaments,
        out_3mf: &out_3mf,
        slicedata_dir: slicedata_dir.as_deref(),
        plate: input.plate,
        arrange: input.arrange,
        allow_newer_file: input.allow_newer_file,
        debug: input.debug,
        filament_colours: &input.filament_colours,
        keepalive: acts.keepalive,
    };

    // Progress goes through the heartbeat task's channel so the callback stays
    // synchronous and heartbeats keep their order.
    let tx = progress.clone();
    let outcome = runner::run(
        req,
        move |progress| {
            let _ = tx.send(progress);
        },
        cancel.clone(),
    )
    .await;
    let outcome = match outcome {
        Ok(o) => o,
        Err(RunError::Cancelled { pgid }) => {
            // The killed group, so a process that outlived the kill can be traced to it.
            span.record("cancelled_pgid", pgid);
            return Err(ActivityError::cancelled());
        }
        Err(e @ RunError::Io(_)) => return Err(error::local(e)),
    };
    // Every finished run, failed or not, as Python's `stage_duration` records them.
    acts.stages.observe("slice", outcome.elapsed.as_secs_f64());
    span.record("exit_code", outcome.exit_code);
    if outcome.exit_code != 0 {
        return Err(error::slicer(
            outcome.exit_code,
            &outcome.stderr_tail,
            &outcome.errors,
        ));
    }
    // Bambu Studio can exit 0 without exporting a plate; that is the slicer's failure,
    // with its stderr, not a transfer's, and the same input fails the same way again.
    if !tokio::fs::try_exists(&out_3mf)
        .await
        .map_err(error::local)?
    {
        return Err(error::no_output(&outcome.stderr_tail, &outcome.errors));
    }

    let sliced = unless_cancelled(cancel, acts.transfer.put(&input.sliced_3mf, &out_3mf)).await?;
    let slicedata = match (&input.slicedata, &slicedata_dir) {
        (Some(url), Some(dir)) => {
            let zip = at("slicedata.zip");
            let (src, dest) = (dir.clone(), zip.clone());
            let zipping = tokio::task::spawn_blocking(move || zip_dir(&src, &dest));
            tokio::select! {
                () = cancel.cancelled() => return Err(ActivityError::cancelled()),
                r = zipping => r.map_err(error::local)?.map_err(error::local)?,
            }
            Some(unless_cancelled(cancel, acts.transfer.put(url, &zip)).await?)
        }
        _ => None,
    };

    Ok(SlicePlateOutput {
        sliced_sha256: sliced.sha256,
        sliced_size: sliced.size,
        slicedata_sha256: slicedata.as_ref().map(|t| t.sha256.clone()),
        slicedata_size: slicedata.as_ref().map(|t| t.size),
        warnings: outcome.warnings,
        exit_code: outcome.exit_code,
        stderr_tail: outcome.stderr_tail,
        elapsed_seconds: outcome.elapsed.as_secs_f64(),
    })
}

/// A transfer, abandoned (its `.part` file removed) when the activity is cancelled.
async fn unless_cancelled(
    cancel: &CancellationToken,
    transfer: impl Future<Output = Result<Transferred, TransferError>>,
) -> Result<Transferred, ActivityError> {
    tokio::select! {
        () = cancel.cancelled() => Err(ActivityError::cancelled()),
        r = transfer => r.map_err(error::transfer),
    }
}

/// `src`'s files, recursively, as a deflated zip at `dest`, named relative to `src`.
fn zip_dir(src: &Path, dest: &Path) -> std::io::Result<()> {
    use zip::write::SimpleFileOptions;
    let mut zip = zip::ZipWriter::new(std::fs::File::create(dest)?);
    let mut stack = vec![src.to_owned()];
    while let Some(dir) = stack.pop() {
        let mut entries: Vec<_> = std::fs::read_dir(&dir)?.collect::<Result<_, _>>()?;
        entries.sort_by_key(std::fs::DirEntry::file_name);
        for entry in entries {
            let path = entry.path();
            let name = path
                .strip_prefix(src)
                .map_err(std::io::Error::other)?
                .to_string_lossy()
                .into_owned();
            let kind = entry.file_type()?;
            if kind.is_dir() {
                zip.add_directory(name, SimpleFileOptions::default())
                    .map_err(std::io::Error::other)?;
                stack.push(path);
            } else if kind.is_file() {
                let options = SimpleFileOptions::default()
                    .compression_method(zip::CompressionMethod::Deflated)
                    .large_file(entry.metadata()?.len() >= u64::from(u32::MAX));
                zip.start_file(name, options)
                    .map_err(std::io::Error::other)?;
                std::io::copy(&mut std::fs::File::open(&path)?, &mut zip)?;
            }
        }
    }
    zip.finish()?.flush()
}
