package blobclient

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"

	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"

	"github.com/eh-homelab/ScadBuddy/tools/go/telemetry"
)

// fake is the area's API on one server and the presigned URL on another.
type fake struct {
	t       *testing.T
	mu      sync.Mutex
	stored  []byte
	puts    int
	commits int
	// putStatus is the store's answer to a PUT; 0 is 200.
	putStatus int
	// commitProblem, when set, is the commit's answer.
	commitProblem string
	// hold, when set, blocks the PUT until the request's context ends; it is closed once
	// the PUT has started.
	hold  chan struct{}
	api   *httptest.Server
	s3    *httptest.Server
	asked map[string]any
	// apiTraced and storeTraced say whether a request to each carried a traceparent.
	apiTraced, storeTraced bool
}

func newFake(t *testing.T) *fake {
	f := &fake{t: t}
	f.s3 = httptest.NewServer(http.HandlerFunc(f.store))
	f.api = httptest.NewServer(http.HandlerFunc(f.serve))
	t.Cleanup(f.s3.Close)
	t.Cleanup(f.api.Close)
	return f
}

func (f *fake) store(w http.ResponseWriter, r *http.Request) {
	if f.hold != nil {
		// Read whole, so the server watches the connection and ends the request's
		// context when the client goes.
		_, _ = io.Copy(io.Discard, r.Body)
		close(f.hold)
		<-r.Context().Done()
		return
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	f.puts++
	f.storeTraced = f.storeTraced || r.Header.Get("traceparent") != ""
	if r.Method != http.MethodPut || r.Header.Get("x-amz-checksum-sha256") != "c2lnbmVk" ||
		r.Header.Get("Content-Type") != "application/octet-stream" || r.URL.Query().Get("X-Amz-Signature") != "s" {
		w.WriteHeader(http.StatusForbidden)
		return
	}
	if f.putStatus != 0 {
		w.WriteHeader(f.putStatus)
		return
	}
	f.stored, _ = io.ReadAll(r.Body)
}

func (f *fake) serve(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.apiTraced = f.apiTraced || r.Header.Get("traceparent") != ""
	if r.Header.Get("Authorization") != "Bearer sbsvc_test" {
		w.WriteHeader(http.StatusUnauthorized)
		return
	}
	switch r.URL.Path {
	case Path + "/uploads":
		_ = json.NewDecoder(r.Body).Decode(&f.asked)
		_ = json.NewEncoder(w).Encode(map[string]any{
			"upload_id":  "the.ticket",
			"url":        f.s3.URL + "/b/t/tag_dump/x?X-Amz-Signature=s",
			"headers":    map[string]string{"Content-Type": "application/octet-stream", "x-amz-checksum-sha256": "c2lnbmVk"},
			"expires_at": "2026-11-26T12:15:00Z",
		})
	case Path + "/uploads/the.ticket/commit":
		f.commits++
		if f.commitProblem != "" {
			w.Header().Set("Content-Type", "application/problem+json")
			w.WriteHeader(http.StatusUnprocessableEntity)
			_ = json.NewEncoder(w).Encode(map[string]any{"status": 422, "detail": "no", "reason": f.commitProblem})
			return
		}
		sum := sha256.Sum256(f.stored)
		_ = json.NewEncoder(w).Encode(map[string]any{
			"key": "tag_dump-" + hex.EncodeToString(sum[:]), "sha256": hex.EncodeToString(sum[:]), "size": len(f.stored),
		})
	default:
		w.WriteHeader(http.StatusNotFound)
	}
}

func (f *fake) client() *Client {
	return New(f.api.URL, func() (string, error) { return "sbsvc_test", nil }, nil)
}

func TestAnUploadStoresTheBytesAsBytes(t *testing.T) {
	f := newFake(t)
	body := []byte{0x00, 0xff, 0x10, 'd', 'u', 'm', 'p'}
	ref, err := f.client().Upload(context.Background(), "tag_dump", "application/octet-stream", body)
	if err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256(body)
	want := Ref{Key: "tag_dump-" + hex.EncodeToString(sum[:]), SHA256: hex.EncodeToString(sum[:]), Size: int64(len(body))}
	if ref != want || string(f.stored) != string(body) {
		t.Fatalf("%+v %q", ref, f.stored)
	}
	if f.asked["kind"] != "tag_dump" || f.asked["sha256"] != want.SHA256 || f.asked["size"] != float64(len(body)) ||
		f.asked["media_type"] != "application/octet-stream" {
		t.Fatal(f.asked)
	}
}

func TestARefusedPutIsAnErrorAndNoCommit(t *testing.T) {
	f := newFake(t)
	f.putStatus = http.StatusBadRequest
	_, err := f.client().Upload(context.Background(), "tag_dump", "application/octet-stream", []byte("x"))
	var e *Error
	if !errors.As(err, &e) || e.Step != "put" || e.Status != http.StatusBadRequest {
		t.Fatal(err)
	}
	if f.commits != 0 {
		t.Fatal("committed after a refused PUT")
	}
	if strings.Contains(err.Error(), "X-Amz-Signature") || strings.Contains(err.Error(), f.s3.URL) {
		t.Fatal("the error quotes the URL:", err)
	}
}

func TestARefusedCommitNamesItsReason(t *testing.T) {
	f := newFake(t)
	f.commitProblem = "upload_mismatch"
	_, err := f.client().Upload(context.Background(), "tag_dump", "application/octet-stream", []byte("x"))
	var e *Error
	if !errors.As(err, &e) || e.Step != "commit" || e.Status != 422 || e.Reason != "upload_mismatch" {
		t.Fatal(err)
	}
}

func TestACancelledUploadStops(t *testing.T) {
	f := newFake(t)
	f.hold = make(chan struct{})
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() {
		_, err := f.client().Upload(ctx, "tag_dump", "application/octet-stream", []byte("x"))
		done <- err
	}()
	<-f.hold
	cancel()
	if err := <-done; !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
	if f.commits != 0 {
		t.Fatal("committed after a cancel")
	}
}

// With telemetry on, each request is a CLIENT span: the calls to core carry the trace
// context and the PUT to the store carries none, and no span records the presigned URL.
func TestAnUploadIsTracedWithoutItsURL(t *testing.T) {
	f := newFake(t)
	spans := tracetest.NewInMemoryExporter()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSyncer(spans))
	t.Cleanup(func() { _ = tp.Shutdown(context.Background()) })
	c := New(f.api.URL, func() (string, error) { return "sbsvc_test", nil }, &telemetry.Telemetry{Traces: tp})
	ctx, parent := tp.Tracer("test").Start(context.Background(), "upload")
	if _, err := c.Upload(ctx, "tag_dump", "application/octet-stream", []byte("dump")); err != nil {
		t.Fatal(err)
	}
	parent.End()
	if !f.apiTraced || f.storeTraced {
		t.Fatalf("trace context to core %v, to the store %v", f.apiTraced, f.storeTraced)
	}
	var put bool
	for _, s := range spans.GetSpans() {
		for _, kv := range s.Attributes {
			if strings.Contains(kv.Value.Emit(), "X-Amz") || strings.Contains(kv.Value.Emit(), "/b/t/") {
				t.Errorf("%s records %s=%s", s.Name, kv.Key, kv.Value.Emit())
			}
			if string(kv.Key) == "server.address" && kv.Value.AsString() == "s3" && s.Name == "PUT" {
				put = true
			}
		}
	}
	if !put {
		t.Fatalf("no PUT span to s3: %v", spans.GetSpans())
	}
}
