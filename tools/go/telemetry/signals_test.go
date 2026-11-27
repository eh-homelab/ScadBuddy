package telemetry

import (
	"bytes"
	"context"
	"encoding/json"
	"log"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"

	"go.opentelemetry.io/otel/attribute"
	sdklog "go.opentelemetry.io/otel/sdk/log"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
)

// logs is an in-memory log exporter.
type logs struct {
	mu      sync.Mutex
	records []sdklog.Record
}

func (l *logs) Export(_ context.Context, records []sdklog.Record) error {
	l.mu.Lock()
	defer l.mu.Unlock()
	for _, r := range records {
		l.records = append(l.records, r.Clone())
	}
	return nil
}
func (l *logs) Shutdown(context.Context) error   { return nil }
func (l *logs) ForceFlush(context.Context) error { return nil }

// collect is every metric the reader holds, by name.
func collect(t *testing.T, reader *sdkmetric.ManualReader) map[string]metricdata.Metrics {
	t.Helper()
	var rm metricdata.ResourceMetrics
	if err := reader.Collect(t.Context(), &rm); err != nil {
		t.Fatal(err)
	}
	out := map[string]metricdata.Metrics{}
	for _, sm := range rm.ScopeMetrics {
		for _, m := range sm.Metrics {
			out[m.Name] = m
		}
	}
	return out
}

func TestTheRuntimeAndTheProcessAreMeasured(t *testing.T) {
	reader := sdkmetric.NewManualReader()
	mp := sdkmetric.NewMeterProvider(sdkmetric.WithReader(reader))
	t.Cleanup(func() { _ = mp.Shutdown(context.Background()) })
	if err := Instrument(mp); err != nil {
		t.Fatal(err)
	}
	got := collect(t, reader)
	for _, name := range []string{
		"go.goroutine.count", "go.memory.used", "go.processor.limit", "go.gc.count", "go.gc.pause.time",
		"go.memory.class", "process.cpu.time", "process.memory.usage", "process.unix.file_descriptor.count",
		"process.uptime",
	} {
		if _, ok := got[name]; !ok {
			t.Errorf("no %s", name)
		}
	}
	if g, ok := got["go.goroutine.count"].Data.(metricdata.Sum[int64]); !ok || len(g.DataPoints) == 0 || g.DataPoints[0].Value < 1 {
		t.Errorf("go.goroutine.count %+v", got["go.goroutine.count"].Data)
	}
}

func TestAServedRequestIsMeasuredByItsRoute(t *testing.T) {
	reader := sdkmetric.NewManualReader()
	mp := sdkmetric.NewMeterProvider(sdkmetric.WithReader(reader))
	t.Cleanup(func() { _ = mp.Shutdown(context.Background()) })
	tel := &Telemetry{Meters: mp}
	h := tel.Server(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		SetRoute(r.Context(), "/api/v1/things/{thing_id}")
		_, _ = w.Write([]byte("hello"))
	}))
	h.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest("POST", "/api/v1/things/1", bytes.NewBufferString("abc")))

	got := collect(t, reader)
	d, ok := got["http.server.request.duration"].Data.(metricdata.Histogram[float64])
	if !ok || len(d.DataPoints) != 1 || d.DataPoints[0].Count != 1 {
		t.Fatalf("duration %+v", got["http.server.request.duration"])
	}
	if v, _ := d.DataPoints[0].Attributes.Value("http.route"); v.AsString() != "/api/v1/things/{thing_id}" {
		t.Errorf("http.route %q", v.AsString())
	}
	if v, _ := d.DataPoints[0].Attributes.Value("http.response.status_code"); v.AsInt64() != 200 {
		t.Errorf("status %d", v.AsInt64())
	}
	sizes := map[string]int64{"http.server.request.body.size": 3, "http.server.response.body.size": 5}
	for name, want := range sizes {
		h, ok := got[name].Data.(metricdata.Histogram[int64])
		if !ok || len(h.DataPoints) != 1 || h.DataPoints[0].Sum != want {
			t.Errorf("%s %+v, want sum %d", name, got[name].Data, want)
		}
	}
}

