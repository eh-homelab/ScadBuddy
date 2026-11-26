// Package pgxtrace makes a span of every pgx query, batch, COPY and traced pool acquire,
// as the API's psycopg instrumentation does: db.system postgresql and the statement's text
// with its values bound apart (never the values), named by a short summary
// ("SELECT ai_sessions"). It measures each one (db.client.operation.duration) and the
// pool's state (ObservePool). A failed statement records its SQLSTATE, never the server's
// message, which can quote a value.
package pgxtrace

import (
	"context"
	"errors"
	"regexp"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/metric"
	metricnoop "go.opentelemetry.io/otel/metric/noop"
	"go.opentelemetry.io/otel/trace"
	tracenoop "go.opentelemetry.io/otel/trace/noop"

	"github.com/eh-homelab/ScadBuddy/tools/go/telemetry"
)

const scope = telemetry.Scope + "/pgxtrace"

const (
	systemKey     = attribute.Key("db.system")
	systemNameKey = attribute.Key("db.system.name")
	statementKey  = attribute.Key("db.statement")
	queryTextKey  = attribute.Key("db.query.text")
	summaryKey    = attribute.Key("db.query.summary")
	operationKey  = attribute.Key("db.operation.name")
	collectionKey = attribute.Key("db.collection.name")
	batchSizeKey  = attribute.Key("db.operation.batch.size")
	rowsKey       = attribute.Key("db.response.rows_affected")
	sqlStateKey   = attribute.Key("db.response.status_code")
	errorTypeKey  = attribute.Key("error.type")
	poolNameKey   = attribute.Key("db.client.connection.pool.name")
	stateKey      = attribute.Key("db.client.connection.state")
)

// durationBuckets are the database conventions' advice for seconds.
var durationBuckets = []float64{0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1, 5, 10}

// Tracer is a pgx.QueryTracer, BatchTracer and CopyFromTracer and a pgxpool.AcquireTracer:
// set it as a pool config's ConnConfig.Tracer.
type Tracer struct {
	tracer   trace.Tracer
	duration metric.Float64Histogram
}

// New traces to tp and measures to mp, either of which may be nil (off).
func New(tp trace.TracerProvider, mp metric.MeterProvider) *Tracer {
	if tp == nil {
		tp = tracenoop.NewTracerProvider()
	}
	if mp == nil {
		mp = metricnoop.NewMeterProvider()
	}
	t := &Tracer{tracer: tp.Tracer(scope)}
	t.duration, _ = mp.Meter(scope).Float64Histogram("db.client.operation.duration", metric.WithUnit("s"),
		metric.WithDescription("Duration of database client operations."), metric.WithExplicitBucketBoundaries(durationBuckets...))
	return t
}

var (
	_ pgx.QueryTracer       = (*Tracer)(nil)
	_ pgx.BatchTracer       = (*Tracer)(nil)
	_ pgx.CopyFromTracer    = (*Tracer)(nil)
	_ pgxpool.AcquireTracer = (*Tracer)(nil)
)

type opKey struct{}

// op is one operation's start and what its measurement records.
type op struct {
	start time.Time
	attrs []attribute.KeyValue
}

// start begins a CLIENT span: with the default sampler, one with no parent (a background
// loop's query) is dropped, as the API's are. The operation is measured either way.
func (t *Tracer) start(ctx context.Context, name string, metricAttrs []attribute.KeyValue, attrs ...attribute.KeyValue) context.Context {
	attrs = append(attrs, systemKey.String("postgresql"), systemNameKey.String("postgresql"))
	ctx, _ = t.tracer.Start(ctx, name, trace.WithSpanKind(trace.SpanKindClient), trace.WithAttributes(attrs...))
	return context.WithValue(ctx, opKey{}, &op{start: time.Now(),
		attrs: append(metricAttrs, systemNameKey.String("postgresql"))})
}

func statement(sql string) (string, []attribute.KeyValue, []attribute.KeyValue) {
	op, table := Summary(sql)
	name := op
	measured := []attribute.KeyValue{operationKey.String(op)}
	if table != "" {
		name += " " + table
		measured = append(measured, collectionKey.String(table))
	}
	attrs := append([]attribute.KeyValue{statementKey.String(sql), queryTextKey.String(sql), summaryKey.String(name)}, measured...)
	return name, measured, attrs
}

func (t *Tracer) TraceQueryStart(ctx context.Context, _ *pgx.Conn, data pgx.TraceQueryStartData) context.Context {
	name, measured, attrs := statement(data.SQL)
	return t.start(ctx, name, measured, attrs...)
}

