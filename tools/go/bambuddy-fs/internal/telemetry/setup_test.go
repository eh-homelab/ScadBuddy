package telemetry

import (
	"bytes"
	"context"
	"log/slog"
	"strings"
	"testing"
	"time"
)

func TestEndpoint(t *testing.T) {
	cases := []struct {
		name            string
		cfg             Config
		env             map[string]string
		traces, metrics string
	}{
		{"default", Config{}, nil,
			"https://otlp.internal.nullreference.io/v1/traces", "https://otlp.internal.nullreference.io/v1/metrics"},
		{"base from the environment", Config{}, map[string]string{"OTEL_EXPORTER_OTLP_ENDPOINT": "http://c:4318/"},
			"http://c:4318/v1/traces", "http://c:4318/v1/metrics"},
		{"a signal's own variable is used as is", Config{}, map[string]string{
			"OTEL_EXPORTER_OTLP_ENDPOINT":         "http://c:4318",
			"OTEL_EXPORTER_OTLP_TRACES_ENDPOINT":  "http://t:4318/custom",
			"OTEL_EXPORTER_OTLP_METRICS_ENDPOINT": "http://m:4318/m",
		}, "http://t:4318/custom", "http://m:4318/m"},
		{"the flag wins", Config{Endpoint: "https://flag.example/otlp"}, map[string]string{
			"OTEL_EXPORTER_OTLP_ENDPOINT":        "http://c:4318",
			"OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "http://t:4318/custom",
		}, "https://flag.example/otlp/v1/traces", "https://flag.example/otlp/v1/metrics"},
		{"--no-telemetry", Config{Endpoint: "https://flag.example", Disabled: true}, nil, "", ""},
		{"OTEL_SDK_DISABLED", Config{}, map[string]string{"OTEL_SDK_DISABLED": "TRUE"}, "", ""},
		{"OTEL_TRACES_EXPORTER=none", Config{}, map[string]string{"OTEL_TRACES_EXPORTER": "none"},
			"", "https://otlp.internal.nullreference.io/v1/metrics"},
		{"OTEL_METRICS_EXPORTER=none", Config{}, map[string]string{"OTEL_METRICS_EXPORTER": "none"},
			"https://otlp.internal.nullreference.io/v1/traces", ""},
	}
	for _, c := range cases {
		getenv := func(k string) string { return c.env[k] }
		if got := endpoint(c.cfg, traces, getenv); got != c.traces {
			t.Errorf("%s: traces %q, want %q", c.name, got, c.traces)
		}
		if got := endpoint(c.cfg, metrics, getenv); got != c.metrics {
			t.Errorf("%s: metrics %q, want %q", c.name, got, c.metrics)
		}
	}
}

func TestDisabledBuildsNothing(t *testing.T) {
	before := rec()
	stop, err := Start(Config{Disabled: true})
	if err != nil {
		t.Fatal(err)
	}
	if rec() != before {
		t.Fatal("a recorder was installed")
	}
	stop(time.Second)
}

// An unresolvable or refusing collector, as off the LAN, delays neither the start nor
// an operation, and the stop is bounded.
func TestAnUnreachableCollectorDelaysNothing(t *testing.T) {
	const within = 300 * time.Millisecond
	for _, endpoint := range []string{"https://bambuddy-fs-test.invalid", "http://127.0.0.1:1"} {
		started := time.Now()
		stop, err := Start(Config{Endpoint: endpoint})
		if err != nil {
			t.Fatal(err)
		}
		if d := time.Since(started); d > 500*time.Millisecond {
			t.Fatalf("%s: start took %v", endpoint, d)
		}
		started = time.Now()
		for range 10000 {
			_, op := StartOp(context.Background(), "mkdir", FolderID(1))
			op.End(0)
		}
		if d := time.Since(started); d > 2*time.Second {
			t.Fatalf("%s: operations took %v", endpoint, d)
		}
		started = time.Now()
		stop(within)
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