func TestALogRecordCarriesItsTrace(t *testing.T) {
	exp := &logs{}
	lp := sdklog.NewLoggerProvider(sdklog.WithProcessor(sdklog.NewSimpleProcessor(exp)))
	tp := sdktrace.NewTracerProvider()
	t.Cleanup(func() { _ = lp.Shutdown(context.Background()); _ = tp.Shutdown(context.Background()) })
	tel := &Telemetry{Logs: lp, Traces: tp}
	var stderr bytes.Buffer
	logger := slog.New(tel.LogHandler(slog.NewJSONHandler(&stderr, nil), "test", nil))

	ctx, span := tp.Tracer("t").Start(t.Context(), "s")
	logger.InfoContext(ctx, "hello", "n", 1)
	span.End()

	exp.mu.Lock()
	defer exp.mu.Unlock()
	if len(exp.records) != 1 {
		t.Fatalf("%d records", len(exp.records))
	}
	r := exp.records[0]
	if r.TraceID() != span.SpanContext().TraceID() || r.SpanID() != span.SpanContext().SpanID() {
		t.Fatalf("record trace %s span %s", r.TraceID(), r.SpanID())
	}
	if r.Body().AsString() != "hello" {
		t.Fatalf("body %v", r.Body())
	}
	var line map[string]any
	if err := json.Unmarshal(stderr.Bytes(), &line); err != nil {
		t.Fatalf("stderr %q: %v", stderr.String(), err)
	}
	if line["msg"] != "hello" || line["trace_id"] != span.SpanContext().TraceID().String() ||
		line["span_id"] != span.SpanContext().SpanID().String() {
		t.Fatalf("stderr %v", line)
	}
}

func TestTheTraceIDsStayAtTheTopUnderAGroup(t *testing.T) {
	tp := sdktrace.NewTracerProvider()
	t.Cleanup(func() { _ = tp.Shutdown(context.Background()) })
	var stderr bytes.Buffer
	var off *Telemetry
	logger := slog.New(off.LogHandler(slog.NewJSONHandler(&stderr, nil), "test", nil)).
		With("a", 1).WithGroup("g").With("b", 2)

	ctx, span := tp.Tracer("t").Start(t.Context(), "s")
	logger.InfoContext(ctx, "hello", "c", 3)
	span.End()

	var line map[string]any
	if err := json.Unmarshal(stderr.Bytes(), &line); err != nil {
		t.Fatalf("stderr %q: %v", stderr.String(), err)
	}
	if line["trace_id"] != span.SpanContext().TraceID().String() ||
		line["span_id"] != span.SpanContext().SpanID().String() || line["a"] != 1.0 {
		t.Fatalf("stderr %v", line)
	}
	group, _ := line["g"].(map[string]any)
	if group["b"] != 2.0 || group["c"] != 3.0 || group["trace_id"] != nil {
		t.Fatalf("group %v", group)
	}
}

func TestWithLogsOffOnlyStderrIsWritten(t *testing.T) {
	var stderr bytes.Buffer
	var off *Telemetry
	slog.New(off.LogHandler(slog.NewJSONHandler(&stderr, nil), "test", nil)).Info("hi")
	if !bytes.Contains(stderr.Bytes(), []byte(`"msg":"hi"`)) {
		t.Fatalf("stderr %q", stderr.String())
	}
}

