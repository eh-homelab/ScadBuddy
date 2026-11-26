package main

import (
	"context"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"time"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/metric"
	metricnoop "go.opentelemetry.io/otel/metric/noop"
	"go.opentelemetry.io/otel/trace"
	tracenoop "go.opentelemetry.io/otel/trace/noop"

	shared "github.com/eh-homelab/ScadBuddy/tools/go/telemetry"

	"github.com/eh-homelab/ScadBuddy/tools/go/fleetd/internal/report"
)

// telemetryDefaultEndpoint is the homelab's collector, as for bambuddy-fs
// (eh-homelab/clusters#2221).
const telemetryDefaultEndpoint = "https://otlp.internal.nullreference.io"

const (
	serviceName = "fleetd"
	scope       = "github.com/eh-homelab/ScadBuddy/tools/go/fleetd"
)

// telemetryConfig is the command line's say: --otel-endpoint and --no-telemetry.
type telemetryConfig struct {
	Endpoint string
	Disabled bool
}

// loggedKeys are the only log attributes sent over OTLP: ids and counts. A path, a remote
// or an error's text stays on stderr.
var loggedKeys = map[string]bool{"session": true, "sessions": true, "sent": true, "failed": true,
	"config_dirs": true, "version": true}

// tel records fleetd's spans and metrics. Its span and metric attributes are counts and
// results only: never a path, a token, a remote or a session's facts.
type tel struct {
	shared   *shared.Telemetry
	tracer   trace.Tracer
	writes   metric.Int64Counter
	sessions metric.Int64Gauge
}

// startTelemetry starts the shared setup and the slog default (text on stderr; with logs
// on, each record over OTLP with loggedKeys only). A bad OTEL_* setting turns telemetry
// off and never stops the service.
func startTelemetry(cfg telemetryConfig, stderr io.Writer) (*tel, func(time.Duration)) {
	text := slog.NewTextHandler(stderr, nil)
	slog.SetDefault(slog.New(text))
	t, err := shared.Start(shared.Options{ServiceName: serviceName, DefaultEndpoint: telemetryDefaultEndpoint,
		Endpoint: cfg.Endpoint, Disabled: cfg.Disabled})
	if err != nil {
		slog.Warn("fleetd: telemetry is off", "err", err)
		t = &shared.Telemetry{}
	}
	if t.Logs != nil {
		slog.SetDefault(slog.New(t.LogHandler(text, serviceName, func(a slog.Attr) bool { return loggedKeys[a.Key] })))
	}
	var tp trace.TracerProvider = tracenoop.NewTracerProvider()
	if p := t.Tracer(); p != nil {
		tp = p
	}
	var mp metric.MeterProvider = metricnoop.NewMeterProvider()
	if p := t.Meter(); p != nil {
		mp = p
	}
	m := mp.Meter(scope)
	writes, _ := m.Int64Counter("fleetd.registry.writes",
		metric.WithDescription("Registry writes (machine and sessions) by result: ok or failed."))
	sessions, _ := m.Int64Gauge("fleetd.sessions",
		metric.WithDescription("Live Claude Code sessions found at the last scan."))
	out := &tel{shared: t, tracer: tp.Tracer(scope), writes: writes, sessions: sessions}
	return out, func(within time.Duration) {
		ctx, cancel := context.WithTimeout(context.Background(), within)
		defer cancel()
		t.Shutdown(ctx)
	}
}

// transport wraps a transport in a CLIENT span per request (method, status, the API's host;
// never the URL's path).
func (t *tel) transport(peer string) func(http.RoundTripper) http.RoundTripper {
	return func(base http.RoundTripper) http.RoundTripper { return t.shared.Transport(base, peer) }
}

// scan runs one scan in a `fleetd.scan` span, with its counts.
func (t *tel) scan(ctx context.Context, do func(context.Context) (report.Result, error)) {
	ctx, span := t.tracer.Start(ctx, "fleetd.scan")
	defer span.End()
	res, err := do(ctx)
	span.SetAttributes(attribute.Int("fleetd.sessions", res.Sessions), attribute.Int("fleetd.sent", res.Sent),
		attribute.Int("fleetd.failed", res.Failed))
	t.sessions.Record(ctx, int64(res.Sessions))
	t.writes.Add(ctx, int64(res.Sent), metric.WithAttributes(attribute.String("result", "ok")))
	t.writes.Add(ctx, int64(res.Failed), metric.WithAttributes(attribute.String("result", "failed")))
	if err != nil {
		// The status is the error's kind, never its text, which can quote a path.
		span.SetStatus(codes.Error, fmt.Sprintf("%T", err))
	}
}
