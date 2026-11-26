package telemetry

import (
	"context"
	"errors"
	"log"
	"log/slog"
	"strings"
	"sync"
	"testing"

	"go.opentelemetry.io/otel/attribute"
	sdklog "go.opentelemetry.io/otel/sdk/log"

	shared "github.com/eh-homelab/ScadBuddy/tools/go/telemetry"
)

type records struct {
	mu  sync.Mutex
	all []sdklog.Record
}

func (r *records) Export(_ context.Context, rs []sdklog.Record) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	for _, x := range rs {
		r.all = append(r.all, x.Clone())
	}
	return nil
}
func (r *records) Shutdown(context.Context) error   { return nil }
func (r *records) ForceFlush(context.Context) error { return nil }

// A path is private on a user's machine: the OTLP copy of a record keeps only its ids, and
// the log package's lines never reach OTLP.
func TestALogRecordReachesOTLPWithNoPathOrError(t *testing.T) {
	exp := &records{}
	lp := sdklog.NewLoggerProvider(sdklog.WithProcessor(sdklog.NewSimpleProcessor(exp)))
	t.Cleanup(func() { _ = lp.Shutdown(context.Background()) })
	restore := useLogs(&shared.Telemetry{Logs: lp})
	t.Cleanup(restore)

	slog.Warn("read failed", "file", 7, "path", "/home/someone/secret.3mf",
		"err", errors.New("open /home/someone/secret.3mf: permission denied"))
	log.Printf("bambuddy-fs: mount: /home/someone/library: busy")

	exp.mu.Lock()
	defer exp.mu.Unlock()
	if len(exp.all) != 1 {
		t.Fatalf("%d records reached OTLP", len(exp.all))
	}
	r := exp.all[0]
	if r.Body().AsString() != "read failed" {
		t.Fatalf("body %q", r.Body().AsString())
	}
	var keys []string
	r.WalkAttributes(func(kv attribute.KeyValue) bool {
		keys = append(keys, string(kv.Key))
		if strings.Contains(kv.Value.Emit(), "someone") {
			t.Errorf("%s holds the path", kv.Key)
		}
		return true
	})
	if len(keys) != 1 || keys[0] != "file" {
		t.Fatalf("attributes %v", keys)
	}
}
