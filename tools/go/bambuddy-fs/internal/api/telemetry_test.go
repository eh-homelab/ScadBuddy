package api

import (
	"bytes"
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"syscall"
	"testing"

	"go.opentelemetry.io/otel/attribute"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"

	"github.com/eh-homelab/ScadBuddy/tools/go/bambuddy-fs/internal/telemetry"
)

// Each API call is a CLIENT span the backend continues through traceparent, measured
// with only low-cardinality attributes, and none of it says a file's name.
func TestAPICallsAreTracedAndMeasured(t *testing.T) {
	const secret = "secret-name.3mf"
	content := []byte("0123456789")
	var mu sync.Mutex
	var parents []string
	mux := http.NewServeMux()
	mux.HandleFunc("GET /api/v1/print/library", func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		parents = append(parents, r.Header.Get("traceparent"))
		mu.Unlock()
		writeJSON(w, http.StatusOK, map[string]any{
			"folders": []any{},
			"files":   []any{map[string]any{"id": 7, "filename": secret, "folder_id": nil, "file_size": len(content), "is_external": false}},
		})
	})
	mux.HandleFunc("GET /api/v1/print/library/7/file", func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		parents = append(parents, r.Header.Get("traceparent"))
		mu.Unlock()
		_, _ = w.Write(content)
	})
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)

	spans := tracetest.NewSpanRecorder()
	reader := sdkmetric.NewManualReader()
	t.Cleanup(telemetry.Use(sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(spans)), sdkmetric.NewMeterProvider(sdkmetric.WithReader(reader))))

	c, err := New(srv.URL+"/api/v1", srv.Client())
	if err != nil {
		t.Fatal(err)
	}
	if l, err := c.List(context.Background(), 0, true); err != nil || len(l.Files) != 1 {
		t.Fatalf("%+v %v", l, err)
	}
	var got bytes.Buffer
	if err := c.Download(context.Background(), 7, &got); err != nil || got.String() != string(content) {
		t.Fatalf("%q %v", got.String(), err)
	}

	ended := spans.Ended()
	if len(ended) != 2 || len(parents) != 2 {
		t.Fatalf("%d spans, %d requests", len(ended), len(parents))
	}
	for i, want := range []struct{ name, route string }{
		{"scadbuddy.list_library", "/api/v1/print/library"},
		{"scadbuddy.download", "/api/v1/print/library/{file_id}/file"},
	} {
		s := ended[i]
		if s.Name() != want.name || s.SpanKind() != trace.SpanKindClient {
			t.Fatalf("span %d: %s %v", i, s.Name(), s.SpanKind())
		}
		attrs := map[attribute.Key]attribute.Value{}
		for _, a := range s.Attributes() {
			attrs[a.Key] = a.Value
		}
		if attrs["url.template"].AsString() != want.route || attrs["http.request.method"].AsString() != "GET" || attrs["http.response.status_code"].AsInt64() != 200 {
			t.Fatalf("span %d attributes: %v", i, s.Attributes())
		}
		sc := s.SpanContext()
		if wantParent := fmt.Sprintf("00-%s-%s-01", sc.TraceID(), sc.SpanID()); parents[i] != wantParent {
			t.Fatalf("request %d traceparent %q, want %q", i, parents[i], wantParent)
		}
		for _, a := range s.Attributes() {
			if strings.Contains(a.Value.Emit(), "secret") {
				t.Fatalf("span %s records %s=%s", s.Name(), a.Key, a.Value.Emit())
			}
		}
	}
	if size := ended[1].Attributes(); !hasAttr(size, "http.response.body.size", int64(len(content))) || !hasAttr(size, "bambuddy_fs.file.id", 7) {
		t.Fatalf("download attributes: %v", size)
	}

	var rm metricdata.ResourceMetrics
	if err := reader.Collect(context.Background(), &rm); err != nil {
		t.Fatal(err)
	}
	allowed := map[attribute.Key]bool{"scadbuddy.operation": true, "http.request.method": true, "http.response.status_code": true, "error.type": true, "direction": true}
	seen := map[string]bool{}
	for _, sm := range rm.ScopeMetrics {
		for _, m := range sm.Metrics {
			seen[m.Name] = true
			switch data := m.Data.(type) {
			case metricdata.Histogram[float64]:
				if len(data.DataPoints) != 2 {
					t.Fatalf("%s: %d series", m.Name, len(data.DataPoints))
				}
				for _, dp := range data.DataPoints {
					checkAttrs(t, m.Name, dp.Attributes.ToSlice(), allowed)
				}
			case metricdata.Sum[int64]:
				for _, dp := range data.DataPoints {
					checkAttrs(t, m.Name, dp.Attributes.ToSlice(), allowed)
					if m.Name == "bambuddy_fs.transfer.bytes" && dp.Value != int64(len(content)) {
						t.Fatalf("transferred %d", dp.Value)
					}
				}
			}
		}
	}
	if !seen["http.client.request.duration"] || !seen["bambuddy_fs.transfer.bytes"] {
		t.Fatalf("metrics: %v", seen)
	}
}

