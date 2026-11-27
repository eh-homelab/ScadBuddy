package telemetry

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
)

func recording(t *testing.T) (*Telemetry, *tracetest.InMemoryExporter) {
	t.Helper()
	exp := tracetest.NewInMemoryExporter()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSyncer(exp))
	t.Cleanup(func() { _ = tp.Shutdown(t.Context()) })
	return &Telemetry{Traces: tp}, exp
}

func attr(s tracetest.SpanStub, key string) attribute.Value {
	for _, kv := range s.Attributes {
		if string(kv.Key) == key {
			return kv.Value
		}
	}
	return attribute.Value{}
}

func TestOffWrapsNothing(t *testing.T) {
	var off *Telemetry
	h := http.NewServeMux()
	if got := off.Server(h); got != http.Handler(h) {
		t.Fatal("the handler was wrapped")
	}
	if got := (&Telemetry{}).Transport(http.DefaultTransport, "x"); got != http.DefaultTransport {
		t.Fatal("the transport was wrapped")
	}
}

func TestAServerSpanIsNamedByItsRouteAndContinuesTheCallersTrace(t *testing.T) {
	tel, exp := recording(t)
	h := tel.Server(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		SetRoute(r.Context(), "/api/v1/things/{thing_id}")
		RecordProblem(r.Context(), 503, "about:blank", "database-unavailable")
		w.WriteHeader(http.StatusServiceUnavailable)
	}), "/healthz")
	req := httptest.NewRequest("GET", "/api/v1/things/42?secret=1", nil)
	req.Header.Set("traceparent", "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01")
	h.ServeHTTP(httptest.NewRecorder(), req)
	h.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest("GET", "/healthz", nil))

	spans := exp.GetSpans()
	if len(spans) != 1 {
		t.Fatalf("%d spans", len(spans))
	}
	s := spans[0]
	if s.Name != "GET /api/v1/things/{thing_id}" || s.SpanKind != trace.SpanKindServer {
		t.Fatalf("span %q kind %v", s.Name, s.SpanKind)
	}
	if s.Parent.TraceID().String() != "0af7651916cd43dd8448eb211c80319c" || s.Parent.SpanID().String() != "b7ad6b7169203331" {
		t.Fatalf("parent %v", s.Parent)
	}
	for key, want := range map[string]any{
		"http.route": "/api/v1/things/{thing_id}", "http.method": "GET", "http.request.method": "GET",
		"http.status_code": int64(503), "http.response.status_code": int64(503),
		"scadbuddy.failure_class": "http-503", "scadbuddy.problem.reason": "database-unavailable",
	} {
		if got := attr(s, key).AsInterface(); got != want {
			t.Errorf("%s = %v, want %v", key, got, want)
		}
	}
	if s.Status.Code != codes.Error {
		t.Errorf("status %v", s.Status)
	}
	for _, kv := range s.Attributes {
		if strings.Contains(kv.Value.String(), "secret") || strings.Contains(kv.Value.String(), "42") {
			t.Errorf("%s holds the path or query: %s", kv.Key, kv.Value.String())
		}
	}
}

func TestAClientSpanCarriesItsTraceparentAndEndsWithTheBody(t *testing.T) {
	tel, exp := recording(t)
	var got string
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		got = r.Header.Get("traceparent")
		_, _ = io.WriteString(w, "body")
	}))
	defer upstream.Close()
	client := &http.Client{Transport: tel.Transport(http.DefaultTransport, "python")}
	ctx, parent := tel.Traces.Tracer("t").Start(t.Context(), "parent")
	req, _ := http.NewRequestWithContext(ctx, "POST", upstream.URL, nil)
	res, err := client.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	if len(exp.GetSpans()) != 0 {
		t.Fatal("the span ended before the body was read")
	}
	_, _ = io.ReadAll(res.Body)
	_ = res.Body.Close()
	parent.End()

	spans := exp.GetSpans()
	if len(spans) != 2 {
		t.Fatalf("%d spans", len(spans))
	}
	c := spans[0]
	if c.Name != "POST" || c.SpanKind != trace.SpanKindClient || c.Parent.SpanID() != parent.SpanContext().SpanID() {
		t.Fatalf("span %q kind %v parent %v", c.Name, c.SpanKind, c.Parent)
	}
	want := "00-" + c.SpanContext.TraceID().String() + "-" + c.SpanContext.SpanID().String() + "-01"
	if got != want {
		t.Fatalf("traceparent %q, want %q", got, want)
	}
	if attr(c, "server.address").AsString() != "python" || attr(c, "http.status_code").AsInt64() != 200 {
		t.Fatalf("attributes %v", c.Attributes)
	}
}

