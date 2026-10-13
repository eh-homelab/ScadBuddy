package telemetry

import (
	"context"
	"strconv"
	"sync/atomic"
	"syscall"
	"time"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/metric"
	metricnoop "go.opentelemetry.io/otel/metric/noop"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/trace"
	tracenoop "go.opentelemetry.io/otel/trace/noop"
)

// Attributes are only ids, byte counts, operation names, errnos and status codes,
// never a name, a path, contents or anything the library returns as text.
const (
	fileKey      = attribute.Key("bambuddy_fs.file.id")
	folderKey    = attribute.Key("bambuddy_fs.folder.id")
	toFolderKey  = attribute.Key("bambuddy_fs.to_folder.id")
	bytesKey     = attribute.Key("bambuddy_fs.bytes")
	opKey        = attribute.Key("op")
	resultKey    = attribute.Key("result")
	errorTypeKey = attribute.Key("error.type")
)

// FileID, FolderID, ToFolderID (a move's destination) and Bytes are what a span may
// say about its operation.
func FileID(id int) attribute.KeyValue     { return fileKey.Int(id) }
func FolderID(id int) attribute.KeyValue   { return folderKey.Int(id) }
func ToFolderID(id int) attribute.KeyValue { return toFolderKey.Int(id) }
func Bytes(n int64) attribute.KeyValue     { return bytesKey.Int64(n) }

const scope = "github.com/eh-homelab/ScadBuddy/tools/go/bambuddy-fs"

// Gauges are the mount's state the observable gauges read at each collection.
type Gauges struct {
	CacheBytes    func() int64
	UnsentEntries func() int64
	UnsentBytes   func() int64
}

var gauges atomic.Pointer[Gauges]

// Observe makes g what the gauges report.
func Observe(g Gauges) { gauges.Store(&g) }

// recorder is what the mount records to: the no-op until Start or Use installs one.
type recorder struct {
	tracer     trace.Tracer
	propagator propagation.TextMapPropagator

	fuseOps       metric.Int64Counter
	fuseDuration  metric.Float64Histogram
	httpDuration  metric.Float64Histogram
	transfer      metric.Int64Counter
	unsentUploads metric.Int64Counter
}

var current atomic.Pointer[recorder]

func init() { install(nil, nil) }

func rec() *recorder { return current.Load() }

// Use records to tp and mp (tests: a span recorder and a manual reader) until the
// returned function restores what was there.
func Use(tp trace.TracerProvider, mp metric.MeterProvider) (restore func()) {
	return install(tp, mp)
}

// durationBuckets are the HTTP semantic conventions' advice for seconds.
var durationBuckets = []float64{0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 0.75, 1, 2.5, 5, 7.5, 10}

func install(tp trace.TracerProvider, mp metric.MeterProvider) (restore func()) {
	if tp == nil {
		tp = tracenoop.NewTracerProvider()
	}
	if mp == nil {
		mp = metricnoop.NewMeterProvider()
	}
	m := mp.Meter(scope)
	r := &recorder{tracer: tp.Tracer(scope), propagator: propagation.TraceContext{}}
	// The instruments' errors are only for invalid names and units, fixed here; a
	// failed one is a no-op.
	r.fuseOps, _ = m.Int64Counter("bambuddy_fs.fuse.operations", metric.WithUnit("{operation}"),
		metric.WithDescription("FUSE operations that reached the library or did work, by op and result"))
	r.fuseDuration, _ = m.Float64Histogram("bambuddy_fs.fuse.operation.duration", metric.WithUnit("s"),
		metric.WithDescription("Duration of those FUSE operations"), metric.WithExplicitBucketBoundaries(durationBuckets...))
	r.httpDuration, _ = m.Float64Histogram("http.client.request.duration", metric.WithUnit("s"),
		metric.WithDescription("Duration of ScadBuddy API requests, until the answer's body is closed"),
		metric.WithExplicitBucketBoundaries(durationBuckets...))
	r.transfer, _ = m.Int64Counter("bambuddy_fs.transfer.bytes", metric.WithUnit("By"),
		metric.WithDescription("File contents downloaded and uploaded"))
	r.unsentUploads, _ = m.Int64Counter("bambuddy_fs.unsent.uploads", metric.WithUnit("{upload}"),
		metric.WithDescription("Attempts to send an unsent write, by result"))
	cache, _ := m.Int64ObservableGauge("bambuddy_fs.cache.bytes", metric.WithUnit("By"),
		metric.WithDescription("Bytes in the cache directory"))
	entries, _ := m.Int64ObservableGauge("bambuddy_fs.unsent.entries", metric.WithUnit("{write}"),
		metric.WithDescription("Writes queued to be sent again"))
	queued, _ := m.Int64ObservableGauge("bambuddy_fs.unsent.bytes", metric.WithUnit("By"),
		metric.WithDescription("Bytes of the writes queued to be sent again"))
	reg, _ := m.RegisterCallback(func(_ context.Context, o metric.Observer) error {
		g := gauges.Load()
		if g == nil {
			return nil
		}
		o.ObserveInt64(cache, g.CacheBytes())
		o.ObserveInt64(entries, g.UnsentEntries())
		o.ObserveInt64(queued, g.UnsentBytes())
		return nil
	}, cache, entries, queued)
	prev := current.Swap(r)
	return func() {
		if reg != nil {
			_ = reg.Unregister()
		}
		current.Store(prev)
	}
}

