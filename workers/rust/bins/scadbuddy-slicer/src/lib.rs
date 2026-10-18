//! The `slicer` queue's worker loop (spec 2026-10-16 §5.2, §7): `slice_plate` only, no
//! workflows, unversioned (versioning comes with the first Rust workflow, §7.2).

use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};

use anyhow::Context as _;
use sb_slicer::activity::SlicerActivities;
use temporalio_client::Client;
use temporalio_sdk::runtime::worker_tuner::{FixedSizeSlotSupplier, TunerHolder};
use temporalio_sdk::{Runtime, Worker, WorkerOptions};
use tokio::signal::unix::{SignalKind, signal};

/// Serves `activities` on `queue`, at most `concurrency` slices at once, until SIGTERM.
/// `ready` is true while the worker runs. SIGTERM stops polling and lets the slices
/// already running finish before this returns.
pub async fn run_worker(
    runtime: &Runtime,
    client: Client,
    queue: &str,
    concurrency: usize,
    activities: SlicerActivities,
    ready: Arc<AtomicBool>,
) -> anyhow::Result<()> {
    let options = WorkerOptions::new(queue)
        .register_activities(activities)
        .tuner(tuner(concurrency))
        .build();
    let mut worker =
        Worker::new(runtime, client, options).context("could not create the worker")?;
    let shutdown = worker.shutdown_handle();
    let mut sigterm = signal(SignalKind::terminate()).context("could not watch for SIGTERM")?;
    let stop = async move {
        sigterm.recv().await;
        tracing::info!("SIGTERM: finishing the running slices");
        shutdown();
        std::future::pending::<()>().await;
    };
    ready.store(true, Ordering::SeqCst);
    let result = tokio::select! {
        r = worker.run() => r,
        () = stop => unreachable!("stop never completes"),
    };
    ready.store(false, Ordering::SeqCst);
    result.context("the worker stopped")
}

/// `concurrency` activity slots; the worker polls no workflow, local activity or Nexus
/// tasks, so one slot each is enough.
fn tuner(concurrency: usize) -> TunerHolder {
    TunerHolder::builder()
        .workflow_task_slot_supplier(FixedSizeSlotSupplier::new(1))
        .activity_task_slot_supplier(FixedSizeSlotSupplier::new(concurrency))
        .local_activity_task_slot_supplier(FixedSizeSlotSupplier::new(1))
        .nexus_task_slot_supplier(FixedSizeSlotSupplier::new(1))
        .build()
}
