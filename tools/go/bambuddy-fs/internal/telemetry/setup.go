// Package telemetry is the mount's OpenTelemetry: traces, metrics and logs over OTLP/HTTP,
// set up by the shared tools/go/telemetry, to the homelab's collector unless the OTEL_*
// variables or --otel-endpoint say otherwise. Nothing here may slow the mount: export runs
// in the background, the first export error is logged once, and shutdown is bounded.
//
// It never touches the global tracer or meter provider. The Kiota libraries start
// spans of their own on the global one, which would carry the host and request
// details; leaving it the no-op keeps them out.
package telemetry

import (
	"context"
	"fmt"
	"log"
	"log/slog"
	"os"
	"strconv"
	"strings"
	"time"

	shared "github.com/eh-homelab/ScadBuddy/tools/go/telemetry"
)

// DefaultEndpoint is the homelab's collector (eh-homelab/clusters#2221). It takes no
// credentials, and its address is also the gateway's public HTTPS target, so it is
// reachable from outside the LAN too.
const DefaultEndpoint = "https://otlp.internal.nullreference.io"

// ServiceName is the resource's service.name, and so the Prometheus job label.
const ServiceName = "bambuddy-fs"

// Config is the command line's say: Endpoint (--otel-endpoint) is a base URL, like
// OTEL_EXPORTER_OTLP_ENDPOINT, and wins over the environment; Disabled
// (--no-telemetry) turns every signal off.
type Config struct {
	Endpoint string
	Disabled bool
}

func (c Config) options() shared.Options {
	return shared.Options{ServiceName: ServiceName, DefaultEndpoint: DefaultEndpoint, Endpoint: c.Endpoint, Disabled: c.Disabled}
}

// loggedKeys are the only log attributes sent over OTLP: ids. A name, a path or an
// error's text, which can carry either, stays on stderr.
var loggedKeys = map[string]bool{"file": true, "files": true, "folder": true}

// Start sets up the providers the mount records to and returns the function that
// flushes and stops them, within the time it is given. It makes no network call: an
// unreachable collector shows only as one logged export error.
func Start(cfg Config) (stop func(within time.Duration), err error) {
	t, err := shared.Start(cfg.options())
	if err != nil {
		return nil, err
	}
	if t.Traces == nil && t.Meters == nil && t.Logs == nil {
		return func(time.Duration) {}, nil
	}
	var restore func()
	if t.Traces != nil || t.Meters != nil {
		restore = install(t.Tracer(), t.Meter())
	}
	prev := slog.Default()
	if t.Logs != nil {
		// The mount's log lines keep their form on stderr; the OTLP copy keeps only ids.
		slog.SetDefault(slog.New(t.LogHandler(&stderrLines{out: log.New(os.Stderr, "", log.LstdFlags)}, ServiceName,
			func(a slog.Attr) bool { return loggedKeys[a.Key] })))
	}
	return func(within time.Duration) {
		ctx, cancel := context.WithTimeout(context.Background(), within)
		defer cancel()
		t.Shutdown(ctx)
		if restore != nil {
			restore()
		}
		if t.Logs != nil {
			slog.SetDefault(prev)
		}
	}, nil
}

// stderrLines writes a record as slog's default handler does, "<time> LEVEL msg k=v",
// through its own logger: once slog.SetDefault has another handler, the log package's
// output goes to that handler, so the default one can no longer be used.
type stderrLines struct {
	out    *log.Logger
	attrs  []slog.Attr
	prefix string
}

func (h *stderrLines) Enabled(_ context.Context, l slog.Level) bool { return l >= slog.LevelInfo }

func (h *stderrLines) Handle(_ context.Context, r slog.Record) error {
	var b strings.Builder
	b.WriteString(r.Level.String())
	b.WriteByte(' ')
	b.WriteString(r.Message)
	for _, a := range h.attrs {
		writeAttr(&b, "", a)
	}
	r.Attrs(func(a slog.Attr) bool {
		writeAttr(&b, h.prefix, a)
		return true
	})
	return h.out.Output(0, b.String())
}

func writeAttr(b *strings.Builder, prefix string, a slog.Attr) {
	a.Value = a.Value.Resolve()
	if a.Value.Kind() == slog.KindGroup {
		for _, g := range a.Value.Group() {
			writeAttr(b, prefix+a.Key+".", g)
		}
		return
	}
	s := fmt.Sprint(a.Value.Any())
	if strings.ContainsAny(s, " \t\n\"=") || s == "" {
		s = strconv.Quote(s)
	}
	fmt.Fprintf(b, " %s%s=%s", prefix, a.Key, s)
}

func (h *stderrLines) WithAttrs(attrs []slog.Attr) slog.Handler {
	c := *h
	c.attrs = append([]slog.Attr{}, h.attrs...)
	for _, a := range attrs {
		a.Key = h.prefix + a.Key
		c.attrs = append(c.attrs, a)
	}
	return &c
}

func (h *stderrLines) WithGroup(name string) slog.Handler {
	c := *h
	c.prefix = h.prefix + name + "."
	return &c
}
