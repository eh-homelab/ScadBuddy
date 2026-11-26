package telemetry

import (
	"bufio"
	"context"
	"errors"
	"io"
	"net"
	"net/http"
	"strconv"
	"sync"
	"sync/atomic"
	"time"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/metric"
	metricnoop "go.opentelemetry.io/otel/metric/noop"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/trace"
	tracenoop "go.opentelemetry.io/otel/trace/noop"
)

// Scope is the instrumentation scope of every span and instrument this package makes.
const Scope = "github.com/eh-homelab/ScadBuddy/tools/go/telemetry"

// Spans carry both the attribute names the API's FastAPI spans carry (http.method,
// http.status_code, which the dashboards query) and the stable ones the agent's carry;
// metrics carry only the stable ones, as the conventions name them.
const (
	methodKey       = attribute.Key("http.method")
	requestMethod   = attribute.Key("http.request.method")
	statusKey       = attribute.Key("http.status_code")
	responseStatus  = attribute.Key("http.response.status_code")
	routeKey        = attribute.Key("http.route")
	schemeKey       = attribute.Key("http.scheme")
	urlScheme       = attribute.Key("url.scheme")
	serverAddress   = attribute.Key("server.address")
	errorTypeKey    = attribute.Key("error.type")
	failureClassKey = attribute.Key("scadbuddy.failure_class")
	reasonKey       = attribute.Key("scadbuddy.problem.reason")
)

// durationBuckets are the HTTP conventions' advice for seconds.
var durationBuckets = []float64{0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 0.75, 1, 2.5, 5, 7.5, 10}

// methods are the methods a span names; any other is "_OTHER", as the conventions say.
var methods = map[string]bool{
	"GET": true, "HEAD": true, "POST": true, "PUT": true, "PATCH": true, "DELETE": true,
	"OPTIONS": true, "CONNECT": true, "TRACE": true,
}

func method(m string) string {
	if methods[m] {
		return m
	}
	return "_OTHER"
}

func scheme(r *http.Request) string {
	if r.TLS != nil {
		return "https"
	}
	return "http"
}

// providers are t's tracer and meter, each the no-op when its signal is off, and whether
// either is on.
func (t *Telemetry) providers() (trace.TracerProvider, metric.MeterProvider, bool) {
	tp, mp := t.Tracer(), t.Meter()
	on := tp != nil || mp != nil
	if tp == nil {
		tp = tracenoop.NewTracerProvider()
	}
	if mp == nil {
		mp = metricnoop.NewMeterProvider()
	}
	return tp, mp, on
}

// sizes are the three instruments of one side of HTTP.
type sizes struct {
	duration          metric.Float64Histogram
	request, response metric.Int64Histogram
}

func newSizes(m metric.Meter, side string) sizes {
	var s sizes
	// An instrument's error is only for an invalid name or unit, fixed here; a failed one
	// is a no-op.
	s.duration, _ = m.Float64Histogram("http."+side+".request.duration", metric.WithUnit("s"),
		metric.WithDescription("Duration of HTTP "+side+" requests."), metric.WithExplicitBucketBoundaries(durationBuckets...))
	s.request, _ = m.Int64Histogram("http."+side+".request.body.size", metric.WithUnit("By"),
		metric.WithDescription("Size of HTTP "+side+" request bodies."))
	s.response, _ = m.Int64Histogram("http."+side+".response.body.size", metric.WithUnit("By"),
		metric.WithDescription("Size of HTTP "+side+" response bodies."))
	return s
}

func (s sizes) record(start time.Time, sent, read int64, attrs []attribute.KeyValue) {
	ctx, set := context.Background(), metric.WithAttributes(attrs...)
	s.duration.Record(ctx, time.Since(start).Seconds(), set)
	s.request.Record(ctx, sent, set)
	s.response.Record(ctx, read, set)
}

type serverKey struct{}

// server is a request's server span and route, which SetRoute names.
type server struct {
	span   trace.Span
	method string
	route  atomic.Pointer[string]
}

