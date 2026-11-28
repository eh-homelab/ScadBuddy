package main

import (
	"context"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	oteltrace "go.opentelemetry.io/otel/trace"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/testdb"
	"github.com/eh-homelab/ScadBuddy/tools/go/telemetry"
	"github.com/eh-homelab/ScadBuddy/tools/go/telemetry/pgxtrace"
)

// recorded is telemetry to an in-memory span exporter and a manual metric reader.
type recorded struct {
	tel    *telemetry.Telemetry
	spans  *tracetest.InMemoryExporter
	reader *sdkmetric.ManualReader
}

func recordTelemetry(t *testing.T) *recorded {
	t.Helper()
	spans := tracetest.NewInMemoryExporter()
	reader := sdkmetric.NewManualReader()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSyncer(spans))
	mp := sdkmetric.NewMeterProvider(sdkmetric.WithReader(reader))
	t.Cleanup(func() {
		_ = tp.Shutdown(context.Background())
		_ = mp.Shutdown(context.Background())
	})
	return &recorded{tel: &telemetry.Telemetry{Traces: tp, Meters: mp}, spans: spans, reader: reader}
}

// named waits up to 5 s for a span of that name: a server span ends after its response
// is written, so it can reach the recorder after the client has its answer.
func (r *recorded) named(name string) []tracetest.SpanStub {
	var out []tracetest.SpanStub
	for deadline := time.Now().Add(5 * time.Second); len(out) == 0 && time.Now().Before(deadline); time.Sleep(10 * time.Millisecond) {
		for _, s := range r.spans.GetSpans() {
			if s.Name == name {
				out = append(out, s)
			}
		}
	}
	return out
}