func (t *Tracer) TraceQueryEnd(ctx context.Context, _ *pgx.Conn, data pgx.TraceQueryEndData) {
	t.end(ctx, data.CommandTag, data.Err)
}

func (t *Tracer) TraceBatchStart(ctx context.Context, _ *pgx.Conn, data pgx.TraceBatchStartData) context.Context {
	size := 0
	if data.Batch != nil {
		size = data.Batch.Len()
	}
	return t.start(ctx, "BATCH", []attribute.KeyValue{operationKey.String("BATCH")},
		operationKey.String("BATCH"), summaryKey.String("BATCH"), batchSizeKey.Int(size))
}

// TraceBatchQuery records each statement of a batch as an event of the batch's span.
func (t *Tracer) TraceBatchQuery(ctx context.Context, _ *pgx.Conn, data pgx.TraceBatchQueryData) {
	span := trace.SpanFromContext(ctx)
	if !span.IsRecording() {
		return
	}
	name, _, attrs := statement(data.SQL)
	if code := sqlState(data.Err); code != "" {
		attrs = append(attrs, sqlStateKey.String(code))
	}
	span.AddEvent(name, trace.WithAttributes(attrs...))
}

func (t *Tracer) TraceBatchEnd(ctx context.Context, _ *pgx.Conn, data pgx.TraceBatchEndData) {
	t.end(ctx, pgconn.CommandTag{}, data.Err)
}

func (t *Tracer) TraceCopyFromStart(ctx context.Context, _ *pgx.Conn, data pgx.TraceCopyFromStartData) context.Context {
	table := data.TableName.Sanitize()
	measured := []attribute.KeyValue{operationKey.String("COPY"), collectionKey.String(table)}
	return t.start(ctx, "COPY "+table, measured, append(measured, summaryKey.String("COPY "+table))...)
}

func (t *Tracer) TraceCopyFromEnd(ctx context.Context, _ *pgx.Conn, data pgx.TraceCopyFromEndData) {
	t.end(ctx, data.CommandTag, data.Err)
}

type acquireKey struct{}

// TraceAcquireStart makes the wait for a pooled connection a span, but only inside a
// traced request: the pool has few connections, so a busy pod's time goes there.
func (t *Tracer) TraceAcquireStart(ctx context.Context, _ *pgxpool.Pool, _ pgxpool.TraceAcquireStartData) context.Context {
	if !trace.SpanContextFromContext(ctx).IsValid() {
		return ctx
	}
	ctx, span := t.tracer.Start(ctx, "pgxpool.acquire")
	return context.WithValue(ctx, acquireKey{}, span)
}

func (t *Tracer) TraceAcquireEnd(ctx context.Context, _ *pgxpool.Pool, data pgxpool.TraceAcquireEndData) {
	span, ok := ctx.Value(acquireKey{}).(trace.Span)
	if !ok {
		return
	}
	if data.Err != nil {
		kind := telemetry.ErrorKind(data.Err)
		span.SetAttributes(errorTypeKey.String(kind))
		span.SetStatus(codes.Error, kind)
	}
	span.End()
}

func (t *Tracer) end(ctx context.Context, tag pgconn.CommandTag, err error) {
	span := trace.SpanFromContext(ctx)
	o, _ := ctx.Value(opKey{}).(*op)
	if o == nil {
		o = &op{start: time.Now()}
	}
	attrs := o.attrs
	if err != nil {
		kind := sqlState(err)
		if kind != "" {
			span.SetAttributes(sqlStateKey.String(kind))
			attrs = append(attrs, sqlStateKey.String(kind))
		} else {
			kind = telemetry.ErrorKind(err)
		}
		span.SetAttributes(errorTypeKey.String(kind))
		span.SetStatus(codes.Error, kind)
		attrs = append(attrs, errorTypeKey.String(kind))
	} else if tag.String() != "" {
		span.SetAttributes(rowsKey.Int64(tag.RowsAffected()))
	}
	span.End()
	t.duration.Record(context.Background(), time.Since(o.start).Seconds(), metric.WithAttributes(attrs...))
}

