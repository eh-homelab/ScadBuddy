//! Logs, traces and metrics (#988, #7667). Logs are JSON lines on stdout (level from
//! `RUST_LOG`, default `info`); a line logged inside a span carries its `trace_id` and
//! `span_id`. Each signal is also exported over OTLP when [`enabled_with`] says so for it,
//! from the standard `OTEL_*` variables: spans through [`Scrub`], `tracing` events as log
//! records (the appender bridge, which reads the trace context of the entered span), and
//! metrics, the process's own ([`process`]) among them.
//!
//! [`Scrub`] removes what `backend/scadbuddy/core/trace_scrub.py` removes from the Python
//! spans: exception messages, status descriptions and attributes that carry a URL or a
//! peer. Code still never puts a parameter value, a typed file name, a transfer URL or CLI
//! stderr in a span attribute, a log field or a metric label; the scrub is the backstop
//! for what the SDK and the tracing bridge record on their own. The scrub cannot see a
//! tracing event's formatted message (it becomes the span event's name), so no call site
//! may interpolate a value into a log message; values go in fields, which it can drop. A
//! log record carries the same fields the stdout line does.

mod process;

use std::borrow::Cow;
use std::time::Duration;

use opentelemetry::KeyValue;
use opentelemetry::trace::{Status, TraceContextExt as _, TracerProvider as _};
use opentelemetry_appender_tracing::layer::OpenTelemetryTracingBridge;
use opentelemetry_sdk::Resource;
use opentelemetry_sdk::error::OTelSdkResult;
use opentelemetry_sdk::logs::SdkLoggerProvider;
use opentelemetry_sdk::metrics::{SdkMeterProvider, Temporality};
use opentelemetry_sdk::trace::{SdkTracerProvider, SpanData, SpanExporter};
use temporalio_sdk::opentelemetry::WorkflowIdGenerator;
use tracing::{Event, Subscriber};
use tracing_subscriber::fmt::format::Writer;
use tracing_subscriber::fmt::{FmtContext, FormatEvent, FormatFields};
use tracing_subscriber::layer::SubscriberExt as _;
use tracing_subscriber::registry::LookupSpan;
use tracing_subscriber::util::SubscriberInitExt as _;
use tracing_subscriber::{EnvFilter, Layer};

pub use process::ProcessMetrics;

const DEFAULT_SERVICE_NAME: &str = "scadbuddy-slicer";

/// How long [`Telemetry::shutdown`] gives the three providers, together, to flush.
pub const FLUSH_TIMEOUT: Duration = Duration::from_secs(5);

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
    "http.status_text",
    "user_agent.original",
    "server.address",
    "client.address",
    "client.port",
    "net.peer.ip",
    "net.peer.port",
    "net.sock.peer.addr",
    "net.sock.peer.port",
    "network.peer.address",
    "network.peer.port",
];

/// One of the three OTLP signals and the variables that set it.
#[derive(Debug, Clone, Copy)]
pub struct Signal {
    endpoint: &'static str,
    exporter: &'static str,
    protocol: &'static str,
}

pub const TRACES: Signal = Signal {
    endpoint: "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    exporter: "OTEL_TRACES_EXPORTER",
    protocol: "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL",
};
pub const METRICS: Signal = Signal {
    endpoint: "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
    exporter: "OTEL_METRICS_EXPORTER",
    protocol: "OTEL_EXPORTER_OTLP_METRICS_PROTOCOL",
};
pub const LOGS: Signal = Signal {
    endpoint: "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
    exporter: "OTEL_LOGS_EXPORTER",
    protocol: "OTEL_EXPORTER_OTLP_LOGS_PROTOCOL",
};

/// The providers [`init_telemetry`] made, each `None` when its signal is off.
#[derive(Default)]
pub struct Telemetry {
    traces: Option<SdkTracerProvider>,
    meters: Option<SdkMeterProvider>,
    logs: Option<SdkLoggerProvider>,
    _process: Option<ProcessMetrics>,
}

