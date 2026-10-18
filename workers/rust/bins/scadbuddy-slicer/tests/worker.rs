//! The worker against a real Temporal dev server: a test workflow on a queue of its own
//! runs `slice_plate` on the slicer's queue, as the Python caller will. Skips unless
//! `SCADBUDDY_TEST_TEMPORAL_DEV_SERVER` names a Temporal CLI or `temporal` is on `PATH`.

#[path = "../../../crates/sb-slicer/tests/support/mod.rs"]
mod support;

use std::path::PathBuf;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};

use sb_slicer::activity::{SLICE_PLATE, SLICER_ERROR, VALIDATION_EXIT};
use sb_slicer::models::{SlicePlateInput, SlicePlateOutput};
use scadbuddy_slicer::WorkerSettings;
use serde::{Deserialize, Serialize};
use temporalio_client::{Client, WorkflowGetResultOptions, WorkflowStartOptions};
use temporalio_common::data_converters::RawValue;
use temporalio_common::{RetryPolicy, UntypedActivity};
use temporalio_sdk::testing::{EphemeralExe, LocalWorkflowEnvironmentOptions, WorkflowEnvironment};
use temporalio_sdk::workflows::{workflow, workflow_methods};
use temporalio_sdk::{
    ActivityExecutionError, ActivityOptions, Runtime, Worker, WorkerOptions, WorkflowContext,
    WorkflowResult,
};

/// The test workflow's queue: the worker under test registers no workflows.
const CALLER_QUEUE: &str = "slicer-test-caller";
/// One slicer worker per fake CLI mode, each on a queue of its own: a worker stays
/// registered on its queue for the runtime's life, so a second one there is refused.
const OK_QUEUE: &str = "slicer-test";
const INVALID_QUEUE: &str = "slicer-test-192";
const DRAIN_QUEUE: &str = "slicer-test-drain";
const DRAIN_CALLER_QUEUE: &str = "slicer-test-drain-caller";

/// The workflow's input: which slicer queue to call.
#[derive(Debug, Serialize, Deserialize)]
struct Call {
    queue: String,
    input: SlicePlateInput,
}

/// What the workflow saw: the output, or how the activity failed.
#[derive(Debug, Serialize, Deserialize)]
struct Seen {
    output: Option<SlicePlateOutput>,
    failure: Option<Failure>,
}

#[derive(Debug, Serialize, Deserialize)]
struct Failure {
    /// `failed` or `cancelled` (the `ActivityExecutionError`), then the cause's kind.
    kind: String,
    type_name: Option<String>,
    non_retryable: bool,
    details: Vec<serde_json::Value>,
}

#[workflow]
#[derive(Default)]
struct SliceCaller;

#[workflow_methods]
impl SliceCaller {
    #[run]
    async fn run(ctx: &mut WorkflowContext<Self>, call: Call) -> WorkflowResult<Seen> {
        let options = ActivityOptions::with_start_to_close_timeout(Duration::from_secs(60))
            .task_queue(call.queue)
            // Well above `sb_slicer::runner::KEEPALIVE` (10 s).
            .heartbeat_timeout(Duration::from_secs(30))
            .retry_policy(RetryPolicy::builder().maximum_attempts(1).build())
            .build();
        // By the activity's type name, as the Python caller starts it: the typed
        // reference would hide a registered name that differs.
        let input = RawValue::from_value(&call.input, ctx.payload_converter());
        let result = ctx
            .execute_activity(UntypedActivity::new(SLICE_PLATE), input, options)
            .await;
        Ok(match result {
            Ok(output) => Seen {
                output: Some(output.to_value(ctx.payload_converter())),
                failure: None,
            },
            Err(err) => Seen {
                output: None,
                failure: Some(failure(&err)),
            },
        })
    }
}

/// How the activity failed: the error's variant, its cause's kind and, for an
/// application failure, its type, retryability and details decoded as JSON. Never
/// panics: a panic here would fail the workflow task, not the test.
fn failure(err: &ActivityExecutionError) -> Failure {
    use temporalio_common::error::IncomingError;
    let variant = match err {
        ActivityExecutionError::Failed(_) => "failed",
        ActivityExecutionError::Cancelled(_) => "cancelled",
        ActivityExecutionError::Serialization(_) => "serialization",
        _ => "other",
    };
    let cause = match err.cause() {
        Some(IncomingError::Application(_)) => "application",
        Some(IncomingError::Timeout(_)) => "timeout",
        Some(IncomingError::Cancelled(_)) => "cancelled",
        Some(_) => "other",
        None => "none",
    };
    let mut failure = Failure {
        kind: format!("{variant}/{cause}"),
        type_name: None,
        non_retryable: false,
        details: Vec::new(),
    };
    if let Some(IncomingError::Application(app)) = err.cause() {
        failure.type_name = app.type_name().map(str::to_owned);
        failure.non_retryable = app.is_non_retryable();
        failure.details = app
            .raw_details()
            .unwrap_or_default()
            .iter()
            .map(|p| serde_json::from_slice(&p.data).unwrap_or(serde_json::Value::Null))
            .collect();
    }
    failure
}

