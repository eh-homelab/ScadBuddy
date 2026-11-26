package telemetry

import (
	"bytes"
	"context"
	"log/slog"
	"strings"
	"testing"
	"time"

	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/trace"
)

func TestEndpoint(t *testing.T) {
	const def = "https://otlp.example"
	withDefault := Options{DefaultEndpoint: def}
	cases := []struct {
		name            string
		opts            Options
		env             map[string]string
		traces, metrics string
	}{
		{"no endpoint is off", Options{}, nil, "", ""},
		{"the default", withDefault, nil, def + "/v1/traces", def + "/v1/metrics"},
		{"base from the environment", Options{}, map[string]string{"OTEL_EXPORTER_OTLP_ENDPOINT": "http://c:4318/"},
			"http://c:4318/v1/traces", "http://c:4318/v1/metrics"},
		{"a signal's own variable is used as is", withDefault, map[string]string{
			"OTEL_EXPORTER_OTLP_ENDPOINT":         "http://c:4318",
			"OTEL_EXPORTER_OTLP_TRACES_ENDPOINT":  "http://t:4318/custom",
			"OTEL_EXPORTER_OTLP_METRICS_ENDPOINT": "http://m:4318/m",
		}, "http://t:4318/custom", "http://m:4318/m"},
		{"the option wins", Options{Endpoint: "https://flag.example/otlp"}, map[string]string{
			"OTEL_EXPORTER_OTLP_ENDPOINT":        "http://c:4318",
			"OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "http://t:4318/custom",
		}, "https://flag.example/otlp/v1/traces", "https://flag.example/otlp/v1/metrics"},
		{"disabled", Options{Endpoint: "https://flag.example", Disabled: true}, nil, "", ""},
		{"OTEL_SDK_DISABLED", withDefault, map[string]string{"OTEL_SDK_DISABLED": "TRUE"}, "", ""},
		{"OTEL_TRACES_EXPORTER=none", withDefault, map[string]string{"OTEL_TRACES_EXPORTER": "none"},
			"", def + "/v1/metrics"},
		{"OTEL_METRICS_EXPORTER=none", withDefault, map[string]string{"OTEL_METRICS_EXPORTER": "none"},
			def + "/v1/traces", ""},
		{"a list that names otlp", withDefault, map[string]string{"OTEL_TRACES_EXPORTER": "console, OTLP"},
			def + "/v1/traces", def + "/v1/metrics"},
		{"an exporter that does not ship", withDefault, map[string]string{"OTEL_TRACES_EXPORTER": "zipkin"},
			"", def + "/v1/metrics"},
	}
	for _, c := range cases {
		getenv := func(k string) string { return c.env[k] }
		if got := Endpoint(c.opts, Traces, getenv); got != c.traces {
			t.Errorf("%s: traces %q, want %q", c.name, got, c.traces)
		}
		if got := Endpoint(c.opts, Metrics, getenv); got != c.metrics {
			t.Errorf("%s: metrics %q, want %q", c.name, got, c.metrics)
		}
	}
}

func TestNoEndpointBuildsNothing(t *testing.T) {
	t.Setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
	t.Setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "")
	tel, err := Start(Options{ServiceName: "x"})
	if err != nil {
		t.Fatal(err)
	}
	if tel.Traces != nil || tel.Meters != nil || tel.Logs != nil || tel.Tracer() != nil {
		t.Fatal("a provider was built")
	}
	tel.Shutdown(context.Background())
}

// An unresolvable or refusing collector delays neither the start nor a span, and the
// stop is bounded.
func TestAnUnreachableCollectorDelaysNothing(t *testing.T) {
	const within = 300 * time.Millisecond
	for _, endpoint := range []string{"https://scadbuddy-telemetry-test.invalid", "http://127.0.0.1:9"} {
		started := time.Now()
		tel, err := Start(Options{ServiceName: "x", Endpoint: endpoint})
		if err != nil {
			t.Fatal(err)
		}
		if d := time.Since(started); d > 500*time.Millisecond {
			t.Fatalf("%s: start took %v", endpoint, d)
		}
		tracer := tel.Tracer().Tracer("test")
		for range 10000 {
			_, span := tracer.Start(context.Background(), "s")
			span.End()
		}
		started = time.Now()
		ctx, cancel := context.WithTimeout(context.Background(), within)
		tel.Shutdown(ctx)
		cancel()
		if d := time.Since(started); d > within+time.Second {
			t.Fatalf("%s: stop took %v", endpoint, d)
		}
	}
}

func TestOnlyTheFirstErrorIsLogged(t *testing.T) {
	var buf bytes.Buffer
	prev := slog.Default()
	slog.SetDefault(slog.New(slog.NewTextHandler(&buf, nil)))
	t.Cleanup(func() { slog.SetDefault(prev) })
	h := &logOnce{}
	h.Handle(context.DeadlineExceeded)
	h.Handle(context.Canceled)
	if n := strings.Count(buf.String(), "telemetry export failed"); n != 1 {
		t.Fatalf("logged %d times: %s", n, buf.String())
	}
}

func TestAParentlessClientIsDropped(t *testing.T) {
	sampler := sdktrace.ParentBased(noParentlessClients{})
	decide := func(ctx context.Context, kind trace.SpanKind) sdktrace.SamplingDecision {
		return sampler.ShouldSample(sdktrace.SamplingParameters{ParentContext: ctx, Kind: kind}).Decision
	}
	if decide(context.Background(), trace.SpanKindClient) != sdktrace.Drop {
		t.Error("a parentless client span was kept")
	}
	if decide(context.Background(), trace.SpanKindServer) != sdktrace.RecordAndSample {
		t.Error("a server span was dropped")
	}
	parent := trace.ContextWithSpanContext(context.Background(), trace.NewSpanContext(trace.SpanContextConfig{
		TraceID: trace.TraceID{1}, SpanID: trace.SpanID{1}, TraceFlags: trace.FlagsSampled,
	}))
	if decide(parent, trace.SpanKindClient) != sdktrace.RecordAndSample {
		t.Error("a client span under a sampled parent was dropped")
	}
}
