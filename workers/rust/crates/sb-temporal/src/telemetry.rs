//! Logs and traces. Logs are JSON lines on stdout (level from `RUST_LOG`, default `info`).
//! Spans are exported over OTLP only when [`exporting`] says so, through [`Scrub`], which
//! removes what `backend/scadbuddy/core/trace_scrub.py` removes from the Python spans:
//! exception messages, status descriptions and attributes that carry a URL or a peer.
//! Code still never puts a parameter value, a typed file name, a transfer URL or CLI
//! stderr in a span attribute, a log field or a metric label; the scrub is the backstop
//! for what the SDK and the tracing bridge record on their own.

use std::borrow::Cow;
use std::time::Duration;

use opentelemetry::KeyValue;
use opentelemetry::trace::{Status, TracerProvider as _};
use opentelemetry_sdk::Resource;
use opentelemetry_sdk::error::OTelSdkResult;
use opentelemetry_sdk::trace::{SdkTracerProvider, SpanData, SpanExporter};
use temporalio_sdk::opentelemetry::WorkflowIdGenerator;
use tracing_subscriber::layer::SubscriberExt as _;
use tracing_subscriber::util::SubscriberInitExt as _;
use tracing_subscriber::{EnvFilter, Layer as _};

const DEFAULT_SERVICE_NAME: &str = "scadbuddy-slicer";

/// Attributes never exported, on spans and on their events: an exception's text, a
/// tracing `error` field (an error's `Display`), a URL or a path, and a peer's address
/// (`trace_scrub.py` `DROPPED_ATTRIBUTES`, `URL_ATTRIBUTES`).
const DROPPED: &[&str] = &[
    "exception.message",
    "exception.stacktrace",
    "error",
    "url.full",
    "url.path",
    "url.query",
    "http.url",
    "http.target",
    "http.host",
    "http.server_name",
    "http.user_agent",
    "user_agent.original",
    "server.address",
    "client.address",
    "client.port",
    "net.peer.ip",
    "net.peer.port",
    "network.peer.address",
    "network.peer.port",
];

/// Installs the global `tracing` subscriber and, when exporting, the global OpenTelemetry
/// tracer provider (with the SDK's `WorkflowIdGenerator`, which the Temporal plugin
/// `connect` installs needs for replay-safe workflow spans). Returns the provider so
/// `main` can flush it at shutdown; `None` when nothing is exported.
pub fn init_tracing() -> anyhow::Result<Option<SdkTracerProvider>> {
    let filter = EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info"));
    let fmt = tracing_subscriber::fmt::layer()
        .json()
        .with_current_span(false)
        .with_span_list(false)
        .with_filter(filter);
    if !exporting() {
        tracing_subscriber::registry().with(fmt).try_init()?;
        return Ok(None);
    }
    let provider = SdkTracerProvider::builder()
        .with_id_generator(WorkflowIdGenerator::default())
        .with_resource(
            Resource::builder()
                .with_service_name(service_name(&env_var))
                .build(),
        )
        .with_batch_exporter(Scrub(span_exporter()?))
        .build();
    opentelemetry::global::set_tracer_provider(provider.clone());
    let otel = tracing_opentelemetry::layer()
        .with_tracer(provider.tracer("scadbuddy"))
        .with_filter(EnvFilter::new("info"));
    tracing_subscriber::registry()
        .with(fmt)
        .with(otel)
        .try_init()?;
    Ok(Some(provider))
}

/// gRPC unless `OTEL_EXPORTER_OTLP_TRACES_PROTOCOL` / `OTEL_EXPORTER_OTLP_PROTOCOL`
/// names another protocol (the exporter's own default with both transports built in is
/// HTTP, so gRPC is chosen here). The endpoint and headers are the exporter's own
/// `OTEL_EXPORTER_OTLP_*` reading.
fn span_exporter() -> anyhow::Result<opentelemetry_otlp::SpanExporter> {
    let named = [
        "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL",
        "OTEL_EXPORTER_OTLP_PROTOCOL",
    ]
    .iter()
    .any(|k| env_var(k).is_some());
    let builder = opentelemetry_otlp::SpanExporter::builder();
    Ok(if named {
        builder.build()?
    } else {
        builder.with_tonic().build()?
    })
}

fn env_var(name: &str) -> Option<String> {
    std::env::var(name)
        .ok()
        .map(|v| v.trim().to_owned())
        .filter(|v| !v.is_empty())
}

fn service_name(env: &impl Fn(&str) -> Option<String>) -> String {
    env("OTEL_SERVICE_NAME").unwrap_or_else(|| DEFAULT_SERVICE_NAME.to_owned())
}

/// Whether spans are exported, as `core/tracing.py` `traces_export_enabled` decides: an
/// endpoint is set (`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` or `OTEL_EXPORTER_OTLP_ENDPOINT`),
/// `OTEL_SDK_DISABLED` is not `true`, and `OTEL_TRACES_EXPORTER` is unset or names `otlp`.
pub(crate) fn exporting() -> bool {
    exporting_with(&env_var)
}