func TestTheDefaultOTLPCopyHasNoPathErrorOrCredentials(t *testing.T) {
	exp := &logs{}
	lp := sdklog.NewLoggerProvider(sdklog.WithProcessor(sdklog.NewSimpleProcessor(exp)))
	t.Cleanup(func() { _ = lp.Shutdown(context.Background()) })
	var stderr bytes.Buffer
	logger := slog.New((&Telemetry{Logs: lp}).LogHandler(slog.NewJSONHandler(&stderr, nil), "test", nil))
	logger.With("dsn", "postgres://u:pw@db/x").Warn("dial postgres://u:hunter2@db:5432/x failed",
		"path", "/api/v1/things/42", "error", "boom 42", "err", "boom", "url", "http://x/y", "query", "q=1",
		"upstream", "https://user:secret@host/", "group", slog.GroupValue(slog.String("path", "/p"), slog.Int("n", 3)),
		"status", 503)

	exp.mu.Lock()
	defer exp.mu.Unlock()
	r := exp.records[0]
	if strings.Contains(r.Body().AsString(), "hunter2") {
		t.Errorf("body %q holds a password", r.Body().AsString())
	}
	got := map[string]string{}
	r.WalkAttributes(func(kv attribute.KeyValue) bool {
		got[string(kv.Key)] = kv.Value.String()
		return true
	})
	for key, value := range got {
		if strings.Contains(value, "secret") || strings.Contains(value, "pw@") || strings.Contains(value, "/p") {
			t.Errorf("%s = %q", key, value)
		}
	}
	for _, key := range []string{"dsn", "path", "error", "err", "url", "query", "upstream"} {
		if _, ok := got[key]; ok {
			t.Errorf("%s reached OTLP", key)
		}
	}
	if _, ok := got["status"]; !ok {
		t.Errorf("status was dropped: %v", got)
	}
	for _, want := range []string{"hunter2", "/api/v1/things/42", "secret"} {
		if !strings.Contains(stderr.String(), want) {
			t.Errorf("stderr lost %q: %s", want, stderr.String())
		}
	}
}

func TestTheOTLPCopyKeepsOnlyWhatKeepKeeps(t *testing.T) {
	exp := &logs{}
	lp := sdklog.NewLoggerProvider(sdklog.WithProcessor(sdklog.NewSimpleProcessor(exp)))
	t.Cleanup(func() { _ = lp.Shutdown(context.Background()) })
	var stderr bytes.Buffer
	keep := func(a slog.Attr) bool { return a.Key == "file" }
	slog.New((&Telemetry{Logs: lp}).LogHandler(slog.NewJSONHandler(&stderr, nil), "test", keep)).
		Info("read failed", "file", 7, "path", "/home/someone/secret.3mf")

	exp.mu.Lock()
	defer exp.mu.Unlock()
	var keys []string
	exp.records[0].WalkAttributes(func(kv attribute.KeyValue) bool {
		keys = append(keys, string(kv.Key))
		return true
	})
	if len(keys) != 1 || keys[0] != "file" {
		t.Fatalf("OTLP attributes %v", keys)
	}
	if !bytes.Contains(stderr.Bytes(), []byte("secret.3mf")) {
		t.Fatalf("stderr lost an attribute: %s", stderr.String())
	}
}

func TestTheLogPackageStaysOffOTLP(t *testing.T) {
	prev, out, flags := slog.Default(), log.Writer(), log.Flags()
	t.Cleanup(func() { slog.SetDefault(prev); log.SetOutput(out); log.SetFlags(flags) })
	exp := &logs{}
	lp := sdklog.NewLoggerProvider(sdklog.WithProcessor(sdklog.NewSimpleProcessor(exp)))
	t.Cleanup(func() { _ = lp.Shutdown(context.Background()) })
	var stderr bytes.Buffer

	(&Telemetry{Logs: lp}).SetDefaultLogger(slog.NewJSONHandler(&stderr, nil), "test", nil)
	// What net/http's ErrorLog and httputil.ReverseProxy write.
	log.Print("http: proxy error: dial tcp 10.0.0.7:8000: connection refused")
	slog.Info("hello")

	exp.mu.Lock()
	defer exp.mu.Unlock()
	if len(exp.records) != 1 || exp.records[0].Body().AsString() != "hello" {
		var bodies []string
		for _, r := range exp.records {
			bodies = append(bodies, r.Body().AsString())
		}
		t.Fatalf("OTLP records %q, want only the slog one", bodies)
	}
	var line map[string]any
	first, _, _ := bytes.Cut(stderr.Bytes(), []byte("\n"))
	if err := json.Unmarshal(first, &line); err != nil {
		t.Fatalf("stderr %q: %v", stderr.String(), err)
	}
	if msg, _ := line["msg"].(string); !strings.Contains(msg, "10.0.0.7:8000") {
		t.Fatalf("stderr lost the log package's line: %q", stderr.String())
	}
}
