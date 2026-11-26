// Package telemetry is the OpenTelemetry setup that every Go module of ScadBuddy uses
// (#7667, epic #988): traces, metrics and logs over OTLP/HTTP, set by the standard OTEL_*
// variables, W3C trace context in and out, the Go runtime's and the process's metrics,
// and a bounded flush at the end. A signal with no endpoint is off and costs nothing: no
// exporter and no provider, and the wrappers below give back what they wrap.
//
// It never touches the global providers. The Kiota libraries start spans of their own on
// the global tracer, which would carry the host and request details; leaving it the no-op
// keeps them out. What a span or a metric records is a route template, a method, a status,
// an id or a statement's text with its values bound apart, never a path, a query string, a
// header or a body.
package telemetry

import (
	"context"
	"errors"
	"log/slog"
	"net/url"
	"os"
	"path"
	"runtime/debug"
	"strings"
	"sync"

	"github.com/google/uuid"
	"go.opentelemetry.io/contrib/instrumentation/runtime"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/exporters/otlp/otlplog/otlploghttp"
	"go.opentelemetry.io/otel/exporters/otlp/otlpmetric/otlpmetrichttp"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
	"go.opentelemetry.io/otel/metric"
	"go.opentelemetry.io/otel/propagation"
	sdklog "go.opentelemetry.io/otel/sdk/log"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/trace"
)

// Options say how one process sets up its telemetry.
type Options struct {
	// ServiceName is the resource's service.name; OTEL_SERVICE_NAME overrides it.
	ServiceName string
	// DefaultEndpoint is the base URL used when no OTEL_EXPORTER_OTLP_* endpoint is set.
	// Empty, which is what a ScadBuddy service uses, means off without one.
	DefaultEndpoint string
	// Endpoint is a base URL that wins over the environment (a command line flag).
	Endpoint string
	// Disabled turns every signal off (a command line flag).
	Disabled bool
	// DropParentlessClients makes the default sampler the API's (backend
	// core/tracing.py): a CLIENT span with no parent is dropped, so a query from a
	// background loop is not a trace of its own. OTEL_TRACES_SAMPLER replaces it.
	DropParentlessClients bool
}

// Signal is one of the three exported signals.
type Signal struct {
	path, envEndpoint, envExporter string
}

