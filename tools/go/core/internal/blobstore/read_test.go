package blobstore

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"
)

func sumOf(b []byte) string {
	s := sha256.Sum256(b)
	return hex.EncodeToString(s[:])
}

// readStore serves objects at path-style paths and records each request.
type readStore struct {
	mu       sync.Mutex
	objects  map[string][]byte
	requests []*http.Request
}

func (s *readStore) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.requests = append(s.requests, r.Clone(context.Background()))
	body, ok := s.objects[r.URL.Path]
	if !ok {
		w.WriteHeader(http.StatusNotFound)
		return
	}
	_, _ = w.Write(body)
}

func readBlobs(t *testing.T, objects map[string][]byte) (*Blobs, *S3, *readStore, time.Time) {
	t.Helper()
	fake := &readStore{objects: objects}
	srv := httptest.NewServer(fake)
	t.Cleanup(srv.Close)
	now := time.Date(2026, 11, 26, 10, 0, 0, 0, time.UTC)
	s3 := &S3{Endpoint: srv.URL, Region: "us-east-1", Bucket: "sb-test", Prefix: "t",
		AccessKeyID: "testing", SecretAccessKey: "testing"}
	return &Blobs{HTTP: srv.Client(), Stream: srv.Client(), Now: func() time.Time { return now }}, s3, fake, now
}

func refOf(body []byte) Ref {
	sum := sumOf(body)
	return Ref{SHA256: sum, Kind: "tag_dump", Backend: backendS3, Size: int64(len(body)),
		BackendID: ObjectKey("t", "tag_dump", sum)}
}

func TestAReadSendsTheSignedGetThePresignMakes(t *testing.T) {
	body := []byte("hello blob")
	ref := refOf(body)
	b, s3, fake, now := readBlobs(t, map[string][]byte{"/sb-test/" + ref.BackendID: body})
	rc, err := b.fetch(context.Background(), s3, ref)
	if err != nil {
		t.Fatal(err)
	}
	got, err := io.ReadAll(rc)
	if err != nil || string(got) != string(body) {
		t.Fatalf("read %q, %v", got, err)
	}
	_ = rc.Close()
	if len(fake.requests) != 1 {
		t.Fatalf("%d requests", len(fake.requests))
	}
	r := fake.requests[0]
	if r.Method != http.MethodGet || r.URL.Path != "/sb-test/t/tag_dump/"+ref.SHA256 {
		t.Fatalf("request %s %s", r.Method, r.URL.Path)
	}
	want, _ := s3.presign("GET", ref.BackendID, nil, readTTL, now)
	if "http://"+r.Host+r.URL.RequestURI() != want {
		t.Fatalf("url %s, want %s", r.URL.RequestURI(), want)
	}
	if q := r.URL.Query(); q.Get("X-Amz-SignedHeaders") != "host" || q.Get("X-Amz-Algorithm") != "AWS4-HMAC-SHA256" {
		t.Fatalf("query %v", q)
	}
}

func TestAReadOfAWrongObjectFailsAtTheEnd(t *testing.T) {
	body := []byte("hello blob")
	ref := refOf(body)
	b, s3, _, _ := readBlobs(t, map[string][]byte{"/sb-test/" + ref.BackendID: []byte("HELLO blob")})
	rc, err := b.fetch(context.Background(), s3, ref)
	if err != nil {
		t.Fatal(err)
	}
	defer rc.Close()
	if _, err := io.ReadAll(rc); !errors.Is(err, ErrBlobMismatch) {
		t.Fatalf("err %v", err)
	}
}

func TestAReadOfAShortObjectFailsAtTheEnd(t *testing.T) {
	body := []byte("hello blob")
	ref := refOf(body)
	b, s3, _, _ := readBlobs(t, map[string][]byte{"/sb-test/" + ref.BackendID: body[:3]})
	rc, err := b.fetch(context.Background(), s3, ref)
	if err != nil {
		t.Fatal(err)
	}
	defer rc.Close()
	if _, err := io.ReadAll(rc); !errors.Is(err, ErrBlobMismatch) {
		t.Fatalf("err %v", err)
	}
}

func TestAReadOfAMissingObjectIsNotFound(t *testing.T) {
	ref := refOf([]byte("x"))
	b, s3, _, _ := readBlobs(t, map[string][]byte{})
	if _, err := b.fetch(context.Background(), s3, ref); !errors.Is(err, ErrBlobNotFound) {
		t.Fatalf("err %v", err)
	}
}

func TestAReadOfAnS3FailureIsWrapped(t *testing.T) {
	ref := refOf([]byte("x"))
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer srv.Close()
	b := &Blobs{HTTP: srv.Client(), Stream: srv.Client(), Now: time.Now}
	s3 := &S3{Endpoint: srv.URL, Region: "r", Bucket: "b", Prefix: "t", AccessKeyID: "a", SecretAccessKey: "s"}
	_, err := b.fetch(context.Background(), s3, ref)
	if err == nil || errors.Is(err, ErrBlobNotFound) || errors.Is(err, ErrNotConfigured) {
		t.Fatalf("err %v", err)
	}
}

