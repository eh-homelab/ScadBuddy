//! The bootstrap every ScadBuddy Rust worker shares (spec 2026-10-16 §7): its
//! environment, the Temporal connection, `/healthz` and `/metrics` on 9090, and tracing.

mod health;
mod metrics;
mod telemetry;

use anyhow::Context as _;
use temporalio_client::{Client, ClientOptions, ConnectionOptions, Url};

pub use health::serve_health;
pub use metrics::{RENDER_BUCKETS, Stages};
pub use telemetry::init_tracing;

/// The port `/healthz` and `/metrics` are served on, as the Python render worker's.
pub const METRICS_PORT: u16 = 9090;

/// How long the first connection may take before `connect` gives up, so a wrong address
/// fails the pod instead of hanging it.
const CONNECT_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(10);

/// A worker's whole configuration: it reads no settings and no database.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WorkerEnv {
    pub address: String,
    pub namespace: String,
    pub task_queue: String,
    pub concurrency: usize,
    pub metrics_port: u16,
}

impl WorkerEnv {
    /// `SCADBUDDY_TEMPORAL_ADDRESS` (required), `SCADBUDDY_TEMPORAL_NAMESPACE` (default
    /// `default`), the queue from `queue_var` (default `queue_default`) and the concurrency
    /// from `concurrency_var` (default one per core pair, at least one; spec §5.2).
    pub fn from_env(
        queue_var: &str,
        queue_default: &str,
        concurrency_var: &str,
    ) -> anyhow::Result<Self> {
        let address =
            var("SCADBUDDY_TEMPORAL_ADDRESS").context("SCADBUDDY_TEMPORAL_ADDRESS is required")?;
        let namespace = var("SCADBUDDY_TEMPORAL_NAMESPACE").unwrap_or_else(|| "default".into());
        let task_queue = var(queue_var).unwrap_or_else(|| queue_default.into());
        let concurrency = match var(concurrency_var) {
            None => default_concurrency(),
            Some(raw) => match raw.parse::<usize>() {
                Ok(n) if n >= 1 => n,
                _ => anyhow::bail!("{concurrency_var} must be a whole number of at least 1"),
            },
        };
        Ok(Self {
            address,
            namespace,
            task_queue,
            concurrency,
            metrics_port: METRICS_PORT,
        })
    }
}

/// A variable's trimmed value, `None` when unset or blank.
fn var(name: &str) -> Option<String> {
    std::env::var(name)
        .ok()
        .map(|v| v.trim().to_owned())
        .filter(|v| !v.is_empty())
}

/// `floor(cpus / 2)`, at least one: Bambu Studio parallelises inside a slice.
fn default_concurrency() -> usize {
    let cpus = std::thread::available_parallelism().map_or(1, |n| n.get());
    (cpus / 2).max(1)
}

/// Connects over plain gRPC to `address` (`host:port`, as the Python workers take it
/// in-cluster) and makes a client for `namespace`. With trace export on (see
/// [`init_tracing`]) it installs the SDK's OpenTelemetry plugin, which uses the global
/// tracer provider `init_tracing` set, so an activity's span is a child of the workflow's.
///
/// `runtime` is taken so a client is only ever made once the worker's runtime exists;
/// temporalio-sdk 1.1.0 exposes no metric meter on `Runtime` (`Runtime::core` is
/// `pub(crate)`), so the client records no SDK metrics.
pub async fn connect(
    env: &WorkerEnv,
    _runtime: &temporalio_sdk::Runtime,
) -> anyhow::Result<Client> {
    let target = if env.address.contains("://") {
        env.address.clone()
    } else {
        format!("http://{}", env.address)
    };
    let target = Url::parse(&target).context("SCADBUDDY_TEMPORAL_ADDRESS is not host:port")?;
    let connection = ConnectionOptions::new(target)
        .identity(identity())
        .connect_timeout(CONNECT_TIMEOUT)
        .build();
    let mut options = ClientOptions::new(env.namespace.clone());
    if telemetry::exporting() {
        options =
            options.plugin(temporalio_sdk::opentelemetry::OpenTelemetryPlugin::builder().build());
    }
    let client = Client::connect(connection, options.build())
        .await
        .context("could not connect to Temporal")?;
    Ok(client)
}

/// `<pid>@<hostname>`, as the Python SDK names a worker.
fn identity() -> String {
    let host = std::env::var("HOSTNAME").unwrap_or_else(|_| "unknown".into());
    format!("{}@{host}", std::process::id())
}