impl Telemetry {
    /// Flushes and stops every provider, all at once, within `within`. Returns the
    /// providers that failed, by signal name, so `main` can log them.
    pub fn shutdown(&self, within: Duration) -> Vec<&'static str> {
        std::thread::scope(|s| {
            let traces = self
                .traces
                .as_ref()
                .map(|p| s.spawn(move || ("traces", p.shutdown_with_timeout(within))));
            let meters = self
                .meters
                .as_ref()
                .map(|p| s.spawn(move || ("metrics", p.shutdown_with_timeout(within))));
            let logs = self
                .logs
                .as_ref()
                .map(|p| s.spawn(move || ("logs", p.shutdown_with_timeout(within))));
            [traces, meters, logs]
                .into_iter()
                .flatten()
                .filter_map(|h| match h.join() {
                    Ok((_, Ok(()))) => None,
                    Ok((name, Err(_))) => Some(name),
                    Err(_) => Some("telemetry"),
                })
                .collect()
        })
    }
}

/// Installs the global `tracing` subscriber and, for each signal that is on, its global
/// OpenTelemetry provider: the tracer (with the SDK's `WorkflowIdGenerator`, which the
/// Temporal plugin `connect` installs needs for replay-safe workflow spans), the meter
/// (with [`ProcessMetrics`]) and the logger. Returns them so `main` can flush them at
/// shutdown.
pub fn init_telemetry() -> anyhow::Result<Telemetry> {
    let filter = EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info"));
    let fmt = stdout_layer(std::io::stdout).with_filter(filter);
    let resource = || {
        Resource::builder()
            .with_service_name(service_name(&env_var))
            .build()
    };
    let mut telemetry = Telemetry::default();
    if enabled_with(&env_var, METRICS) {
        let exporter = metric_exporter()?;
        let provider = SdkMeterProvider::builder()
            .with_resource(resource())
            .with_periodic_exporter(exporter)
            .build();
        opentelemetry::global::set_meter_provider(provider.clone());
        telemetry._process = Some(ProcessMetrics::register(&provider));
        telemetry.meters = Some(provider);
    }
    if enabled_with(&env_var, LOGS) {
        telemetry.logs = Some(
            SdkLoggerProvider::builder()
                .with_resource(resource())
                .with_batch_exporter(log_exporter()?)
                .build(),
        );
    }
    if enabled_with(&env_var, TRACES) {
        let provider = SdkTracerProvider::builder()
            .with_id_generator(WorkflowIdGenerator::default())
            .with_resource(resource())
            .with_batch_exporter(Scrub(span_exporter()?))
            .build();
        opentelemetry::global::set_tracer_provider(provider.clone());
        telemetry.traces = Some(provider);
    }
    let otel = telemetry.traces.as_ref().map(|p| {
        tracing_opentelemetry::layer()
            .with_tracer(p.tracer("scadbuddy"))
            .with_filter(EnvFilter::new("info"))
    });
    let logs = telemetry
        .logs
        .as_ref()
        .map(|p| OpenTelemetryTracingBridge::new(p).with_filter(EnvFilter::new("info")));
    tracing_subscriber::registry()
        .with(fmt)
        .with(otel)
        .with(logs)
        .try_init()?;
    Ok(telemetry)
}

/// The JSON lines on stdout (`writer`), each with the entered span's `trace_id` and
/// `span_id`.
fn stdout_layer<S, W>(writer: W) -> impl Layer<S>
where
    S: Subscriber + for<'a> LookupSpan<'a>,
    W: for<'w> tracing_subscriber::fmt::MakeWriter<'w> + Send + Sync + 'static,
{
    tracing_subscriber::fmt::layer()
        .with_writer(writer)
        .json()
        .with_current_span(false)
        .with_span_list(false)
        .map_event_format(WithTraceIds)
}

/// An event format that adds the entered span's ids (the context the tracing bridge
/// activates on enter) to the JSON object the inner format writes.
struct WithTraceIds<F>(F);