// Op is a FUSE operation's span and measurements. A nil *Op records nothing, so a
// handler that only sometimes does work can End one unconditionally.
type Op struct {
	span  trace.Span
	name  string
	start time.Time
}

// StartOp starts the span fuse.<name>.
func StartOp(ctx context.Context, name string, attrs ...attribute.KeyValue) (context.Context, *Op) {
	ctx, span := rec().tracer.Start(ctx, "fuse."+name, trace.WithSpanKind(trace.SpanKindInternal), trace.WithAttributes(attrs...))
	return ctx, &Op{span: span, name: name, start: time.Now()}
}

// Set adds attributes learned during the operation.
func (o *Op) Set(attrs ...attribute.KeyValue) {
	if o != nil {
		o.span.SetAttributes(attrs...)
	}
}

// End ends the span with the operation's answer and records it.
func (o *Op) End(errno syscall.Errno) {
	if o == nil {
		return
	}
	r := rec()
	result := "ok"
	if errno != 0 {
		result = ErrnoName(errno)
	}
	// A lookup of a name that is not there is an answer, not a failure: every
	// create looks its name up first.
	if errno != 0 && (o.name != "lookup" || errno != syscall.ENOENT) {
		o.span.SetAttributes(errorTypeKey.String(result))
		o.span.SetStatus(codes.Error, result)
	}
	o.span.End()
	set := metric.WithAttributes(opKey.String(o.name), resultKey.String(result))
	ctx := context.Background()
	r.fuseOps.Add(ctx, 1, set)
	r.fuseDuration.Record(ctx, time.Since(o.start).Seconds(), set)
}

// Retry is one attempt to send an unsent write.
type Retry struct{ span trace.Span }

// StartRetry starts the span unsent.retry for the write's attempt'th attempt.
func StartRetry(ctx context.Context, attempt int) (context.Context, *Retry) {
	ctx, span := rec().tracer.Start(ctx, "unsent.retry", trace.WithSpanKind(trace.SpanKindInternal),
		trace.WithAttributes(attribute.Int("bambuddy_fs.unsent.attempt", attempt)))
	return ctx, &Retry{span: span}
}

// Unsent attempts' results.
const (
	RetrySent    = "ok"
	RetryFailed  = "failed"
	RetryRefused = "refused"
)

// End ends the attempt with its result, one of the Retry constants, and the file it
// was sent as (0 if it was not).
func (r *Retry) End(result string, fileID int) {
	r.span.SetAttributes(attribute.String("bambuddy_fs.unsent.result", result))
	if fileID != 0 {
		r.span.SetAttributes(FileID(fileID))
	}
	if result != RetrySent {
		r.span.SetStatus(codes.Error, result)
	}
	r.span.End()
	rec().unsentUploads.Add(context.Background(), 1, metric.WithAttributes(resultKey.String(result)))
}

var errnoNames = map[syscall.Errno]string{
	syscall.ENOENT: "ENOENT", syscall.EIO: "EIO", syscall.EEXIST: "EEXIST", syscall.EBUSY: "EBUSY",
	syscall.EFBIG: "EFBIG", syscall.EACCES: "EACCES", syscall.EROFS: "EROFS", syscall.ENOTSUP: "ENOTSUP",
	syscall.EISDIR: "EISDIR", syscall.ENOTDIR: "ENOTDIR", syscall.ENODATA: "ENODATA", syscall.EINTR: "EINTR",
	syscall.ENOTEMPTY: "ENOTEMPTY", syscall.EPERM: "EPERM", syscall.EINVAL: "EINVAL", syscall.ENOSPC: "ENOSPC",
}

// ErrnoName is an errno's symbolic name, like ENOENT.
func ErrnoName(errno syscall.Errno) string {
	if name, ok := errnoNames[errno]; ok {
		return name
	}
	return "errno_" + strconv.Itoa(int(errno))
}
