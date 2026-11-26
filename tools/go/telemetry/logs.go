package telemetry

import (
	"context"
	"errors"
	"log/slog"

	"go.opentelemetry.io/contrib/bridges/otelslog"
	"go.opentelemetry.io/otel/trace"
)

// LogHandler is base, which keeps writing where it writes (stderr, for kubectl logs),
// plus, with logs on, the OTLP bridge (otelslog) under the scope name. A record logged
// with a context inside a span carries its trace_id and span_id in both: the bridge reads
// them from the context, and base gets them as attributes. keep, when it is not nil, says
// which attributes the OTLP copy may carry; base gets them all.
func (t *Telemetry) LogHandler(base slog.Handler, name string, keep func(slog.Attr) bool) slog.Handler {
	h := slog.Handler(&traceIDs{base})
	if t != nil && t.Logs != nil {
		var otlp slog.Handler = otelslog.NewHandler(name, otelslog.WithLoggerProvider(t.Logs))
		if keep != nil {
			otlp = &kept{otlp, keep}
		}
		h = &fanout{h, otlp}
	}
	return h
}

// kept passes on only the attributes keep keeps.
type kept struct {
	slog.Handler
	keep func(slog.Attr) bool
}

func (k *kept) Handle(ctx context.Context, r slog.Record) error {
	out := slog.NewRecord(r.Time, r.Level, r.Message, r.PC)
	r.Attrs(func(a slog.Attr) bool {
		if k.keep(a) {
			out.AddAttrs(a)
		}
		return true
	})
	return k.Handler.Handle(ctx, out)
}

func (k *kept) WithAttrs(attrs []slog.Attr) slog.Handler {
	var keep []slog.Attr
	for _, a := range attrs {
		if k.keep(a) {
			keep = append(keep, a)
		}
	}
	return &kept{k.Handler.WithAttrs(keep), k.keep}
}

func (k *kept) WithGroup(name string) slog.Handler { return &kept{k.Handler.WithGroup(name), k.keep} }

// traceIDs adds the context's trace_id and span_id to each record.
type traceIDs struct{ slog.Handler }

func (h *traceIDs) Handle(ctx context.Context, r slog.Record) error {
	if sc := trace.SpanContextFromContext(ctx); sc.IsValid() {
		r = r.Clone()
		r.AddAttrs(slog.String("trace_id", sc.TraceID().String()), slog.String("span_id", sc.SpanID().String()))
	}
	return h.Handler.Handle(ctx, r)
}

func (h *traceIDs) WithAttrs(attrs []slog.Attr) slog.Handler {
	return &traceIDs{h.Handler.WithAttrs(attrs)}
}

func (h *traceIDs) WithGroup(name string) slog.Handler { return &traceIDs{h.Handler.WithGroup(name)} }

// fanout gives each record to both handlers that want its level.
type fanout struct{ a, b slog.Handler }

func (f *fanout) Enabled(ctx context.Context, l slog.Level) bool {
	return f.a.Enabled(ctx, l) || f.b.Enabled(ctx, l)
}

func (f *fanout) Handle(ctx context.Context, r slog.Record) error {
	var err error
	if f.a.Enabled(ctx, r.Level) {
		err = f.a.Handle(ctx, r.Clone())
	}
	if f.b.Enabled(ctx, r.Level) {
		err = errors.Join(err, f.b.Handle(ctx, r))
	}
	return err
}

func (f *fanout) WithAttrs(attrs []slog.Attr) slog.Handler {
	return &fanout{f.a.WithAttrs(attrs), f.b.WithAttrs(attrs)}
}

func (f *fanout) WithGroup(name string) slog.Handler {
	return &fanout{f.a.WithGroup(name), f.b.WithGroup(name)}
}