func (r *recorded) metrics(t *testing.T) map[string]metricdata.Metrics {
	t.Helper()
	var rm metricdata.ResourceMetrics
	if err := r.reader.Collect(context.Background(), &rm); err != nil {
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

func value(s tracetest.SpanStub, key string) attribute.Value {
	for _, kv := range s.Attributes {
		if string(kv.Key) == key {
			return kv.Value
		}
	}
	return attribute.Value{}
}

func TestAServedRouteIsTracedAndMeasuredWithItsQueries(t *testing.T) {
	rec := recordTelemetry(t)
	pool := testdb.NewWith(t, func(c *pgxpool.Config) {
		c.ConnConfig.Tracer = pgxtrace.New(rec.tel.Tracer(), rec.tel.Meter())
	})
	if err := pgxtrace.ObservePool(rec.tel.Meter(), pool, serviceName); err != nil {
		t.Fatal(err)
	}
	router, err := newRouter(pool, http.NotFoundHandler(), nil, "session-events", "", callerPackages, nil, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(rec.tel.Server(router, untraced...))
	t.Cleanup(server.Close)
	a := &api{t: t, pool: pool, url: server.URL}
	a.token = a.credential("agent", "api", "crud.agent")
	missing := uuid()

	expect(t, a.ok("GET", "/sessions/"+missing+"/events/last-seq", "").raw, `{"seq":0}`)
	gone := a.do("POST", base+"/sessions/"+missing+"/events", object("events", []string{`{"type":"x"}`}))
	expect(t, gone.status, 404)
	a.do("GET", "/healthz", "")

	read := rec.named("GET " + base + "/sessions/{session_id}/events/last-seq")
	if len(read) != 1 {
		t.Fatalf("spans: %v", names(rec.spans.GetSpans()))
	}
	server0 := read[0]
	expect(t, server0.SpanKind, oteltrace.SpanKindServer)
	expect(t, value(server0, "http.route").AsString(), base+"/sessions/{session_id}/events/last-seq")
	expect(t, value(server0, "http.status_code").AsInt64(), int64(200))
	for _, kv := range server0.Attributes {
		if strings.Contains(kv.Value.Emit(), missing) {
			t.Errorf("%s holds the session id of the path", kv.Key)
		}
	}

	// Every query of the request is a child of its server span, and holds no bound value.
	var queries int
	for _, s := range rec.spans.GetSpans() {
		if s.SpanKind != oteltrace.SpanKindClient || s.SpanContext.TraceID() != server0.SpanContext.TraceID() {
			continue
		}
		queries++
		expect(t, value(s, "db.system").AsString(), "postgresql")
		if value(s, "db.query.summary").AsString() == "" {
			t.Errorf("%q has no summary", s.Name)
		}
		for _, kv := range s.Attributes {
			if strings.Contains(kv.Value.Emit(), missing) {
				t.Errorf("%q: %s holds a bound value", s.Name, kv.Key)
			}
		}
	}
	if queries == 0 {
		t.Fatalf("no query spans: %v", names(rec.spans.GetSpans()))
	}

	posted := rec.named("POST " + base + "/sessions/{session_id}/events")
	if len(posted) != 1 {
		t.Fatalf("spans: %v", names(rec.spans.GetSpans()))
	}
	expect(t, value(posted[0], "scadbuddy.problem.reason").AsString(), "session_gone")
	expect(t, value(posted[0], "scadbuddy.failure_class").AsString(), "http-404")
	expect(t, posted[0].Status.Code, codes.Unset)
	if len(rec.named("GET")) != 0 {
		t.Fatal("/healthz was traced")
	}

	got := rec.metrics(t)
	for _, name := range []string{"http.server.request.duration", "db.client.operation.duration", "db.client.connection.count"} {
		if _, ok := got[name]; !ok {
			t.Errorf("no %s", name)
		}
	}
}

func names(spans tracetest.SpanStubs) []string {
	var out []string
	for _, s := range spans {
		out = append(out, s.Name)
	}
	return out
}

// Python gets core's forward as its parent, so a forwarded request is one trace.
func TestAForwardedRequestIsOneTrace(t *testing.T) {
	rec := recordTelemetry(t)
	traceparent := make(chan string, 1)
	socket := filepath.Join(t.TempDir(), "s")
	listener, err := net.Listen("unix", socket)
	if err != nil {
		t.Fatal(err)
	}
	upstream := &http.Server{Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		traceparent <- r.Header.Get("traceparent")
		_, _ = io.WriteString(w, "python")
	})}
	go upstream.Serve(listener)
	t.Cleanup(func() { upstream.Close() })
	next, err := newUpstream("unix://"+socket, nil, rec.tel)
	if err != nil {
		t.Fatal(err)
	}
	router, err := newRouter(nil, next, nil, "", "", callerPackages, nil, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(rec.tel.Server(router, untraced...))
	t.Cleanup(server.Close)

	req, _ := http.NewRequest("GET", server.URL+"/api/v1/models", nil)
	req.Header.Set("traceparent", "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01")
	res, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	_, _ = io.ReadAll(res.Body)
	res.Body.Close()

	sent := <-traceparent
	// The server span ends after the response is written, so it can reach the recorder
	// after the client has read the body: wait for both.
	var front, forward tracetest.SpanStub
	for deadline := time.Now().Add(5 * time.Second); time.Now().Before(deadline); time.Sleep(10 * time.Millisecond) {
		front, forward = tracetest.SpanStub{}, tracetest.SpanStub{}
		for _, s := range rec.spans.GetSpans() {
			switch s.SpanKind {
			case oteltrace.SpanKindServer:
				front = s
			case oteltrace.SpanKindClient:
				forward = s
			}
		}
		if front.SpanContext.IsValid() && forward.SpanContext.IsValid() {
			break
		}
	}
	expect(t, front.SpanContext.TraceID().String(), "0af7651916cd43dd8448eb211c80319c")
	expect(t, front.Parent.SpanID().String(), "b7ad6b7169203331")
	expect(t, forward.Parent.SpanID(), front.SpanContext.SpanID())
	expect(t, value(forward, "server.address").AsString(), "scadbuddy")
	expect(t, value(forward, "http.status_code").AsInt64(), int64(200))
	// Python continues the trace under core's forward.
	expect(t, sent, "00-0af7651916cd43dd8448eb211c80319c-"+forward.SpanContext.SpanID().String()+"-01")
	if _, ok := rec.metrics(t)["http.client.request.duration"]; !ok {
		t.Error("no http.client.request.duration")
	}
}

// At a pod's start, core listens before Python does: a forward waits for it.
func TestAForwardWaitsForPythonToListen(t *testing.T) {
	socket := filepath.Join(t.TempDir(), "s")
	next, err := newUpstream("unix://"+socket, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(next)
	t.Cleanup(server.Close)
	calls := make(chan struct{}, 10)
	go func() {
		time.Sleep(700 * time.Millisecond)
		listener, err := net.Listen("unix", socket)
		if err != nil {
			return
		}
		upstream := &http.Server{Handler: http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
			calls <- struct{}{}
			w.WriteHeader(http.StatusAccepted)
		})}
		t.Cleanup(func() { upstream.Close() })
		_ = upstream.Serve(listener)
	}()
	res, err := http.Post(server.URL+"/api/v1/things", "text/plain", strings.NewReader("once"))
	if err != nil {
		t.Fatal(err)
	}
	res.Body.Close()
	expect(t, res.StatusCode, http.StatusAccepted)
	expect(t, len(calls), 1)
}

func TestAForwardStopsWaitingAfterDialWait(t *testing.T) {
	was := dialWait
	dialWait = 300 * time.Millisecond
	t.Cleanup(func() { dialWait = was })
	closed, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	address := closed.Addr().String()
	closed.Close()
	next, err := newUpstream("http://"+address, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(next)
	t.Cleanup(server.Close)
	started := time.Now()
	res, err := http.Get(server.URL + "/api/v1/things")
	if err != nil {
		t.Fatal(err)
	}
	res.Body.Close()
	expect(t, res.StatusCode, http.StatusBadGateway)
	if d := time.Since(started); d < 300*time.Millisecond || d > 5*time.Second {
		t.Fatalf("answered after %v", d)
	}
}

// freeAddress is a TCP address nothing listens on.
func freeAddress(t *testing.T) string {
	t.Helper()
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	address := l.Addr().String()
	l.Close()
	return address
}

func TestAForwardStopsWaitingWhenItsClientGoesAway(t *testing.T) {
	address := freeAddress(t)
	wait := &upstreamWait{}
	dials := atomic.Int32{}
	dial := wait.dial(func(ctx context.Context, network, address string) (net.Conn, error) {
		dials.Add(1)
		return (&net.Dialer{}).DialContext(ctx, network, address)
	})
	gone := make(chan struct{})
	ctx := context.WithValue(context.Background(), requestDone{}, (<-chan struct{})(gone))
	result := make(chan error, 1)
	go func() {
		_, err := dial(ctx, "tcp", address)
		result <- err
	}()
	time.Sleep(600 * time.Millisecond)
	close(gone)
	select {
	case err := <-result:
		if err == nil {
			t.Fatal("a dial to nothing succeeded")
		}
	case <-time.After(2 * time.Second):
		t.Fatal("the dial went on after its request ended")
	}
	if n := dials.Load(); n < 2 || n > 5 {
		t.Fatalf("%d dials", n)
	}
}

func TestARefusalAfterPythonHasAnsweredFailsAtOnce(t *testing.T) {
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	address := l.Addr().String()
	go func() {
		for {
			c, err := l.Accept()
			if err != nil {
				return
			}
			c.Close()
		}
	}()
	wait := &upstreamWait{}
	dial := wait.dial((&net.Dialer{}).DialContext)
	conn, err := dial(context.Background(), "tcp", address)
	if err != nil {
		t.Fatal(err)
	}
	conn.Close()
	l.Close()
	started := time.Now()
	if _, err := dial(context.Background(), "tcp", address); err == nil {
		t.Fatal("a dial to a closed port succeeded")
	}
	if d := time.Since(started); d > 200*time.Millisecond {
		t.Fatalf("a refusal after Python answered waited %v", d)
	}
}
