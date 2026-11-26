package telemetry

import (
	"context"
	"errors"
	"log"
	"log/slog"
	"regexp"

	"go.opentelemetry.io/contrib/bridges/otelslog"
	"go.opentelemetry.io/otel/trace"
)

// LogHandler is base, which keeps writing where it writes (stderr, for kubectl logs),
// plus, with logs on, the OTLP bridge (otelslog) under the scope name. A record logged
// with a context inside a span carries its trace_id and span_id in both: the bridge reads
// them from the context, and base gets them as attributes. keep says which attributes the
// OTLP copy may carry; nil is DefaultKeep. A module widens or narrows it with its own
// function, which may call DefaultKeep. Whatever keep says, a URL with credentials never
// reaches OTLP, in an attribute or in the message. Base gets everything. To make it slog's
// default, use SetDefaultLogger, which keeps the log package off OTLP.
func (t *Telemetry) LogHandler(base slog.Handler, name string, keep func(slog.Attr) bool) slog.Handler {
	h := slog.Handler(&traceIDs{Handler: base})
	if t != nil && t.Logs != nil {
		if keep == nil {
			keep = DefaultKeep
		}
		otlp := &kept{otelslog.NewHandler(name, otelslog.WithLoggerProvider(t.Logs)), keep}
		h = &fanout{h, otlp}
	}
	return h
}

// SetDefaultLogger makes slog's default logger LogHandler(base, name, keep). A service
// sets its default logger with it, not with slog.SetDefault alone. slog.SetDefault also
// sends the log package's lines to the new handler, and so to OTLP. Those lines are
// net/http's ErrorLog and httputil.ReverseProxy's, and their message holds client
// addresses, upstream error text and panic values, which keep does not filter. So the log
// package writes to base alone.
func (t *Telemetry) SetDefaultLogger(base slog.Handler, name string, keep func(slog.Attr) bool) {
	slog.SetDefault(slog.New(t.LogHandler(base, name, keep)))
	log.SetOutput(slog.NewLogLogger(base, slog.LevelInfo).Writer())
	log.SetFlags(0)
}

// dropped are the attributes DefaultKeep leaves out of the OTLP copy: a request's path or
// query, an error's text (which can quote either, or a value), a URL and a DSN.
var dropped = map[string]bool{"path": true, "error": true, "err": true, "url": true, "query": true, "dsn": true}

// credentials is a URL with a user and a password in it.
var credentials = regexp.MustCompile(`[A-Za-z][A-Za-z0-9+.-]*://[^\s/@:]*:[^\s/@]*@`)

// DefaultKeep is the OTLP copy's default policy: every attribute but those named path,
// error, err, url, query or dsn.
func DefaultKeep(a slog.Attr) bool { return !dropped[a.Key] }

// filter is a after keep and the credentials rule, and whether anything is left.
func filter(a slog.Attr, keep func(slog.Attr) bool) (slog.Attr, bool) {
	if !keep(a) {
		return a, false
	}
	a.Value = a.Value.Resolve()
	if a.Value.Kind() == slog.KindGroup {
		var inner []slog.Attr
		for _, g := range a.Value.Group() {
			if g, ok := filter(g, keep); ok {
				inner = append(inner, g)
			}
		}
		return slog.Attr{Key: a.Key, Value: slog.GroupValue(inner...)}, len(inner) > 0
	}
	if a.Value.Kind() == slog.KindString || a.Value.Kind() == slog.KindAny {
		if credentials.MatchString(a.Value.String()) {
			return a, false
		}
	}
	return a, true
}

// kept passes on only the attributes keep keeps, and no credentials.
type kept struct {
	slog.Handler
	keep func(slog.Attr) bool
}

func (k *kept) Handle(ctx context.Context, r slog.Record) error {
	out := slog.NewRecord(r.Time, r.Level, credentials.ReplaceAllString(r.Message, "<credentials>@"), r.PC)
	r.Attrs(func(a slog.Attr) bool {
		if a, ok := filter(a, k.keep); ok {
			out.AddAttrs(a)
		}
		return true
	})
	return k.Handler.Handle(ctx, out)
}

func (k *kept) WithAttrs(attrs []slog.Attr) slog.Handler {
	var keep []slog.Attr
	for _, a := range attrs {
		if a, ok := filter(a, k.keep); ok {
			keep = append(keep, a)
		}
	}
	return &kept{k.Handler.WithAttrs(keep), k.keep}
}

func (k *kept) WithGroup(name string) slog.Handler { return &kept{k.Handler.WithGroup(name), k.keep} }

// traceIDs adds the context's trace_id and span_id to each record, at the top level of the
// line even under WithGroup: it keeps the handler as it was before its first group, and,
// for a record inside a span, adds the ids there and replays the attrs and groups since.
type traceIDs struct {
	slog.Handler
	root  slog.Handler                      // the handler before the first group, or nil
	since []func(slog.Handler) slog.Handler // the WithAttrs and WithGroup calls after it
}

func (h *traceIDs) Handle(ctx context.Context, r slog.Record) error {
	sc := trace.SpanContextFromContext(ctx)
	if !sc.IsValid() {
		return h.Handler.Handle(ctx, r)
	}
	ids := []slog.Attr{slog.String("trace_id", sc.TraceID().String()), slog.String("span_id", sc.SpanID().String())}
	if h.root == nil {
		r = r.Clone()
		r.AddAttrs(ids...)
		return h.Handler.Handle(ctx, r)
	}
	out := h.root.WithAttrs(ids)
	for _, step := range h.since {
		out = step(out)
	}
	return out.Handle(ctx, r)
}

func (h *traceIDs) WithAttrs(attrs []slog.Attr) slog.Handler {
	next := &traceIDs{Handler: h.Handler.WithAttrs(attrs), root: h.root}
	if h.root != nil {
		next.since = append(h.since[:len(h.since):len(h.since)], func(x slog.Handler) slog.Handler { return x.WithAttrs(attrs) })
	}
	return next
}

func (h *traceIDs) WithGroup(name string) slog.Handler {
	if name == "" {
		return h
	}
	root := h.root
	if root == nil {
		root = h.Handler
	}
	return &traceIDs{
		Handler: h.Handler.WithGroup(name),
		root:    root,
		since:   append(h.since[:len(h.since):len(h.since)], func(x slog.Handler) slog.Handler { return x.WithGroup(name) }),
	}
}

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