/// The current OpenTelemetry span's trace and span ids, when it is valid.
fn current_ids() -> Option<(String, String)> {
    opentelemetry::Context::map_current(|cx| {
        let sc = cx.span().span_context().clone();
        sc.is_valid()
            .then(|| (sc.trace_id().to_string(), sc.span_id().to_string()))
    })
}

impl<S, N, F> FormatEvent<S, N> for WithTraceIds<F>
where
    S: Subscriber + for<'a> LookupSpan<'a>,
    N: for<'a> FormatFields<'a> + 'static,
    F: FormatEvent<S, N>,
{
    fn format_event(
        &self,
        ctx: &FmtContext<'_, S, N>,
        mut writer: Writer<'_>,
        event: &Event<'_>,
    ) -> std::fmt::Result {
        let Some((trace_id, span_id)) = current_ids() else {
            return self.0.format_event(ctx, writer, event);
        };
        let mut line = String::new();
        self.0.format_event(ctx, Writer::new(&mut line), event)?;
        match line.trim_end().strip_suffix('}') {
            Some(object) => writeln!(
                writer,
                "{object},\"trace_id\":\"{trace_id}\",\"span_id\":\"{span_id}\"}}"
            ),
            None => writer.write_str(&line),
        }
    }
}

/// How a signal leaves: `http/protobuf` unless its protocol variable (or else
/// `OTEL_EXPORTER_OTLP_PROTOCOL`) says `grpc`, the default every ScadBuddy service has
/// (tracing spec 2026-10-01 §3; the deployment points `OTEL_EXPORTER_OTLP_ENDPOINT` at
/// the collector's 4318 with no protocol variable).
#[derive(Debug, PartialEq, Eq)]
enum Transport {
    Http,
    Grpc,
}

fn transport(env: &impl Fn(&str) -> Option<String>, signal: Signal) -> Transport {
    let named = env(signal.protocol).or_else(|| env("OTEL_EXPORTER_OTLP_PROTOCOL"));
    match named {
        Some(p) if p.eq_ignore_ascii_case("grpc") => Transport::Grpc,
        _ => Transport::Http,
    }
}

/// The endpoint and headers of each exporter are its own `OTEL_EXPORTER_OTLP_*` reading.
fn span_exporter() -> anyhow::Result<opentelemetry_otlp::SpanExporter> {
    let builder = opentelemetry_otlp::SpanExporter::builder();
    Ok(match transport(&env_var, TRACES) {
        Transport::Http => builder.with_http().build()?,
        Transport::Grpc => builder.with_tonic().build()?,
    })
}

/// Cumulative, whatever `OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE` says:
/// Prometheus's OTLP receiver refuses delta.
fn metric_exporter() -> anyhow::Result<opentelemetry_otlp::MetricExporter> {
    let builder = opentelemetry_otlp::MetricExporter::builder();
    Ok(match transport(&env_var, METRICS) {
        Transport::Http => builder
            .with_http()
            .with_temporality(Temporality::Cumulative)
            .build()?,
        Transport::Grpc => builder
            .with_tonic()
            .with_temporality(Temporality::Cumulative)
            .build()?,
    })
}