func TestARowOnAnotherBackendIsNotConfigured(t *testing.T) {
	b, s3, _, _ := readBlobs(t, nil)
	ref := refOf([]byte("x"))
	ref.Backend = "local"
	if _, err := b.open(context.Background(), s3, ref); !errors.Is(err, ErrNotConfigured) {
		t.Fatalf("err %v", err)
	}
}

// The DB cases run in CI (they skip without SCADBUDDY_TEST_DATABASE_URL).
func TestACommittedBlobIsReadByKeyAndByContent(t *testing.T) {
	h := newHarness(t)
	body := []byte("a Bambu tag's dump")
	_, upload := h.post("/uploads", core, ask(body))
	put(t, upload, body)
	h.post("/uploads/"+upload["upload_id"].(string)+"/commit", core, nil)
	sha := sumOf(body)
	ctx := context.Background()
	for name, open := range map[string]func() (io.ReadCloser, Ref, error){
		"key":     func() (io.ReadCloser, Ref, error) { return h.blobs.OpenKey(ctx, "tag_dump-"+sha) },
		"content": func() (io.ReadCloser, Ref, error) { return h.blobs.Open(ctx, "tag_dump", sha) },
	} {
		rc, ref, err := open()
		if err != nil {
			t.Fatal(name, err)
		}
		got, err := io.ReadAll(rc)
		_ = rc.Close()
		if err != nil || string(got) != string(body) || ref.SHA256 != sha || ref.Size != int64(len(body)) || ref.Kind != "tag_dump" {
			t.Fatal(name, string(got), ref, err)
		}
	}
	if _, _, err := h.blobs.OpenKey(ctx, "tag_dump-"+sumOf([]byte("none"))); !errors.Is(err, ErrBlobNotFound) {
		t.Fatal(err)
	}
}

func TestAReadOnAVolumeStoreIsNotConfigured(t *testing.T) {
	h := newHarness(t)
	if _, err := h.pool.Exec(context.Background(),
		`INSERT INTO settings (name, value) VALUES ('store_backend', '"local"'::jsonb)`); err != nil {
		t.Fatal(err)
	}
	if _, _, err := h.blobs.OpenKey(context.Background(), "tag_dump-x"); !errors.Is(err, ErrNotConfigured) {
		t.Fatal(err)
	}
}

func TestAStoreThatIsNotS3IsNotConfigured(t *testing.T) {
	for _, store := range []*Store{{Backend: "local"}, {Backend: "s3"}} {
		if _, err := storeS3(store); !errors.Is(err, ErrNotConfigured) {
			t.Fatalf("%+v: err %v", store, err)
		}
	}
}

// #8130 review: http.Client.Timeout also covers reading the body, so the read's client has none.
func TestTheReadClientHasNoTotalTimeout(t *testing.T) {
	b := New(nil, nil, nil, nil)
	if b.Stream.Timeout != 0 || b.HTTP.Timeout != readTTL {
		t.Fatalf("stream %s, http %s", b.Stream.Timeout, b.HTTP.Timeout)
	}
}

// gatedStore sends the headers at once and the body only once release is closed.
func gatedStore(t *testing.T, body []byte, release <-chan struct{}) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		w.(http.Flusher).Flush()
		<-release
		_, _ = w.Write(body)
	}))
	t.Cleanup(srv.Close)
	return srv
}

func TestABodyMayTakeLongerThanTheHeaderWait(t *testing.T) {
	body := []byte("hello blob")
	release := make(chan struct{})
	srv := gatedStore(t, body, release)
	b := &Blobs{Stream: srv.Client(), Now: time.Now, HeaderWait: time.Second}
	s3 := &S3{Endpoint: srv.URL, Region: "r", Bucket: "b", Prefix: "t", AccessKeyID: "a", SecretAccessKey: "s"}
	rc, err := b.fetch(context.Background(), s3, refOf(body))
	if err != nil {
		t.Fatal(err)
	}
	defer rc.Close()
	time.Sleep(1500 * time.Millisecond) // past the header wait: the body must still arrive
	close(release)
	if got, err := io.ReadAll(rc); err != nil || string(got) != string(body) {
		t.Fatalf("read %q, %v", got, err)
	}
}

func TestAStoreThatSendsNoHeadersInTheWaitIsUnavailable(t *testing.T) {
	block := make(chan struct{})
	srv := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) { <-block }))
	t.Cleanup(srv.Close)
	t.Cleanup(func() { close(block) }) // runs first, so srv.Close does not wait on the handler
	b := &Blobs{Stream: srv.Client(), Now: time.Now, HeaderWait: 20 * time.Millisecond}
	s3 := &S3{Endpoint: srv.URL, Region: "r", Bucket: "b", Prefix: "t", AccessKeyID: "a", SecretAccessKey: "s"}
	_, err := b.fetch(context.Background(), s3, refOf([]byte("x")))
	if err == nil || errors.Is(err, context.Canceled) || errors.Is(err, ErrBlobNotFound) {
		t.Fatalf("err %v", err)
	}
}
