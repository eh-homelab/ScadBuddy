package telemetry

import (
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
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/trace"
)

// Call names a ScadBuddy API request: Op is the span's suffix (scadbuddy.<Op>) and
// the metric's scadbuddy.operation, Route the route template, never the URL. Transfer
// is "download" or "upload" for a request that moves file contents, counted as such.
type Call struct {
	Op       string
	Route    string
	Transfer string
	Attrs    []attribute.KeyValue
}

// The transfer directions.
const (
	Download = "download"
	Upload   = "upload"
)

// OperationID is a polled operation's id.
func OperationID(id string) attribute.KeyValue {
	return attribute.String("scadbuddy.operation.id", id)
}

type callKey struct{}

// WithCall names the requests sent with ctx.
func WithCall(ctx context.Context, c Call) context.Context {
	return context.WithValue(ctx, callKey{}, c)
}

// Transport wraps base so each request is a CLIENT span named by its Call, carries
// traceparent for the backend to continue, and is measured. The span ends when the
// answer's body is closed, so a download's span covers its bytes.
func Transport(base http.RoundTripper) http.RoundTripper {
	if base == nil {
		base = http.DefaultTransport
	}
	return transport{base: base}
}

type transport struct{ base http.RoundTripper }

func (t transport) RoundTrip(req *http.Request) (*http.Response, error) {
	r := rec()
	call, ok := req.Context().Value(callKey{}).(Call)
	if !ok {
		call = Call{Op: "unknown", Route: "unknown"}
	}
	attrs := append([]attribute.KeyValue{
		attribute.String("http.request.method", req.Method),
		attribute.String("url.template", call.Route),
	}, call.Attrs...)
	ctx, span := r.tracer.Start(req.Context(), "scadbuddy."+call.Op, trace.WithSpanKind(trace.SpanKindClient), trace.WithAttributes(attrs...))
	req = req.Clone(ctx)
	r.propagator.Inject(ctx, propagation.HeaderCarrier(req.Header))
	m := &measured{r: r, span: span, call: call, method: req.Method, start: time.Now()}
	if req.Body != nil && call.Transfer == Upload {
		req.Body = &counted{ReadCloser: req.Body, n: &m.sent}
	}
	resp, err := t.base.RoundTrip(req)
	if err != nil {
		m.finish(0, err)
		return nil, err
	}
	resp.Body = &answer{ReadCloser: resp.Body, m: m, status: resp.StatusCode}
	return resp, nil
}

// measured is one request's span and measurements, finished once.
type measured struct {
	r          *recorder
	span       trace.Span
	call       Call
	method     string
	start      time.Time
	sent, read atomic.Int64
	once       sync.Once
}

func (m *measured) finish(status int, err error) {
	m.once.Do(func() {
		set := []attribute.KeyValue{
			attribute.String("scadbuddy.operation", m.call.Op),
			attribute.String("http.request.method", m.method),
		}
		if status != 0 {
			set = append(set, attribute.Int("http.response.status_code", status))
			if status >= 400 {
				m.span.SetStatus(codes.Error, strconv.Itoa(status/100)+"xx")
				set = append(set, errorTypeKey.String(strconv.Itoa(status)))
			}
		}
		if err != nil {
			kind := errorKind(err)
			set = append(set, errorTypeKey.String(kind))
			m.span.SetStatus(codes.Error, kind)
		}
		m.span.SetAttributes(set[2:]...)
		ctx := context.Background()
		var moved int64
		switch m.call.Transfer {
		case Upload:
			// The whole multipart body; the file's own bytes are counted by Uploaded.
			m.span.SetAttributes(attribute.Int64("http.request.body.size", m.sent.Load()))
		case Download:
			moved = m.read.Load()
			m.span.SetAttributes(attribute.Int64("http.response.body.size", moved))
		}
		if moved > 0 {
			m.r.transfer.Add(ctx, moved, metric.WithAttributes(attribute.String("direction", Download)))
		}
		m.r.httpDuration.Record(ctx, time.Since(m.start).Seconds(), metric.WithAttributes(set...))
		m.span.End()
	})
}

// errorKind is a fixed description of a request that got no answer; the error's own
// text, which names the host, is never recorded.
func errorKind(err error) string {
	var ne net.Error
	switch {
	case errors.Is(err, context.Canceled):
		return "canceled"
	case errors.Is(err, context.DeadlineExceeded), errors.As(err, &ne) && ne.Timeout():
		return "timeout"
	}
	return "transport"
}

// Uploaded counts n bytes of file contents sent in an upload's form.
func Uploaded(n int64) {
	if n > 0 {
		rec().transfer.Add(context.Background(), n, metric.WithAttributes(attribute.String("direction", Upload)))
	}
}

// counted counts the bytes read from a request body.
type counted struct {
	io.ReadCloser
	n *atomic.Int64
}

func (c *counted) Read(p []byte) (int, error) {
	n, err := c.ReadCloser.Read(p)
	c.n.Add(int64(n))
	return n, err
}

// answer ends its request's span when it is closed.
type answer struct {
	io.ReadCloser
	m      *measured
	status int
	failed error
}

func (a *answer) Read(p []byte) (int, error) {
	n, err := a.ReadCloser.Read(p)
	a.m.read.Add(int64(n))
	if err != nil && !errors.Is(err, io.EOF) {
		a.failed = err
	}
	return n, err
}

func (a *answer) Close() error {
	err := a.ReadCloser.Close()
	a.m.finish(a.status, a.failed)
	return err
}