fn log_exporter() -> anyhow::Result<opentelemetry_otlp::LogExporter> {
    let builder = opentelemetry_otlp::LogExporter::builder();
    Ok(match transport(&env_var, LOGS) {
        Transport::Http => builder.with_http().build()?,
        Transport::Grpc => builder.with_tonic().build()?,
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

/// Whether spans are exported (see [`enabled_with`]).
pub(crate) fn exporting() -> bool {
    enabled_with(&env_var, TRACES)
}

/// Whether a signal is exported, as `core/tracing.py` `traces_export_enabled` decides for
/// spans and the Go services' `telemetry.Endpoint` for every signal: an endpoint is set
/// (the signal's own variable or `OTEL_EXPORTER_OTLP_ENDPOINT`), `OTEL_SDK_DISABLED` is
/// not `true`, and the signal's exporter variable is unset or names `otlp`.
pub fn enabled_with(env: &impl Fn(&str) -> Option<String>, signal: Signal) -> bool {
    if env("OTEL_SDK_DISABLED").is_some_and(|v| v.eq_ignore_ascii_case("true")) {
        return false;
    }
    if let Some(names) = env(signal.exporter)
        && !names
            .split(',')
            .any(|n| n.trim().eq_ignore_ascii_case("otlp"))
    {
        return false;
    }
    [signal.endpoint, "OTEL_EXPORTER_OTLP_ENDPOINT"]
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

/// As `trace_scrub.py` `scrub`: an error's description becomes the exception's type (or
/// `error`), and attributes are dropped from the span, its events and its links.
fn scrub(mut span: SpanData) -> SpanData {
    for event in span.events.events.iter_mut() {
        keep(&mut event.attributes);
    }
    for link in span.links.links.iter_mut() {
        keep(&mut link.attributes);
    }
    keep(&mut span.attributes);
    if let Status::Error { description } = &span.status
        && !description.is_empty()
    {
        let kind = exception_type(&span.events.events).unwrap_or_else(|| "error".to_owned());
        span.status = Status::Error {
            description: Cow::Owned(kind),
        };
    }
    span
}

fn exception_type(events: &[opentelemetry::trace::Event]) -> Option<String> {
    events
        .iter()
        .filter(|e| e.name == "exception")
        .flat_map(|e| e.attributes.iter())
        .find(|kv| kv.key.as_str() == "exception.type")
        .and_then(|kv| match &kv.value {
            opentelemetry::Value::String(s) => Some(s.as_str().to_owned()),
            _ => None,
        })
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
    use opentelemetry::trace::{Event, Link, SpanContext, SpanId, SpanKind};
    use opentelemetry_sdk::trace::{SpanEvents, SpanLinks};

    use super::*;

    fn env(pairs: &[(&str, &str)]) -> impl Fn(&str) -> Option<String> + use<> {
        let map: HashMap<String, String> = pairs
            .iter()
            .map(|(k, v)| ((*k).to_owned(), (*v).to_owned()))
            .collect();
        move |k| map.get(k).cloned().filter(|v| !v.is_empty())
    }

    #[test]
    fn exports_only_with_an_endpoint_and_not_disabled() {
        let endpoint = ("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318");
        for signal in [TRACES, METRICS, LOGS] {
            assert!(!enabled_with(&env(&[]), signal));
            assert!(enabled_with(&env(&[endpoint]), signal));
            assert!(enabled_with(
                &env(&[(signal.endpoint, "http://collector:4318/v1/x")]),
                signal
            ));
            assert!(!enabled_with(
                &env(&[endpoint, ("OTEL_SDK_DISABLED", "TRUE")]),
                signal
            ));
            assert!(enabled_with(
                &env(&[endpoint, ("OTEL_SDK_DISABLED", "false")]),
                signal
            ));
            assert!(!enabled_with(
                &env(&[endpoint, (signal.exporter, "none")]),
                signal
            ));
            assert!(!enabled_with(
                &env(&[endpoint, (signal.exporter, "zipkin")]),
                signal
            ));
            assert!(enabled_with(
                &env(&[endpoint, (signal.exporter, "console, OTLP")]),
                signal
            ));
        }
        // One signal's own variables leave the others alone.
        let traces_only = env(&[(
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
            "http://collector:4318/v1/traces",
        )]);
        assert!(enabled_with(&traces_only, TRACES));
        assert!(!enabled_with(&traces_only, METRICS));
        let no_logs = env(&[endpoint, ("OTEL_LOGS_EXPORTER", "none")]);
        assert!(!enabled_with(&no_logs, LOGS));
        assert!(enabled_with(&no_logs, METRICS));
    }

    #[test]
    fn signals_leave_over_http_unless_grpc_is_named() {
        assert_eq!(transport(&env(&[]), TRACES), Transport::Http);
        assert_eq!(
            transport(&env(&[("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc")]), METRICS),
            Transport::Grpc
        );
        assert_eq!(
            transport(
                &env(&[("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")]),
                LOGS
            ),
            Transport::Http
        );
        let traces_grpc = env(&[
            ("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf"),
            ("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL", "grpc"),
        ]);
        assert_eq!(transport(&traces_grpc, TRACES), Transport::Grpc);
        assert_eq!(transport(&traces_grpc, LOGS), Transport::Http);
    }

    /// A span, a process metric and a log record inside the span, through in-memory
    /// exporters: the record carries the span's trace and span ids, and so does the
    /// stdout line.
    #[test]
    fn a_span_a_process_metric_and_a_log_record_that_carries_the_trace_id() {
        use std::sync::{Arc, Mutex};

        use opentelemetry_sdk::logs::InMemoryLogExporter;
        use opentelemetry_sdk::metrics::InMemoryMetricExporter;
        use opentelemetry_sdk::trace::InMemorySpanExporter;
        use tracing_opentelemetry::OpenTelemetrySpanExt as _;

        let spans = InMemorySpanExporter::default();
        let traces = SdkTracerProvider::builder()
            .with_simple_exporter(Scrub(spans.clone()))
            .build();
        let logs = InMemoryLogExporter::default();
        let loggers = SdkLoggerProvider::builder()
            .with_simple_exporter(logs.clone())
            .build();
        let metrics = InMemoryMetricExporter::default();
        let meters = SdkMeterProvider::builder()
            .with_periodic_exporter(metrics.clone())
            .build();
        let _process = ProcessMetrics::register(&meters);

        #[derive(Clone, Default)]
        struct Lines(Arc<Mutex<Vec<u8>>>);
        impl std::io::Write for Lines {
            fn write(&mut self, b: &[u8]) -> std::io::Result<usize> {
                self.0.lock().unwrap().extend_from_slice(b);
                Ok(b.len())
            }
            fn flush(&mut self) -> std::io::Result<()> {
                Ok(())
            }
        }
        let stdout = Lines::default();
        let writer = stdout.clone();
        let subscriber = tracing_subscriber::registry()
            .with(stdout_layer(move || writer.clone()))
            .with(tracing_opentelemetry::layer().with_tracer(traces.tracer("test")))
            .with(OpenTelemetryTracingBridge::new(&loggers));
        let (trace_id, span_id) = tracing::subscriber::with_default(subscriber, || {
            let span = tracing::info_span!("slice_plate");
            let _entered = span.enter();
            tracing::info!(stage = "slice", "a stage ended");
            let sc = span.context().span().span_context().clone();
            (sc.trace_id(), sc.span_id())
        });

        traces.force_flush().unwrap();
        let finished = spans.get_finished_spans().unwrap();
        assert_eq!(finished.len(), 1);
        assert_eq!(finished[0].name, "slice_plate");
        assert_eq!(finished[0].span_context.trace_id(), trace_id);

        loggers.force_flush().unwrap();
        let records = logs.get_emitted_logs().unwrap();
        assert_eq!(records.len(), 1);
        let context = records[0].record.trace_context().expect("a trace context");
        assert_eq!(context.trace_id, trace_id);
        assert_eq!(context.span_id, span_id);

        let line = String::from_utf8(stdout.0.lock().unwrap().clone()).unwrap();
        let json: serde_json::Value = serde_json::from_str(line.trim()).unwrap();
        assert_eq!(json["trace_id"], trace_id.to_string());
        assert_eq!(json["span_id"], span_id.to_string());
        assert_eq!(json["fields"]["message"], "a stage ended");

        meters.force_flush().unwrap();
        let exported = metrics.get_finished_metrics().unwrap();
        let names: Vec<String> = exported
            .iter()
            .flat_map(|rm| rm.scope_metrics())
            .flat_map(|sm| sm.metrics())
            .map(|m| m.name().to_owned())
            .collect();
        for name in [
            "process.cpu.time",
            "process.uptime",
            "process.memory.usage",
            "process.unix.file_descriptor.count",
        ] {
            assert!(names.iter().any(|n| n == name), "{name} in {names:?}");
        }
    }

    #[test]
    fn service_name_defaults_to_the_slicer() {
        assert_eq!(service_name(&env(&[])), "scadbuddy-slicer");
        assert_eq!(
            service_name(&env(&[("OTEL_SERVICE_NAME", "scadbuddy-cli")])),
            "scadbuddy-cli"
        );
    }

    fn span(
        attributes: Vec<KeyValue>,
        events: Vec<Event>,
        links: Vec<Link>,
        status: Status,
    ) -> SpanData {
        let mut span_events = SpanEvents::default();
        span_events.events = events;
        let mut span_links = SpanLinks::default();
        span_links.links = links;
        SpanData {
            span_context: SpanContext::empty_context(),
            parent_span_id: SpanId::INVALID,
            parent_span_is_remote: false,
            span_kind: SpanKind::Internal,
            name: "RunActivity:slice_plate".into(),
            start_time: SystemTime::now(),
            end_time: SystemTime::now(),
            attributes,
            dropped_attributes_count: 0,
            events: span_events,
            links: span_links,
            status,
            instrumentation_scope: InstrumentationScope::builder("scadbuddy").build(),
        }
    }

    fn keys(kvs: &[KeyValue]) -> Vec<String> {
        kvs.iter().map(|kv| kv.key.to_string()).collect()
    }

    fn exception(attributes: Vec<KeyValue>) -> Event {
        Event::new("exception", SystemTime::now(), attributes, 0)
    }

    #[test]
    fn scrub_drops_messages_and_urls_from_spans_and_events() {
        let out = scrub(span(
            vec![
                KeyValue::new("temporalWorkflowID", "wf-1"),
                KeyValue::new("url.full", "https://s3/bucket/key?X-Amz-Signature=secret"),
                KeyValue::new("http.request.header.x-scadbuddy-transfer", "token"),
                KeyValue::new("http.status_text", "Not Found: My Secret Box.3mf"),
                KeyValue::new("net.sock.peer.addr", "10.0.0.7"),
                KeyValue::new("net.sock.peer.port", 51234),
            ],
            vec![exception(vec![
                KeyValue::new("exception.type", "SlicerError"),
                KeyValue::new("exception.message", "stderr: /tmp/My Secret Box.3mf"),
                KeyValue::new("error", "got 'secret'"),
            ])],
            vec![],
            Status::Unset,
        ));
        assert_eq!(keys(&out.attributes), ["temporalWorkflowID"]);
        assert_eq!(keys(&out.events.events[0].attributes), ["exception.type"]);
        assert_eq!(out.status, Status::Unset);
    }

    #[test]
    fn scrub_drops_attributes_from_links() {
        let mut link = Link::with_context(SpanContext::empty_context());
        link.attributes = vec![
            KeyValue::new("kind", "follows"),
            KeyValue::new("url.full", "https://s3/bucket/key?X-Amz-Signature=secret"),
        ];
        let out = scrub(span(vec![], vec![], vec![link], Status::Unset));
        assert_eq!(keys(&out.links.links[0].attributes), ["kind"]);
    }

    #[test]
    fn an_error_status_is_described_by_its_exception_type_or_error() {
        let typed = scrub(span(
            vec![],
            vec![exception(vec![
                KeyValue::new("exception.type", "SlicerError"),
                KeyValue::new("exception.message", "/tmp/My Secret Box.3mf"),
            ])],
            vec![],
            Status::error("slicer exited 1: /tmp/My Secret Box.3mf"),
        ));
        assert_eq!(typed.status, Status::error("SlicerError"));
        let untyped = scrub(span(vec![], vec![], vec![], Status::error("got 'secret'")));
        assert_eq!(untyped.status, Status::error("error"));
        let bare = scrub(span(vec![], vec![], vec![], Status::error("")));
        assert_eq!(bare.status, Status::error(""));
    }
}