// Server wraps next so each request is a SERVER span, a child of the traceparent it
// carries, and is measured (http.server.request.duration and the body sizes). The span is
// named by its method until SetRoute names its route, as the API's FastAPI spans are
// ("GET /api/v1/models"). A path in skip (/healthz) is neither traced nor measured. With
// traces and metrics off it is next itself.
func (t *Telemetry) Server(next http.Handler, skip ...string) http.Handler {
	tp, mp, on := t.providers()
	if !on {
		return next
	}
	tracer := tp.Tracer(Scope)
	measure := newSizes(mp.Meter(Scope), "server")
	skipped := map[string]bool{}
	for _, p := range skip {
		skipped[p] = true
	}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if skipped[r.URL.Path] {
			next.ServeHTTP(w, r)
			return
		}
		start := time.Now()
		m, sch := method(r.Method), scheme(r)
		ctx := propagator.Extract(r.Context(), propagation.HeaderCarrier(r.Header))
		ctx, span := tracer.Start(ctx, m, trace.WithSpanKind(trace.SpanKindServer), trace.WithAttributes(
			methodKey.String(m), requestMethod.String(m), schemeKey.String(sch), urlScheme.String(sch),
		))
		s := &server{span: span, method: m}
		ctx = context.WithValue(ctx, serverKey{}, s)
		rec := &recorder{ResponseWriter: w}
		var sent atomic.Int64
		r = r.WithContext(ctx)
		if r.Body != nil && r.Body != http.NoBody {
			r.Body = &counted{ReadCloser: r.Body, n: &sent}
		}
		defer func() {
			status := rec.status
			attrs := []attribute.KeyValue{requestMethod.String(m), urlScheme.String(sch)}
			if route := s.route.Load(); route != nil {
				attrs = append(attrs, routeKey.String(*route))
			}
			if p := recover(); p != nil {
				// net/http answers a panic by closing the connection; the client sees no status.
				// http.ErrAbortHandler is a deliberate abort: ReverseProxy panics with it when
				// the client leaves mid-stream (an SSE or progress answer). That is a client
				// cancel, not an error.
				if p != http.ErrAbortHandler {
					span.SetStatus(codes.Error, "panic")
					span.SetAttributes(errorTypeKey.String("panic"))
					attrs = append(attrs, errorTypeKey.String("panic"))
				}
				span.End()
				measure.record(start, max(r.ContentLength, sent.Load()), rec.written, attrs)
				panic(p)
			}
			if status == 0 {
				status = http.StatusOK
			}
			span.SetAttributes(statusKey.Int(status), responseStatus.Int(status))
			attrs = append(attrs, responseStatus.Int(status))
			if status >= 500 {
				span.SetStatus(codes.Error, "")
				span.SetAttributes(errorTypeKey.String(strconv.Itoa(status)))
				attrs = append(attrs, errorTypeKey.String(strconv.Itoa(status)))
			}
			span.End()
			measure.record(start, max(r.ContentLength, sent.Load()), rec.written, attrs)
		}()
		next.ServeHTTP(rec, r)
	})
}

// SetRoute names the request's server span by its route template, which the span and the
// request's metrics record as http.route. It does nothing outside a measured request.
func SetRoute(ctx context.Context, route string) {
	if s, ok := ctx.Value(serverKey{}).(*server); ok {
		s.route.Store(&route)
		s.span.SetName(s.method + " " + route)
		s.span.SetAttributes(routeKey.String(route))
	}
}

// RecordProblem records a problem answer on the current span: scadbuddy.failure_class
// as core/tracing.py words it ("http-<status>" for an about:blank problem, else its
// type) and the problem's reason. Never its detail, which can carry values.
func RecordProblem(ctx context.Context, status int, kind, reason string) {
	span := trace.SpanFromContext(ctx)
	if !span.IsRecording() {
		return
	}
	class := kind
	if class == "" || class == "about:blank" {
		class = "http-" + strconv.Itoa(status)
	}
	span.SetAttributes(failureClassKey.String(class))
	if reason != "" {
		span.SetAttributes(reasonKey.String(reason))
	}
}

// recorder notes the status a handler answers with and the bytes it writes. Unwrap lets
// http.ResponseController reach what it does not name, and Flush and Hijack pass through,
// which the forward's SSE and WebSockets use.
type recorder struct {
	http.ResponseWriter
	status  int
	written int64
}

func (r *recorder) WriteHeader(code int) {
	if r.status == 0 || r.status < 200 && code >= 200 {
		r.status = code
	}
	r.ResponseWriter.WriteHeader(code)
}

func (r *recorder) Write(b []byte) (int, error) {
	if r.status == 0 {
		r.status = http.StatusOK
	}
	n, err := r.ResponseWriter.Write(b)
	r.written += int64(n)
	return n, err
}

func (r *recorder) Flush() {
	if r.status == 0 {
		r.status = http.StatusOK
	}
	_ = http.NewResponseController(r.ResponseWriter).Flush()
}

func (r *recorder) Hijack() (net.Conn, *bufio.ReadWriter, error) {
	if r.status == 0 {
		r.status = http.StatusSwitchingProtocols
	}
	return http.NewResponseController(r.ResponseWriter).Hijack()
}

func (r *recorder) Unwrap() http.ResponseWriter { return r.ResponseWriter }

// counted counts the bytes read from a body.
type counted struct {
	io.ReadCloser
	n *atomic.Int64
}

