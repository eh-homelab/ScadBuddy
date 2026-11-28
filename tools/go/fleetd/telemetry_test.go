package main

import (
	"bytes"
	"context"
	"log"
	"log/slog"
	"strings"
	"sync"
	"testing"

	"go.opentelemetry.io/otel/attribute"
	sdklog "go.opentelemetry.io/otel/sdk/log"

	shared "github.com/eh-homelab/ScadBuddy/tools/go/telemetry"
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

func TestTheLogPackageAndAnErrorsTextStayOnStderr(t *testing.T) {
	prev, out, flags := slog.Default(), log.Writer(), log.Flags()
	t.Cleanup(func() { slog.SetDefault(prev); log.SetOutput(out); log.SetFlags(flags) })
	exp := &logs{}
	lp := sdklog.NewLoggerProvider(sdklog.WithProcessor(sdklog.NewSimpleProcessor(exp)))
	t.Cleanup(func() { _ = lp.Shutdown(context.Background()) })
	var stderr bytes.Buffer

	setLogger(&shared.Telemetry{Logs: lp}, slog.NewTextHandler(&stderr, nil))
	// What net/http's ErrorLog writes.
	log.Print("http: TLS handshake error from 10.0.0.7:51234: EOF")
	slog.Warn("fleetd: sending a session failed", "session", "s-1", "err", "open /home/elan/x: denied")

	exp.mu.Lock()
	defer exp.mu.Unlock()
	if len(exp.records) != 1 || exp.records[0].Body().AsString() != "fleetd: sending a session failed" {
		var bodies []string
		for _, r := range exp.records {
			bodies = append(bodies, r.Body().AsString())
		}
		t.Fatalf("OTLP records %q, want only the slog one", bodies)
	}
	var keys []string
	exp.records[0].WalkAttributes(func(kv attribute.KeyValue) bool {
		keys = append(keys, string(kv.Key))
		return true
	})
	if strings.Join(keys, ",") != "session" {
		t.Errorf("OTLP attributes %q, want only session", keys)
	}
	for _, want := range []string{"10.0.0.7:51234", "open /home/elan/x"} {
		if !strings.Contains(stderr.String(), want) {
			t.Errorf("stderr lost %q: %q", want, stderr.String())
		}
	}
}