/// The Temporal CLI the tests start their dev server from, as the agent's tests find it.
fn temporal_cli() -> Option<PathBuf> {
    if let Some(path) = std::env::var_os("SCADBUDDY_TEST_TEMPORAL_DEV_SERVER")
        .filter(|p| !p.is_empty())
        .map(PathBuf::from)
    {
        return Some(path);
    }
    std::env::split_paths(&std::env::var_os("PATH")?)
        .map(|dir| dir.join("temporal"))
        .find(|p| p.is_file())
}

async fn start_server() -> Option<WorkflowEnvironment<temporalio_sdk::testing::LocalServer>> {
    let Some(cli) = temporal_cli() else {
        eprintln!(
            "skipped: no Temporal CLI (SCADBUDDY_TEST_TEMPORAL_DEV_SERVER or `temporal` on PATH)"
        );
        return None;
    };
    let options = LocalWorkflowEnvironmentOptions::builder()
        .server_executable(EphemeralExe::ExistingPath(
            cli.to_string_lossy().into_owned(),
        ))
        .build();
    Some(
        WorkflowEnvironment::start_local(options)
            .await
            .expect("dev server"),
    )
}

fn settings(queue: &str, shutdown_grace: Duration) -> WorkerSettings {
    WorkerSettings {
        task_queue: queue.to_owned(),
        concurrency: 1,
        shutdown_grace,
    }
}

fn caller_worker(runtime: &Runtime, client: &Client, queue: &str) -> Worker {
    Worker::new(
        runtime,
        client.clone(),
        WorkerOptions::new(queue)
            .register_workflow::<SliceCaller>()
            .unwrap()
            .build(),
    )
    .unwrap()
}

/// Starts `SliceCaller` on `caller_queue`, calling the slicer on `queue`.
async fn start(
    client: &Client,
    caller_queue: &str,
    queue: &str,
    input: SlicePlateInput,
) -> temporalio_client::WorkflowHandle<
    Client,
    impl temporalio_common::HasWorkflowDefinition<Output = Seen>,
> {
    let call = Call {
        queue: queue.to_owned(),
        input,
    };
    let id = format!("slice-caller-{queue}-{}", std::process::id());
    client
        .start_workflow(
            SliceCaller::run,
            call,
            WorkflowStartOptions::new(caller_queue, id).build(),
        )
        .await
        .unwrap()
}

/// Runs `SliceCaller` on `queue` to the end and returns what it saw.
async fn slice(client: &Client, queue: &str, input: SlicePlateInput) -> Seen {
    let handle = start(client, CALLER_QUEUE, queue, input).await;
    tokio::time::timeout(
        Duration::from_secs(60),
        handle.get_result(WorkflowGetResultOptions::default()),
    )
    .await
    .expect("the workflow finishes within 60 s")
    .unwrap()
}

#[tokio::test(flavor = "multi_thread")]
async fn the_worker_serves_slice_plate_and_its_failures() {
    let Some(env) = start_server().await else {
        return;
    };
    let client = env.client().clone();
    let (base, transfers) = support::serve().await;
    let runtime = Runtime::from_current_tokio(Default::default()).unwrap();
    let ready = Arc::new(AtomicBool::new(false));
    let grace = Duration::from_secs(20);
    let ok = scadbuddy_slicer::run_worker(
        &runtime,
        client.clone(),
        settings(OK_QUEUE, grace),
        support::activities(""),
        ready.clone(),
        std::future::pending(),
    );
    let invalid = scadbuddy_slicer::run_worker(
        &runtime,
        client.clone(),
        settings(INVALID_QUEUE, grace),
        support::activities("exit-192"),
        Arc::new(AtomicBool::new(false)),
        std::future::pending(),
    );
    let mut caller = caller_worker(&runtime, &client, CALLER_QUEUE);

    let checks = async {
        let seen = slice(&client, OK_QUEUE, support::input(&base)).await;
        assert!(ready.load(Ordering::SeqCst), "ready while the worker runs");
        let output = seen
            .output
            .unwrap_or_else(|| panic!("a sliced plate, not {:?}", seen.failure));
        assert_eq!(output.exit_code, 0);
        assert_eq!(output.warnings, vec!["thin wall".to_owned()]);
        assert_eq!(
            transfers.lock().unwrap().puts["sliced"].as_slice(),
            b"PK fake 3mf"
        );

        // A non-zero exit reaches the workflow as an ActivityError whose cause is the
        // application failure `error::slicer` built, details intact.
        let seen = slice(&client, INVALID_QUEUE, support::input(&base)).await;
        assert!(seen.output.is_none());
        let failure = seen.failure.expect("an activity failure");
        assert_eq!(failure.kind, "failed/application");
        assert_eq!(failure.type_name.as_deref(), Some(SLICER_ERROR));
        assert!(failure.non_retryable);
        assert_eq!(failure.details.len(), 1, "{:?}", failure.details);
        assert_eq!(failure.details[0]["exit_code"], VALIDATION_EXIT);
        let tail = failure.details[0]["stderr_tail"]
            .as_str()
            .expect("a stderr tail");
        assert!(tail.contains("fake stderr line"), "{tail}");
    };
    tokio::select! {
        () = checks => {}
        r = ok => panic!("the slicer worker stopped: {r:?}"),
        r = invalid => panic!("the second slicer worker stopped: {r:?}"),
        r = caller.run() => panic!("the caller worker stopped: {r:?}"),
    }

    env.shutdown().await.unwrap();
}

