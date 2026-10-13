// Package telemetry is the mount's OpenTelemetry: traces and metrics over OTLP/HTTP,
// to the homelab's collector unless the OTEL_* variables or --otel-endpoint say
// otherwise. Nothing here may slow the mount: export runs in the background, the
// first export error is logged once, and shutdown is bounded.
//
// It never touches the global tracer or meter provider. The Kiota libraries start
// spans of their own on the global one, which would carry the host and request
// details; leaving it the no-op keeps them out.
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
	"time"

	"github.com/google/uuid"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/exporters/otlp/otlpmetric/otlpmetrichttp"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
)

// DefaultEndpoint is the collector the homelab exposes to the LAN (eh-homelab/clusters#2221).
const DefaultEndpoint = "https://otlp.internal.nullreference.io"

// ServiceName is the resource's service.name, and so the Prometheus job label.
const ServiceName = "bambuddy-fs"

// shutdownTimeout bounds the final flush on unmount.
var shutdownTimeout = 5 * time.Second

// Config is the command line's say: Endpoint (--otel-endpoint) is a base URL, like
// OTEL_EXPORTER_OTLP_ENDPOINT, and wins over the environment; Disabled
// (--no-telemetry) turns both signals off.
type Config struct {
	Endpoint string
	Disabled bool
}

// signal is one of the two exported signals.
type signal struct {
	path, envEndpoint, envExporter string
}

var (
	traces  = signal{"/v1/traces", "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "OTEL_TRACES_EXPORTER"}
	metrics = signal{"/v1/metrics", "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", "OTEL_METRICS_EXPORTER"}
)

// endpoint is where a signal is sent, or "" if it is off: the flag's base URL, else the
// signal's own variable as is, else OTEL_EXPORTER_OTLP_ENDPOINT's base, else
// DefaultEndpoint's, as the SDK reads them.
func endpoint(cfg Config, s signal, getenv func(string) string) string {
	if cfg.Disabled || strings.EqualFold(getenv("OTEL_SDK_DISABLED"), "true") || strings.EqualFold(getenv(s.envExporter), "none") {
		return ""
	}
	if cfg.Endpoint != "" {
		return join(cfg.Endpoint, s.path)
	}
	if v := getenv(s.envEndpoint); v != "" {
		return v
	}
	if v := getenv("OTEL_EXPORTER_OTLP_ENDPOINT"); v != "" {
		return join(v, s.path)
	}
	return join(DefaultEndpoint, s.path)
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

// Start sets up the providers the mount records to and returns the function that
// flushes and stops them, within shutdownTimeout. It makes no network call: an
// unreachable collector shows only as one logged export error.
func Start(cfg Config) (stop func(), err error) {
	tracesURL, metricsURL := endpoint(cfg, traces, os.Getenv), endpoint(cfg, metrics, os.Getenv)
	if tracesURL == "" && metricsURL == "" {
		return func() {}, nil
	}
	otel.SetErrorHandler(&logOnce{})
	res, err := newResource()
	if err != nil {
		return nil, err
	}
	ctx := context.Background()
	var shutdowns []func(context.Context) error
	var tp *sdktrace.TracerProvider
	var mp *sdkmetric.MeterProvider
	if tracesURL != "" {
		exp, err := otlptracehttp.New(ctx, otlptracehttp.WithEndpointURL(tracesURL))
		if err != nil {
			return nil, err
		}
		// The sampler is OTEL_TRACES_SAMPLER's, read by the SDK.
		tp = sdktrace.NewTracerProvider(sdktrace.WithBatcher(exp), sdktrace.WithResource(res))
		shutdowns = append(shutdowns, tp.Shutdown)
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
		mp = sdkmetric.NewMeterProvider(sdkmetric.WithReader(sdkmetric.NewPeriodicReader(exp)), sdkmetric.WithResource(res))
		shutdowns = append(shutdowns, mp.Shutdown)
	}
	restore := install(tp, mp)
	return func() {
		ctx, cancel := context.WithTimeout(context.Background(), shutdownTimeout)
		defer cancel()
		var wg sync.WaitGroup
		for _, shutdown := range shutdowns {
			wg.Go(func() { _ = shutdown(ctx) })
		}
		wg.Wait()
		restore()
	}, nil
}

// newResource is service.name bambuddy-fs, the binary's version if it has one, and
// service.instance.id the host's name (a random one if it has none), so workstations'
// series stay apart: the collector replaces host.name with its own. OTEL_SERVICE_NAME
// and OTEL_RESOURCE_ATTRIBUTES override them.
func newResource() (*resource.Resource, error) {
	instance, err := os.Hostname()
	if err != nil || instance == "" {
		instance = uuid.NewString()
	}
	attrs := []attribute.KeyValue{
		attribute.String("service.name", ServiceName),
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

// logOnce logs the SDK's first error, which is how an unreachable collector shows,
// and drops the rest, so a mount off the LAN does not fill its log.
type logOnce struct{ once sync.Once }

func (l *logOnce) Handle(err error) {
	l.once.Do(func() {
		slog.Warn("telemetry export failed; later telemetry errors are not logged", "err", err)
	})
}