func TestAClientThatGetsNoAnswerIsAnError(t *testing.T) {
	tel, exp := recording(t)
	client := &http.Client{Transport: tel.Transport(http.DefaultTransport, "python")}
	ctx, parent := tel.Traces.Tracer("t").Start(t.Context(), "parent")
	defer parent.End()
	closed := httptest.NewServer(http.NotFoundHandler())
	closed.Close()
	req, _ := http.NewRequestWithContext(ctx, "GET", closed.URL, nil)
	if _, err := client.Do(req); err == nil {
		t.Fatal("no error")
	}
	s := exp.GetSpans()[0]
	if s.Status.Code != codes.Error || attr(s, "error.type").AsString() != "transport" {
		t.Fatalf("status %v attributes %v", s.Status, s.Attributes)
	}
}

func TestAnAbortedHandlerIsAClientCancelNotAnError(t *testing.T) {
	wrapped := fmt.Errorf("the client left: %w", http.ErrAbortHandler)
	for _, abort := range []error{http.ErrAbortHandler, wrapped} {
		tel, exp := recording(t)
		reader := sdkmetric.NewManualReader()
		mp := sdkmetric.NewMeterProvider(sdkmetric.WithReader(reader))
		t.Cleanup(func() { _ = mp.Shutdown(context.Background()) })
		tel.Meters = mp
		h := tel.Server(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			SetRoute(r.Context(), "/api/v1/progress")
			w.WriteHeader(http.StatusOK)
			panic(abort)
		}))
		var p any
		func() {
			defer func() { p = recover() }()
			h.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest("GET", "/api/v1/progress", nil))
		}()
		if p != abort {
			t.Fatalf("recovered %v, want %v again, so net/http aborts the connection", p, abort)
		}
		spans := exp.GetSpans()
		if len(spans) != 1 {
			t.Fatalf("%d spans", len(spans))
		}
		if spans[0].Status.Code == codes.Error {
			t.Errorf("%v: status %v", abort, spans[0].Status)
		}
		if got := attr(spans[0], "error.type"); got.Type() != attribute.EMPTY {
			t.Errorf("%v: error.type = %v", abort, got.String())
		}
		d, ok := collect(t, reader)["http.server.request.duration"].Data.(metricdata.Histogram[float64])
		if !ok || len(d.DataPoints) != 1 {
			t.Fatalf("%v: duration %+v", abort, d)
		}
		if v, ok := d.DataPoints[0].Attributes.Value("error.type"); ok {
			t.Errorf("%v: the duration carries error.type=%s", abort, v.String())
		}
	}
}

func TestAStreamEndsItsSpanWhenItStarts(t *testing.T) {
	tel, exp := recording(t)
	started, finish := make(chan struct{}), make(chan struct{})
	h := tel.Server(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		SetRoute(r.Context(), "/api/v1/stream")
		_, _ = w.Write([]byte("ready\n"))
		Streaming(r.Context())
		Streaming(r.Context())
		close(started)
		<-finish
		_, _ = w.Write([]byte("later\n"))
	}))
	done := make(chan struct{})
	go func() {
		defer close(done)
		h.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest("GET", "/api/v1/stream", nil))
	}()
	<-started
	spans := exp.GetSpans()
	if len(spans) != 1 || spans[0].Name != "GET /api/v1/stream" || attr(spans[0], "http.status_code").AsInt64() != 200 {
		t.Fatalf("spans while the stream is open: %v", spans)
	}
	close(finish)
	<-done
	if n := len(exp.GetSpans()); n != 1 {
		t.Fatalf("%d spans after the stream ended", n)
	}
}

func TestAnyOtherPanicIsAnError(t *testing.T) {
	tel, exp := recording(t)
	h := tel.Server(http.HandlerFunc(func(http.ResponseWriter, *http.Request) { panic("boom") }))
	func() {
		defer func() { _ = recover() }()
		h.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest("GET", "/", nil))
	}()
	s := exp.GetSpans()[0]
	if s.Status.Code != codes.Error || attr(s, "error.type").AsString() != "panic" {
		t.Fatalf("status %v error.type %q", s.Status, attr(s, "error.type").AsString())
	}
}