var (
	Traces  = Signal{"/v1/traces", "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "OTEL_TRACES_EXPORTER"}
	Metrics = Signal{"/v1/metrics", "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", "OTEL_METRICS_EXPORTER"}
	Logs    = Signal{"/v1/logs", "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT", "OTEL_LOGS_EXPORTER"}
)

// Endpoint is where a signal is sent, or "" if it is off: the option's base URL, else the
// signal's own variable as is, else OTEL_EXPORTER_OTLP_ENDPOINT's base, else the default's,
// as the SDK reads them. OTEL_SDK_DISABLED=true turns every signal off. The signal's
// exporter variable may be unset or a comma list that names otlp; none turns it off, and
// any other value too, with a warning, since only the OTLP exporter ships.
func Endpoint(opts Options, s Signal, getenv func(string) string) string {
	if opts.Disabled || strings.EqualFold(strings.TrimSpace(getenv("OTEL_SDK_DISABLED")), "true") {
		return ""
	}
	if raw := strings.TrimSpace(getenv(s.envExporter)); raw != "" {
		names := strings.Split(strings.ToLower(raw), ",")
		otlp := false
		for i, n := range names {
			names[i] = strings.TrimSpace(n)
			otlp = otlp || names[i] == "otlp"
		}
		if !otlp {
			if len(names) != 1 || names[0] != "none" {
				slog.Warn("telemetry: the exporter variable names no exporter this build ships (only otlp); the signal is off",
					"variable", s.envExporter, "value", raw)
			}
			return ""
		}
	}
	if opts.Endpoint != "" {
		return join(opts.Endpoint, s.path)
	}
	if v := strings.TrimSpace(getenv(s.envEndpoint)); v != "" {
		return v
	}
	if v := strings.TrimSpace(getenv("OTEL_EXPORTER_OTLP_ENDPOINT")); v != "" {
		return join(v, s.path)
	}
	if opts.DefaultEndpoint != "" {
		return join(opts.DefaultEndpoint, s.path)
	}
	return ""
}

// join appends a signal's path to a base URL's path.
func join(base, p string) string {
	u, err := url.Parse(base)
	if err != nil {
		return base
	}
	u.Path = path.Join("/", u.Path, p)
	return u.String()
}

// Telemetry is one process's providers, each nil when its signal is off; a nil *Telemetry
// is all off.
type Telemetry struct {
	Traces *sdktrace.TracerProvider
	Meters *sdkmetric.MeterProvider
	Logs   *sdklog.LoggerProvider
}

// propagator is the W3C trace context, the only one the API reads (core/tracing.py).
var propagator = propagation.TraceContext{}

// Start sets up the providers the options and the environment ask for, and with metrics
// on, the runtime's and the process's metrics. It makes no network call: an unreachable
// collector shows only as one logged export error.
func Start(opts Options) (*Telemetry, error) {
	tracesURL := Endpoint(opts, Traces, os.Getenv)
	metricsURL := Endpoint(opts, Metrics, os.Getenv)
	logsURL := Endpoint(opts, Logs, os.Getenv)
	t := &Telemetry{}
	if tracesURL == "" && metricsURL == "" && logsURL == "" {
		return t, nil
	}
	otel.SetErrorHandler(&logOnce{})
	res, err := newResource(opts.ServiceName)
	if err != nil {
		return nil, err
	}
	ctx := context.Background()
	if tracesURL != "" {
		exp, err := otlptracehttp.New(ctx, otlptracehttp.WithEndpointURL(tracesURL))
		if err != nil {
			return nil, err
		}
		options := []sdktrace.TracerProviderOption{sdktrace.WithBatcher(exp), sdktrace.WithResource(res)}
		// Without OTEL_TRACES_SAMPLER; with it, the SDK reads it.
		if opts.DropParentlessClients && os.Getenv("OTEL_TRACES_SAMPLER") == "" {
			options = append(options, sdktrace.WithSampler(sdktrace.ParentBased(noParentlessClients{})))
		}
		t.Traces = sdktrace.NewTracerProvider(options...)
	}
	if metricsURL != "" {
		// Cumulative whatever OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE says:
		// Prometheus's OTLP receiver refuses delta.
		exp, err := otlpmetrichttp.New(ctx, otlpmetrichttp.WithEndpointURL(metricsURL),
			otlpmetrichttp.WithTemporalitySelector(sdkmetric.DefaultTemporalitySelector))
		if err != nil {
			return nil, err
		}
		// The interval is OTEL_METRIC_EXPORT_INTERVAL's (default 60 s), read by the SDK.
		t.Meters = sdkmetric.NewMeterProvider(sdkmetric.WithResource(res), sdkmetric.WithReader(
			sdkmetric.NewPeriodicReader(exp, sdkmetric.WithProducer(runtime.NewProducer()))))
		if err := Instrument(t.Meters); err != nil {
			return nil, err
		}
	}
	if logsURL != "" {
		exp, err := otlploghttp.New(ctx, otlploghttp.WithEndpointURL(logsURL))
		if err != nil {
			return nil, err
		}
		t.Logs = sdklog.NewLoggerProvider(sdklog.WithResource(res), sdklog.WithProcessor(sdklog.NewBatchProcessor(exp)))
	}
	return t, nil
}

// Instrument starts the Go runtime's metrics (contrib's runtime instrumentation, plus the
// GC's cycles and pauses and every memory class) and the process's (CPU time, resident
// memory, open descriptors, uptime) on mp. Start calls it; a test calls it on a reader's
// provider.
func Instrument(mp metric.MeterProvider) error {
	if err := runtime.Start(runtime.WithMeterProvider(mp)); err != nil {
		return err
	}
	return startProcess(mp)
}

// Tracer is the tracer provider, or nil when tracing is off.
func (t *Telemetry) Tracer() trace.TracerProvider {
	if t == nil || t.Traces == nil {
		return nil
	}
	return t.Traces
}

// Meter is the meter provider, or nil when metrics are off.
func (t *Telemetry) Meter() metric.MeterProvider {
	if t == nil || t.Meters == nil {
		return nil
	}
	return t.Meters
}

// Shutdown flushes what is buffered and stops the providers, all at once, until ctx ends.
func (t *Telemetry) Shutdown(ctx context.Context) {
	if t == nil {
		return
	}
	var wg sync.WaitGroup
	if t.Traces != nil {
		wg.Go(func() { _ = t.Traces.Shutdown(ctx) })
	}
	if t.Meters != nil {
		wg.Go(func() { _ = t.Meters.Shutdown(ctx) })
	}
	if t.Logs != nil {
		wg.Go(func() { _ = t.Logs.Shutdown(ctx) })
	}
	wg.Wait()
}

// newResource is service.name, the binary's version if it has one, and
// service.instance.id the host's name (a random one if it has none): the collector
// replaces host.name with its own. OTEL_SERVICE_NAME and OTEL_RESOURCE_ATTRIBUTES override
// them.
func newResource(service string) (*resource.Resource, error) {
	instance, err := os.Hostname()
	if err != nil || instance == "" {
		instance = uuid.NewString()
	}
	attrs := []attribute.KeyValue{
		attribute.String("service.name", service),
		attribute.String("service.instance.id", instance),
	}
	if info, ok := debug.ReadBuildInfo(); ok && info.Main.Version != "" && info.Main.Version != "(devel)" {
		attrs = append(attrs, attribute.String("service.version", info.Main.Version))
	}
	res, err := resource.New(context.Background(),
		resource.WithAttributes(attrs...), resource.WithHost(), resource.WithTelemetrySDK(), resource.WithFromEnv())
	if errors.Is(err, resource.ErrPartialResource) {
		// A bad OTEL_RESOURCE_ATTRIBUTES entry is skipped, the rest kept.
		slog.Warn("telemetry: part of the resource could not be read", "err", err)
		return res, nil
	}
	return res, err
}

// noParentlessClients is the root sampler of core/tracing.py: a CLIENT span with no
// parent is dropped, everything else kept.
type noParentlessClients struct{}

func (noParentlessClients) ShouldSample(p sdktrace.SamplingParameters) sdktrace.SamplingResult {
	state := trace.SpanContextFromContext(p.ParentContext).TraceState()
	if p.Kind == trace.SpanKindClient {
		return sdktrace.SamplingResult{Decision: sdktrace.Drop, Tracestate: state}
	}
	return sdktrace.SamplingResult{Decision: sdktrace.RecordAndSample, Tracestate: state}
}

func (noParentlessClients) Description() string { return "NoParentlessClients" }

// logOnce logs the SDK's first error, which is how an unreachable collector shows, and
// drops the rest, so a process off the LAN does not fill its log.
type logOnce struct{ once sync.Once }

func (l *logOnce) Handle(err error) {
	l.once.Do(func() {
		slog.Warn("telemetry export failed; later telemetry errors are not logged", "err", err)
	})
}
