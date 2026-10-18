//! The worker against a real Temporal dev server: a test workflow on a queue of its own
//! runs `slice_plate` on the slicer's queue, as the Python caller will. Skips unless
//! `SCADBUDDY_TEST_TEMPORAL_DEV_SERVER` names a Temporal CLI or `temporal` is on `PATH`.

#[path = "../../../crates/sb-slicer/tests/support/mod.rs"]
mod support;

use std::path::PathBuf;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use sb_slicer::activity::{SLICER_ERROR, SlicerActivities, VALIDATION_EXIT};
use sb_slicer::models::{SlicePlateInput, SlicePlateOutput};
use serde::{Deserialize, Serialize};
use temporalio_client::{Client, WorkflowGetResultOptions, WorkflowStartOptions};
use temporalio_common::RetryPolicy;
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
        let result = ctx
            .execute_activity(SlicerActivities::slice_plate, call.input, options)
            .await;
        Ok(match result {
            Ok(output) => Seen {
                output: Some(output),
                failure: None,
            },
            Err(err) => Seen {
                output: None,
                failure: Some(failure(&err)),
            },
        })
    }
}

/// An activity failure's application cause, its details decoded as JSON.
fn failure(err: &ActivityExecutionError) -> Failure {
    use temporalio_common::error::IncomingError;
    let ActivityExecutionError::Failed(failed) = err else {
        panic!("expected an activity failure, got {err:?}");
    };
    let Some(IncomingError::Application(app)) = failed.cause() else {
        panic!("expected an application failure as the cause, got {err:?}");
    };
    Failure {
        type_name: app.type_name().map(str::to_owned),
        non_retryable: app.is_non_retryable(),
        details: app
            .raw_details()
            .unwrap_or_default()
            .iter()
            .map(|p| serde_json::from_slice(&p.data).expect("JSON details"))
            .collect(),
    }
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

/// Runs `SliceCaller` on `queue` to the end and returns what it saw.
async fn slice(client: &Client, queue: &str, input: SlicePlateInput) -> Seen {
    let call = Call {
        queue: queue.to_owned(),
        input,
    };
    let id = format!("slice-caller-{queue}-{}", std::process::id());
    let handle = client
        .start_workflow(
            SliceCaller::run,
            call,
            WorkflowStartOptions::new(CALLER_QUEUE, id).build(),
        )
        .await
        .unwrap();
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
    let ok = scadbuddy_slicer::run_worker(
        &runtime,
        client.clone(),
        OK_QUEUE,
        1,
        support::activities(""),
        ready.clone(),
    );
    let invalid = scadbuddy_slicer::run_worker(
        &runtime,
        client.clone(),
        INVALID_QUEUE,
        1,
        support::activities("exit-192"),
        Arc::new(AtomicBool::new(false)),
    );
    let mut caller = Worker::new(
        &runtime,
        client.clone(),
        WorkerOptions::new(CALLER_QUEUE)
            .register_workflow::<SliceCaller>()
            .unwrap()
            .build(),
    )
    .unwrap();

    let checks = async {
        let seen = slice(&client, OK_QUEUE, support::input(&base)).await;
        assert!(ready.load(Ordering::SeqCst), "ready while the worker runs");
        let output = seen.output.expect("a sliced plate");
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
