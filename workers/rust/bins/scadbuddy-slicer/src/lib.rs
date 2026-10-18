//! The `slicer` queue's worker loop (spec 2026-10-16 §5.2, §7): `slice_plate` only, no
//! workflows, unversioned (versioning comes with the first Rust workflow, §7.2).

use std::future::Future;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use anyhow::Context as _;
use sb_slicer::activity::SlicerActivities;
use temporalio_client::Client;
use temporalio_sdk::runtime::worker_tuner::{FixedSizeSlotSupplier, TunerHolder};
use temporalio_sdk::{Runtime, Worker, WorkerOptions};

/// What `main` reads from the environment for [`run_worker`].
#[derive(Debug, Clone)]
pub struct WorkerSettings {
    pub task_queue: String,
    /// Slices at once.
    pub concurrency: usize,
    /// How long running slices may go on after `stop`, before they are cancelled. Below
    /// the Deployment's `terminationGracePeriodSeconds`, so a cancelled slice's process
    /// group is killed and its failure reported before the pod is: the retry then starts
    /// at once instead of after the heartbeat timeout.
    pub shutdown_grace: Duration,
}

/// Serves `activities` on `settings.task_queue` until `stop` resolves (SIGTERM, in
/// `main`), then stops polling, lets running slices go on for `shutdown_grace`, cancels
/// what is left (the runner kills each one's process group) and returns.
pub async fn run_worker(
    runtime: &Runtime,
    client: Client,
    settings: WorkerSettings,
    activities: SlicerActivities,
    ready: Arc<AtomicBool>,
    stop: impl Future<Output = ()>,
) -> anyhow::Result<()> {
    let options = WorkerOptions::new(settings.task_queue.as_str())
        .register_activities(activities)
        .tuner(tuner(settings.concurrency))
        .graceful_shutdown_period(settings.shutdown_grace)
        .build();
    let mut worker =
        Worker::new(runtime, client, options).context("could not create the worker")?;
    let shutdown = worker.shutdown_handle();
    let stop = async move {
        stop.await;
        tracing::info!("stopping: draining the running slices");
        shutdown();
        std::future::pending::<()>().await;
    };
    // Advisory: this worker serves no Service traffic, so nothing routes by it. It is set
    // just before `run()` validates the namespace and stays true while draining.
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
