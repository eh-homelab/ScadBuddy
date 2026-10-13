package fsys

import (
	"context"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"

	"github.com/eh-homelab/ScadBuddy/tools/go/bambuddy-fs/internal/telemetry"
)

// recording records spans and metrics until the test ends.
func recording(t *testing.T) (*tracetest.SpanRecorder, *sdkmetric.ManualReader) {
	t.Helper()
	spans := tracetest.NewSpanRecorder()
	reader := sdkmetric.NewManualReader()
	t.Cleanup(telemetry.Use(sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(spans)), sdkmetric.NewMeterProvider(sdkmetric.WithReader(reader))))
	return spans, reader
}

func spanNames(spans *tracetest.SpanRecorder) map[string]int {
	out := map[string]int{}
	for _, s := range spans.Ended() {
		out[s.Name()]++
	}
	return out
}

// collect returns every metric's data points' attributes by metric name, and the
// gauges' and counters' values.
func collect(t *testing.T, reader *sdkmetric.ManualReader) (map[string][][]attribute.KeyValue, map[string]int64) {
	t.Helper()
	var rm metricdata.ResourceMetrics
	if err := reader.Collect(context.Background(), &rm); err != nil {
		t.Fatal(err)
	}
	attrs, values := map[string][][]attribute.KeyValue{}, map[string]int64{}
	for _, sm := range rm.ScopeMetrics {
		for _, m := range sm.Metrics {
			switch data := m.Data.(type) {
			case metricdata.Histogram[float64]:
				for _, dp := range data.DataPoints {
					attrs[m.Name] = append(attrs[m.Name], dp.Attributes.ToSlice())
				}
			case metricdata.Sum[int64]:
				for _, dp := range data.DataPoints {
					attrs[m.Name] = append(attrs[m.Name], dp.Attributes.ToSlice())
					values[m.Name] += dp.Value
				}
			case metricdata.Gauge[int64]:
				for _, dp := range data.DataPoints {
					attrs[m.Name] = append(attrs[m.Name], dp.Attributes.ToSlice())
					values[m.Name] = dp.Value
				}
			}
		}
	}
	return attrs, values
}

// FUSE operations that reach the library are spans and measurements whose attributes
// never say a name.
func TestOperationsAreTracedWithoutNames(t *testing.T) {
	spans, reader := recording(t)
	lib := newFakeLibrary()
	lib.file(10, "stored-secret.3mf", 0, []byte("stored"))
	dir := mount(t, lib, Options{})

	if err := os.Mkdir(filepath.Join(dir, "secret-folder"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "written-secret.3mf"), []byte("written"), 0o644); err != nil {
		t.Fatal(err)
	}
	if data, err := os.ReadFile(filepath.Join(dir, "stored-secret.3mf")); err != nil || string(data) != "stored" {
		t.Fatalf("%q %v", data, err)
	}
	if err := os.Rename(filepath.Join(dir, "written-secret.3mf"), filepath.Join(dir, "renamed-secret.3mf")); err != nil {
		t.Fatal(err)
	}
	if err := os.Remove(filepath.Join(dir, "stored-secret.3mf")); err != nil {
		t.Fatal(err)
	}
	if err := os.Remove(filepath.Join(dir, "secret-folder")); err != nil {
		t.Fatal(err)
	}
	eventually(t, "every span", func() bool {
		n := spanNames(spans)
		return n["fuse.mkdir"] == 1 && n["fuse.create"] == 1 && n["fuse.flush"] >= 1 && n["fuse.read"] == 1 &&
			n["fuse.rename"] == 1 && n["fuse.unlink"] == 1 && n["fuse.rmdir"] == 1
	})
	for _, s := range spans.Ended() {
		if !strings.HasPrefix(s.Name(), "fuse.") {
			t.Fatalf("span %s", s.Name())
		}
		for _, a := range s.Attributes() {
			if strings.Contains(a.Value.Emit(), "secret") {
				t.Fatalf("span %s records %s=%s", s.Name(), a.Key, a.Value.Emit())
			}
		}
		if strings.Contains(s.Status().Description, "secret") {
			t.Fatalf("span %s status %q", s.Name(), s.Status().Description)
		}
		for _, e := range s.Events() {
			for _, a := range append(e.Attributes, attribute.String("name", e.Name)) {
				if strings.Contains(a.Value.Emit(), "secret") {
					t.Fatalf("span %s event %s records %s=%s", s.Name(), e.Name, a.Key, a.Value.Emit())
				}
			}
		}
		if s.Name() == "fuse.read" && !hasAttr(s.Attributes(), "bambuddy_fs.file.id", "10") {
			t.Fatalf("read attributes: %v", s.Attributes())
		}
	}

	attrs, values := collect(t, reader)
	for _, name := range []string{"bambuddy_fs.fuse.operations", "bambuddy_fs.fuse.operation.duration"} {
		if len(attrs[name]) == 0 {
			t.Fatalf("no %s", name)
		}
		for _, set := range attrs[name] {
			if len(set) != 2 || set[0].Key != "op" || set[1].Key != "result" {
				t.Fatalf("%s attributes %v", name, set)
			}
		}
	}
	if !slices.ContainsFunc(attrs["bambuddy_fs.fuse.operations"], func(set []attribute.KeyValue) bool {
		return set[0].Value.AsString() == "mkdir" && set[1].Value.AsString() == "ok"
	}) {
		t.Fatalf("no mkdir ok: %v", attrs["bambuddy_fs.fuse.operations"])
	}
	for name, sets := range attrs {
		for _, set := range sets {
			for _, a := range set {
				if strings.Contains(a.Value.Emit(), "secret") {
					t.Fatalf("%s records %s=%s", name, a.Key, a.Value.Emit())
				}
			}
		}
	}
	if values["bambuddy_fs.cache.bytes"] == 0 {
		t.Fatalf("cache gauge: %v", values)
	}
}