// A poll carries its operation's id; a write's own span is named for the write.
func TestAPollIsItsOwnSpan(t *testing.T) {
	f := newFake(t)
	f.slow["mkdir"] = true
	spans := tracetest.NewSpanRecorder()
	t.Cleanup(telemetry.Use(sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(spans)), nil))
	if _, err := f.client(t).Mkdir(context.Background(), 0, "A"); err != nil {
		t.Fatal(err)
	}
	var names []string
	for _, s := range spans.Ended() {
		names = append(names, s.Name())
		if s.Name() == "scadbuddy.poll_operation" {
			found := false
			for _, a := range s.Attributes() {
				found = found || (a.Key == "scadbuddy.operation.id" && a.Value.AsString() != "")
			}
			if !found {
				t.Fatalf("poll attributes: %v", s.Attributes())
			}
		}
	}
	if strings.Join(names, ",") != "scadbuddy.create_folder,scadbuddy.poll_operation,scadbuddy.poll_operation" {
		t.Fatalf("spans %v", names)
	}
}

func hasAttr(attrs []attribute.KeyValue, key string, v int64) bool {
	for _, a := range attrs {
		if string(a.Key) == key && a.Value.AsInt64() == v {
			return true
		}
	}
	return false
}

func checkAttrs(t *testing.T, metric string, attrs []attribute.KeyValue, allowed map[attribute.Key]bool) {
	t.Helper()
	for _, a := range attrs {
		if !allowed[a.Key] {
			t.Fatalf("%s has attribute %s", metric, a.Key)
		}
	}
}

// An upload counts the file's bytes, not the multipart body's, and a request answered
// with an error carries its status as error.type.
func TestUploadCountsFileBytesAndAnErrorHasAType(t *testing.T) {
	f := newFake(t)
	f.folder(4, "Top", 0)
	reader := sdkmetric.NewManualReader()
	t.Cleanup(telemetry.Use(nil, sdkmetric.NewMeterProvider(sdkmetric.WithReader(reader))))
	c := f.client(t)
	if _, err := c.Upload(context.Background(), 4, "a.3mf", "", strings.NewReader("0123456789")); err != nil {
		t.Fatal(err)
	}
	if err := c.Download(context.Background(), 99, &bytes.Buffer{}); Errno(err) != syscall.ENOENT {
		t.Fatalf("%v", err)
	}
	var rm metricdata.ResourceMetrics
	if err := reader.Collect(context.Background(), &rm); err != nil {
		t.Fatal(err)
	}
	var uploaded int64
	typed := false
	for _, sm := range rm.ScopeMetrics {
		for _, m := range sm.Metrics {
			switch data := m.Data.(type) {
			case metricdata.Sum[int64]:
				for _, dp := range data.DataPoints {
					if v, _ := dp.Attributes.Value("direction"); m.Name == "bambuddy_fs.transfer.bytes" && v.AsString() == "upload" {
						uploaded += dp.Value
					}
				}
			case metricdata.Histogram[float64]:
				for _, dp := range data.DataPoints {
					status, _ := dp.Attributes.Value("http.response.status_code")
					errType, ok := dp.Attributes.Value("error.type")
					if status.AsInt64() == 404 {
						typed = ok && errType.AsString() == "404"
					} else if ok {
						t.Fatalf("error.type on a %d", status.AsInt64())
					}
				}
			}
		}
	}
	if uploaded != 10 || !typed {
		t.Fatalf("uploaded %d, 404 typed %v", uploaded, typed)
	}
}