// ObservePool reports pool's state at each collection, named name: the connections by
// state (db.client.connection.count, used and idle), its most
// (db.client.connection.max), and how often and how long acquires waited for one
// (pgxpool.acquire.waits, pgxpool.acquire.wait_time) or were canceled
// (pgxpool.acquire.canceled).
func ObservePool(mp metric.MeterProvider, pool *pgxpool.Pool, name string) error {
	if mp == nil {
		return nil
	}
	m := mp.Meter(scope)
	count, err := m.Int64ObservableUpDownCounter("db.client.connection.count", metric.WithUnit("{connection}"),
		metric.WithDescription("The number of connections that are currently in the state described by the state attribute."))
	if err != nil {
		return err
	}
	most, err := m.Int64ObservableUpDownCounter("db.client.connection.max", metric.WithUnit("{connection}"),
		metric.WithDescription("The maximum number of open connections allowed."))
	if err != nil {
		return err
	}
	waits, err := m.Int64ObservableCounter("pgxpool.acquire.waits", metric.WithUnit("{acquire}"),
		metric.WithDescription("Acquires that waited for a connection to be released or made."))
	if err != nil {
		return err
	}
	waited, err := m.Float64ObservableCounter("pgxpool.acquire.wait_time", metric.WithUnit("s"),
		metric.WithDescription("Total time acquires waited for a connection."))
	if err != nil {
		return err
	}
	canceled, err := m.Int64ObservableCounter("pgxpool.acquire.canceled", metric.WithUnit("{acquire}"),
		metric.WithDescription("Acquires canceled by their context."))
	if err != nil {
		return err
	}
	pooled := attribute.NewSet(poolNameKey.String(name))
	used := metric.WithAttributes(poolNameKey.String(name), stateKey.String("used"))
	idle := metric.WithAttributes(poolNameKey.String(name), stateKey.String("idle"))
	_, err = m.RegisterCallback(func(_ context.Context, o metric.Observer) error {
		s := pool.Stat()
		o.ObserveInt64(count, int64(s.AcquiredConns()), used)
		o.ObserveInt64(count, int64(s.IdleConns()), idle)
		o.ObserveInt64(most, int64(s.MaxConns()), metric.WithAttributeSet(pooled))
		o.ObserveInt64(waits, s.EmptyAcquireCount(), metric.WithAttributeSet(pooled))
		o.ObserveFloat64(waited, s.EmptyAcquireWaitTime().Seconds(), metric.WithAttributeSet(pooled))
		o.ObserveInt64(canceled, s.CanceledAcquireCount(), metric.WithAttributeSet(pooled))
		return nil
	}, count, most, waits, waited, canceled)
	return err
}

// sqlState is a Postgres error's SQLSTATE, or "".
func sqlState(err error) string {
	var pg *pgconn.PgError
	if errors.As(err, &pg) {
		return pg.Code
	}
	return ""
}

var (
	lead    = regexp.MustCompile(`^(?s:\s+|--[^\n]*\n?|/\*.*?\*/|\()*`)
	keyword = regexp.MustCompile(`^[A-Za-z]+`)
	name    = `(?:"[^"]+"|[A-Za-z_][A-Za-z0-9_$]*)`
	ident   = `(?P<table>` + name + `(?:\.` + name + `)?)`
	targets = map[string]*regexp.Regexp{
		"SELECT": regexp.MustCompile(`(?i)\bfrom\s+(?:only\s+)?` + ident),
		"DELETE": regexp.MustCompile(`(?i)^delete\s+from\s+(?:only\s+)?` + ident),
		"INSERT": regexp.MustCompile(`(?i)^insert\s+into\s+` + ident),
		"UPDATE": regexp.MustCompile(`(?i)^update\s+(?:only\s+)?` + ident),
		"LOCK":   regexp.MustCompile(`(?i)^lock\s+(?:table\s+)?(?:only\s+)?` + ident),
	}
)

// Summary is a statement's operation (its first keyword, upper case) and, for a
// statement that names one plainly, its table. It reads only the statement's words, never
// a value: in a SELECT, a name followed by "(" is a function's (extract(epoch FROM now())), so it is
// skipped.
func Summary(sql string) (op, table string) {
	rest := sql[len(lead.FindString(sql)):]
	op = strings.ToUpper(keyword.FindString(rest))
	if op == "" {
		return "SQL", ""
	}
	re, ok := targets[op]
	if !ok {
		return op, ""
	}
	at := re.SubexpIndex("table")
	for _, m := range re.FindAllStringSubmatchIndex(rest, -1) {
		after := strings.TrimLeft(rest[m[2*at+1]:], " \t\r\n")
		if op != "SELECT" || !strings.HasPrefix(after, "(") {
			return op, rest[m[2*at]:m[2*at+1]]
		}
	}
	return op, ""
}