func (c *counted) Read(p []byte) (int, error) {
	n, err := c.ReadCloser.Read(p)
	c.n.Add(int64(n))
	return n, err
}

// Transport wraps base so each request is a CLIENT span to peer (server.address), a child
// of the request's context, and is measured (http.client.request.duration and the body
// sizes). It carries the span's traceparent: the service it reaches continues the trace
// under it. The span ends when the answer's body is closed, so a streamed answer's span
// covers the stream. With traces and metrics off it is base itself.
func (t *Telemetry) Transport(base http.RoundTripper, peer string) http.RoundTripper {
	tp, mp, on := t.providers()
	if !on {
		return base
	}
	if base == nil {
		base = http.DefaultTransport
	}
	return &transport{base: base, tracer: tp.Tracer(Scope), measure: newSizes(mp.Meter(Scope), "client"), peer: peer}
}

type transport struct {
	base    http.RoundTripper
	tracer  trace.Tracer
	measure sizes
	peer    string
}

func (t *transport) RoundTrip(req *http.Request) (*http.Response, error) {
	m := method(req.Method)
	ctx, span := t.tracer.Start(req.Context(), m, trace.WithSpanKind(trace.SpanKindClient), trace.WithAttributes(
		methodKey.String(m), requestMethod.String(m), serverAddress.String(t.peer),
		urlScheme.String(req.URL.Scheme),
	))
	req = req.Clone(ctx)
	propagator.Inject(ctx, propagation.HeaderCarrier(req.Header))
	e := &ender{span: span, measure: t.measure, start: time.Now(),
		attrs: []attribute.KeyValue{requestMethod.String(m), serverAddress.String(t.peer)}, length: req.ContentLength}
	if req.Body != nil && req.Body != http.NoBody {
		req.Body = &counted{ReadCloser: req.Body, n: &e.sent}
	}
	res, err := t.base.RoundTrip(req)
	if err != nil {
		e.read(err)
		e.end()
		return nil, err
	}
	e.status = res.StatusCode
	span.SetAttributes(statusKey.Int(res.StatusCode), responseStatus.Int(res.StatusCode))
	if rw, ok := res.Body.(io.ReadWriteCloser); ok {
		// A 101's body is the tunnel, which the reverse proxy writes to as well.
		res.Body = &tunnel{ReadWriteCloser: rw, ender: e}
	} else {
		res.Body = &body{ReadCloser: res.Body, ender: e}
	}
	return res, nil
}

// ender ends a client span and records its measurements once, at the close of its answer.
type ender struct {
	span    trace.Span
	measure sizes
	start   time.Time
	attrs   []attribute.KeyValue
	status  int
	length  int64 // the request's Content-Length, -1 when unknown
	sent    atomic.Int64
	got     atomic.Int64

	mu     sync.Mutex
	failed error
	done   bool
}

func (e *ender) read(err error) {
	if err == nil || errors.Is(err, io.EOF) {
		return
	}
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.failed == nil {
		e.failed = err
	}
}

func (e *ender) end() {
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.done {
		return
	}
	e.done = true
	attrs := e.attrs
	if e.status != 0 {
		attrs = append(attrs, responseStatus.Int(e.status))
	}
	switch {
	case e.failed != nil:
		kind := ErrorKind(e.failed)
		e.span.SetAttributes(errorTypeKey.String(kind))
		e.span.SetStatus(codes.Error, kind)
		attrs = append(attrs, errorTypeKey.String(kind))
	case e.status >= 500:
		e.span.SetStatus(codes.Error, "")
		e.span.SetAttributes(errorTypeKey.String(strconv.Itoa(e.status)))
		attrs = append(attrs, errorTypeKey.String(strconv.Itoa(e.status)))
	}
	e.span.End()
	e.measure.record(e.start, max(e.length, e.sent.Load()), e.got.Load(), attrs)
}

type body struct {
	io.ReadCloser
	ender *ender
}

func (b *body) Read(p []byte) (int, error) {
	n, err := b.ReadCloser.Read(p)
	b.ender.got.Add(int64(n))
	b.ender.read(err)
	return n, err
}

func (b *body) Close() error {
	err := b.ReadCloser.Close()
	b.ender.end()
	return err
}

type tunnel struct {
	io.ReadWriteCloser
	ender *ender
}

func (b *tunnel) Close() error {
	err := b.ReadWriteCloser.Close()
	b.ender.end()
	return err
}

// ErrorKind is a fixed description of a request that got no answer; the error's own text,
// which names the host, is never recorded.
func ErrorKind(err error) string {
	var ne net.Error
	switch {
	case errors.Is(err, context.Canceled):
		return "canceled"
	case errors.Is(err, context.DeadlineExceeded), errors.As(err, &ne) && ne.Timeout():
		return "timeout"
	}
	return "transport"
}