fn exporting_with(env: &impl Fn(&str) -> Option<String>) -> bool {
    if env("OTEL_SDK_DISABLED").is_some_and(|v| v.eq_ignore_ascii_case("true")) {
        return false;
    }
    if let Some(names) = env("OTEL_TRACES_EXPORTER")
        && !names
            .split(',')
            .any(|n| n.trim().eq_ignore_ascii_case("otlp"))
    {
        return false;
    }
    [
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
    ]
    .iter()
    .any(|k| env(k).is_some())
}

/// The exporter every span leaves through.
#[derive(Debug)]
struct Scrub<E>(E);

impl<E: SpanExporter> SpanExporter for Scrub<E> {
    async fn export(&self, batch: Vec<SpanData>) -> OTelSdkResult {
        self.0.export(batch.into_iter().map(scrub).collect()).await
    }

    fn shutdown_with_timeout(&self, timeout: Duration) -> OTelSdkResult {
        self.0.shutdown_with_timeout(timeout)
    }

    fn force_flush(&self) -> OTelSdkResult {
        self.0.force_flush()
    }

    fn set_resource(&mut self, resource: &Resource) {
        self.0.set_resource(resource);
    }
}

fn scrub(mut span: SpanData) -> SpanData {
    if let Status::Error { .. } = span.status {
        span.status = Status::Error {
            description: Cow::Borrowed(""),
        };
    }
    keep(&mut span.attributes);
    for event in span.events.events.iter_mut() {
        keep(&mut event.attributes);
    }
    span
}

fn keep(attributes: &mut Vec<KeyValue>) {
    attributes.retain(|kv| {
        let key = kv.key.as_str();
        !DROPPED.contains(&key)
            && !key.starts_with("http.request.header.")
            && !key.starts_with("http.response.header.")
    });
}

#[cfg(test)]
mod tests {
    use std::collections::HashMap;
    use std::time::SystemTime;

    use opentelemetry::InstrumentationScope;
    use opentelemetry::trace::{Event, SpanContext, SpanId, SpanKind};
    use opentelemetry_sdk::trace::{SpanEvents, SpanLinks};

    use super::*;

    fn env(pairs: &[(&str, &str)]) -> impl Fn(&str) -> Option<String> {
        let map: HashMap<String, String> = pairs
            .iter()
            .map(|(k, v)| ((*k).to_owned(), (*v).to_owned()))
            .collect();
        move |k| map.get(k).cloned().filter(|v| !v.is_empty())
    }

    #[test]
    fn exports_only_with_an_endpoint_and_not_disabled() {
        let endpoint = ("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317");
        assert!(!exporting_with(&env(&[])));
        assert!(exporting_with(&env(&[endpoint])));
        assert!(exporting_with(&env(&[(
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
            "http://collector:4317"
        )])));
        assert!(!exporting_with(&env(&[
            endpoint,
            ("OTEL_SDK_DISABLED", "TRUE")
        ])));
        assert!(exporting_with(&env(&[
            endpoint,
            ("OTEL_SDK_DISABLED", "false")
        ])));
        assert!(!exporting_with(&env(&[
            endpoint,
            ("OTEL_TRACES_EXPORTER", "none")
        ])));
        assert!(exporting_with(&env(&[
            endpoint,
            ("OTEL_TRACES_EXPORTER", "console, OTLP")
        ])));
    }

    #[test]
    fn service_name_defaults_to_the_slicer() {
        assert_eq!(service_name(&env(&[])), "scadbuddy-slicer");
        assert_eq!(
            service_name(&env(&[("OTEL_SERVICE_NAME", "scadbuddy-cli")])),
            "scadbuddy-cli"
        );
    }

    #[test]
    fn scrub_drops_messages_descriptions_and_urls() {
        let mut events = SpanEvents::default();
        events.events = vec![Event::new(
            "exception",
            SystemTime::now(),
            vec![
                KeyValue::new("exception.type", "SlicerError"),
                KeyValue::new("exception.message", "stderr: /tmp/My Secret Box.3mf"),
                KeyValue::new("error", "got 'secret'"),
            ],
            0,
        )];
        let span = SpanData {
            span_context: SpanContext::empty_context(),
            parent_span_id: SpanId::INVALID,
            parent_span_is_remote: false,
            span_kind: SpanKind::Internal,
            name: "RunActivity:slice_plate".into(),
            start_time: SystemTime::now(),
            end_time: SystemTime::now(),
            attributes: vec![
                KeyValue::new("temporalWorkflowID", "wf-1"),
                KeyValue::new("url.full", "https://s3/bucket/key?X-Amz-Signature=secret"),
                KeyValue::new("http.request.header.x-scadbuddy-transfer", "token"),
            ],
            dropped_attributes_count: 0,
            events,
            links: SpanLinks::default(),
            status: Status::error("slicer exited 1: /tmp/My Secret Box.3mf"),
            instrumentation_scope: InstrumentationScope::builder("scadbuddy").build(),
        };
        let out = scrub(span);
        let keys = |kvs: &[KeyValue]| kvs.iter().map(|kv| kv.key.to_string()).collect::<Vec<_>>();
        assert_eq!(keys(&out.attributes), ["temporalWorkflowID"]);
        assert_eq!(keys(&out.events.events[0].attributes), ["exception.type"]);
        assert_eq!(
            out.status,
            Status::Error {
                description: Cow::Borrowed("")
            }
        );
    }
}