/// The newest running fake CLI's process group: the runner makes the CLI a group leader,
/// and the `hang` wrapper execs the fake, whose command line names it.
async fn fake_cli_group() -> i32 {
    let deadline = Instant::now() + Duration::from_secs(20);
    loop {
        let out = std::process::Command::new("pgrep")
            .args(["-n", "-f", "fake/../fake-bambu-studio.sh"])
            .output()
            .unwrap();
        let pid = String::from_utf8_lossy(&out.stdout).trim().to_owned();
        if !pid.is_empty() {
            let out = std::process::Command::new("ps")
                .args(["-o", "pgid=", "-p", &pid])
                .output()
                .unwrap();
            if let Ok(pgid) = String::from_utf8_lossy(&out.stdout).trim().parse() {
                return pgid;
            }
        }
        assert!(Instant::now() < deadline, "the fake CLI never started");
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
}

/// Shutdown with a slice still running: past the grace the SDK cancels the activity, the
/// runner kills the CLI's process group, and the failure reaches the workflow at once,
/// not after the 30 s heartbeat timeout.
#[tokio::test(flavor = "multi_thread")]
async fn shutdown_cancels_a_running_slice_after_the_grace() {
    let Some(env) = start_server().await else {
        return;
    };
    let client = env.client().clone();
    let (base, transfers) = support::serve().await;
    let runtime = Runtime::from_current_tokio(Default::default()).unwrap();
    let (stop_tx, stop_rx) = tokio::sync::oneshot::channel::<()>();
    let mut slicer = std::pin::pin!(scadbuddy_slicer::run_worker(
        &runtime,
        client.clone(),
        settings(DRAIN_QUEUE, Duration::from_secs(1)),
        support::activities("hang"),
        Arc::new(AtomicBool::new(false)),
        async move {
            let _ = stop_rx.await;
        },
    ));
    let mut caller = caller_worker(&runtime, &client, DRAIN_CALLER_QUEUE);
    let mut caller_run = std::pin::pin!(caller.run());

    let (handle, pgid) = tokio::select! {
        started = async {
            let handle = start(&client, DRAIN_CALLER_QUEUE, DRAIN_QUEUE, support::input(&base)).await;
            (handle, fake_cli_group().await)
        } => started,
        r = &mut slicer => panic!("the slicer worker stopped early: {r:?}"),
        r = &mut caller_run => panic!("the caller worker stopped: {r:?}"),
    };

    stop_tx.send(()).unwrap();
    let stopping = Instant::now();
    let stopped = tokio::select! {
        r = tokio::time::timeout(Duration::from_secs(10), &mut slicer) => {
            r.expect("run_worker returns within 10 s of stop")
        }
        r = &mut caller_run => panic!("the caller worker stopped: {r:?}"),
    };
    stopped.expect("run_worker returns Ok after a drain");
    assert!(
        stopping.elapsed() >= Duration::from_millis(900),
        "the slice had its grace: {:?}",
        stopping.elapsed()
    );
    support::assert_group_gone(pgid).await;

    let seen = tokio::select! {
        r = tokio::time::timeout(
            Duration::from_secs(15),
            handle.get_result(WorkflowGetResultOptions::default()),
        ) => r.expect("the failure reaches the workflow before the heartbeat timeout").unwrap(),
        r = &mut caller_run => panic!("the caller worker stopped: {r:?}"),
    };
    assert!(seen.output.is_none());
    let failure = seen.failure.expect("an activity failure");
    // The SDK fails a slice cancelled by its shutdown as a retryable `WorkerShutdown`, so
    // a real policy retries it on another pod at once.
    assert_eq!(failure.kind, "failed/application", "{failure:?}");
    assert_eq!(failure.type_name.as_deref(), Some("WorkerShutdown"));
    assert!(!failure.non_retryable);
    assert!(transfers.lock().unwrap().puts.is_empty());

    env.shutdown().await.unwrap();
}