// A lookup answered from the cached listing has no span.
func TestACachedLookupHasNoSpan(t *testing.T) {
	spans, _ := recording(t)
	lib := newFakeLibrary()
	lib.file(10, "a.3mf", 0, []byte("a"))
	dir := mount(t, lib, Options{TTL: time.Minute})
	names(t, dir)
	before := len(spans.Ended())
	for range 5 {
		if _, err := os.Stat(filepath.Join(dir, "missing")); err == nil {
			t.Fatal("missing exists")
		}
	}
	if after := len(spans.Ended()); after != before {
		t.Fatalf("%d spans for cached lookups", after-before)
	}
}

// A lookup of a name that is not there is answered, not failed: its span is no error.
func TestALookupOfAMissingNameIsNoSpanError(t *testing.T) {
	spans, _ := recording(t)
	lib := newFakeLibrary()
	dir := mount(t, lib, Options{})
	if _, err := os.Stat(filepath.Join(dir, "missing")); err == nil {
		t.Fatal("missing exists")
	}
	found := false
	for _, s := range spans.Ended() {
		if s.Name() == "fuse.lookup" {
			found = true
			if s.Status().Code == codes.Error {
				t.Fatalf("lookup span: %v %v", s.Status(), s.Attributes())
			}
		}
	}
	if !found {
		t.Fatal("no lookup span")
	}
}

// Each attempt to send a queued write is a span and counted by result, and the queue
// is measured.
func TestUnsentRetriesAreTraced(t *testing.T) {
	spans, reader := recording(t)
	logs(t)
	lib := newFakeLibrary()
	lib.setDown(true)
	dir, _ := retrying(t, lib, Options{})
	if err := os.WriteFile(filepath.Join(dir, "queued-secret.3mf"), []byte("queued"), 0o644); err != nil {
		t.Fatal(err)
	}
	eventually(t, "a failed retry", func() bool {
		for _, s := range spans.Ended() {
			if s.Name() == "unsent.retry" {
				return true
			}
		}
		return false
	})
	if _, values := collect(t, reader); values["bambuddy_fs.unsent.entries"] != 1 || values["bambuddy_fs.unsent.bytes"] != int64(len("queued")) {
		t.Fatalf("gauges: %v", values)
	}
	lib.setDown(false)
	eventually(t, "the write sent", func() bool { return libraryHas(lib, map[string]string{"0/queued-secret.3mf": "queued"}) })
	eventually(t, "a sent retry", func() bool {
		for _, s := range spans.Ended() {
			if s.Name() == "unsent.retry" && hasAttr(s.Attributes(), "bambuddy_fs.unsent.result", "ok") {
				return true
			}
		}
		return false
	})
	for _, s := range spans.Ended() {
		for _, a := range s.Attributes() {
			if strings.Contains(a.Value.Emit(), "secret") {
				t.Fatalf("span %s records %s=%s", s.Name(), a.Key, a.Value.Emit())
			}
		}
	}
	attrs, values := collect(t, reader)
	results := map[string]bool{}
	for _, set := range attrs["bambuddy_fs.unsent.uploads"] {
		if len(set) != 1 || set[0].Key != "result" {
			t.Fatalf("unsent.uploads attributes %v", set)
		}
		results[set[0].Value.AsString()] = true
	}
	if !results["ok"] || !results["failed"] || values["bambuddy_fs.unsent.entries"] != 0 {
		t.Fatalf("results %v, gauges %v", results, values)
	}
}

// hasAttr is whether attrs has key with the value that prints as want.
func hasAttr(attrs []attribute.KeyValue, key, want string) bool {
	for _, a := range attrs {
		if string(a.Key) == key && a.Value.Emit() == want {
			return true
		}
	}
	return false
}
