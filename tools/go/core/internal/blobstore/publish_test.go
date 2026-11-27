package blobstore

import (
	"archive/zip"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"runtime"
	"testing"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
)

// packCase is one input of backend/tests/fixtures/pack-dir-vectors.json, which
// backend/tests/store/test_pack_dir_vectors.py writes from store/archive.py pack_dir.
type packCase struct {
	Name string `json:"name"`
	File string `json:"file"`
	Data string `json:"data"`
	Zip  string `json:"zip"`
}

func packVectors(t *testing.T) []packCase {
	t.Helper()
	_, file, _, _ := runtime.Caller(0)
	raw, err := os.ReadFile(filepath.Join(filepath.Dir(file), "../../../../../backend/tests/fixtures/pack-dir-vectors.json"))
	if err != nil {
		t.Fatal(err)
	}
	var v struct {
		Cases []packCase `json:"cases"`
	}
	if err := json.Unmarshal(raw, &v); err != nil || len(v.Cases) == 0 {
		t.Fatal(err, len(v.Cases))
	}
	return v.Cases
}

func b64(t *testing.T, s string) []byte {
	t.Helper()
	return must(base64.StdEncoding.DecodeString(s))
}

// Python's zip, with its own deflate stream put back between core's header and directory,
// is the same bytes: everything but the stream is Python's.
func TestThePackedContainerIsPythons(t *testing.T) {
	for _, c := range packVectors(t) {
		python := b64(t, c.Zip)
		e := zipEntry{
			name:  c.File,
			crc:   binary.LittleEndian.Uint32(python[14:]),
			csize: int64(binary.LittleEndian.Uint32(python[18:])),
			size:  int64(binary.LittleEndian.Uint32(python[22:])),
		}
		start := 30 + len(c.File)
		stream := python[start : start+int(e.csize)]
		got := append(append(localHeader(e), stream...), directory(e)...)
		if !bytes.Equal(got, python) {
			t.Errorf("%s:\n got %x\nwant %x", c.Name, got, python)
		}
	}
}

// Core's own pack of each input holds Python's entry: the name, the bytes, the CRC, the date,
// the mode and the system, and reads back whole.
func TestAPackedFileReadsBackAsPythonsDoes(t *testing.T) {
	for _, c := range packVectors(t) {
		data, python := b64(t, c.Data), b64(t, c.Zip)
		out := must(os.CreateTemp(t.TempDir(), "pack"))
		e, err := packFile(out, c.File, bytes.NewReader(data))
		if err != nil {
			t.Fatal(err)
		}
		packed := must(io.ReadAll(io.NewSectionReader(out, 0, 1<<30)))
		if e.crc != binary.LittleEndian.Uint32(python[14:]) || e.size != int64(len(data)) {
			t.Errorf("%s: %+v", c.Name, e)
		}
		if !bytes.Equal(packed[:14], python[:14]) {
			t.Errorf("%s: the local header starts %x, not %x", c.Name, packed[:14], python[:14])
		}
		r := must(zip.NewReader(bytes.NewReader(packed), int64(len(packed))))
		if len(r.File) != 1 {
			t.Fatal(c.Name, len(r.File))
		}
		f := r.File[0]
		if f.Name != c.File || f.Method != zip.Deflate || f.CreatorVersion != 0x0314 || f.Flags != 0 ||
			f.ExternalAttrs != 0o644<<16 || f.ModifiedDate != 0x21 || f.ModifiedTime != 0 || len(f.Extra) != 0 {
			t.Errorf("%s: %+v", c.Name, f.FileHeader)
		}
		if got := must(io.ReadAll(must(f.Open()))); !bytes.Equal(got, data) {
			t.Errorf("%s: read back %q", c.Name, got)
		}
		_ = out.Close()
	}
}

func sum(b []byte) string {
	s := sha256.Sum256(b)
	return hex.EncodeToString(s[:])
}

// blobRow is the content index's row of key, as Python's tests read it; nil for none.
func (h *harness) blobRow(key string) map[string]any {
	h.t.Helper()
	if _, found, err := h.blobs.row(context.Background(), key); err != nil || !found {
		if err != nil {
			h.t.Fatal(err)
		}
		return nil
	}
	return row(h.t, h.pool, key)
}

func (h *harness) insertRow(key, sha, backend, backendID string, size int) {
	h.t.Helper()
	if _, err := h.pool.Exec(context.Background(), `INSERT INTO store_blobs (key, sha256, kind, backend, backend_id, size, meta, touched_at)
 VALUES ($1, $2, 'piece', $3, $4, $5, '{}'::jsonb, now() - interval '1 day')`, key, sha, backend, backendID, size); err != nil {
		h.t.Fatal(err)
	}
}

func (h *harness) putObject(key string, body []byte) {
	h.s3.mu.Lock()
	defer h.s3.mu.Unlock()
	h.s3.objects["/"+h.bucket+"/"+key] = body
}

func (h *harness) dropObject(key string) {
	h.s3.mu.Lock()
	defer h.s3.mu.Unlock()
	delete(h.s3.objects, "/"+h.bucket+"/"+key)
}

func (h *harness) touchedRecently(key string) bool {
	h.t.Helper()
	var recent bool
	if err := h.pool.QueryRow(context.Background(),
		"SELECT touched_at > now() - interval '1 minute' FROM store_blobs WHERE key = $1", key).Scan(&recent); err != nil {
		h.t.Fatal(err)
	}
	return recent
}

func TestAClaimTouchesTheRowThenAsksTheStore(t *testing.T) {
	h := newHarness(t)
	ctx := context.Background()
	body := []byte("a mirror")
	object := ObjectKey("t", "piece", sum(body))
	h.insertRow("plugin-mirror-a", sum(body), "s3", object, len(body))
	h.putObject(object, body)
	if held, err := h.blobs.Claim(ctx, "plugin-mirror-a"); err != nil || !held {
		t.Fatal(held, err)
	}
	if !h.touchedRecently("plugin-mirror-a") {
		t.Fatal("the claim did not touch the row")
	}
	if got := h.s3.requests[len(h.s3.requests)-1]; got != "HEAD /sb-test/"+object {
		t.Fatal(got)
	}
	// The object is lost: the row is forgotten.
	h.dropObject(object)
	if held, err := h.blobs.Claim(ctx, "plugin-mirror-a"); err != nil || held {
		t.Fatal(held, err)
	}
	if got := h.blobRow("plugin-mirror-a"); got != nil {
		t.Fatal(got)
	}
	// No row, and a row of another backend (mid-migration): not this store's; the other
	// backend's row is touched and stays.
	if held, err := h.blobs.Claim(ctx, "plugin-mirror-none"); err != nil || held {
		t.Fatal(held, err)
	}
	h.insertRow("plugin-mirror-b", sum(body), "bambuddy", "42", len(body))
	if held, err := h.blobs.Claim(ctx, "plugin-mirror-b"); err != nil || held {
		t.Fatal(held, err)
	}
	if !h.touchedRecently("plugin-mirror-b") {
		t.Fatal("the other backend's row was not touched")
	}
}

// publish is PublishFile of data as the file "file" of key.
func (h *harness) publish(key string, data []byte) error {
	return h.blobs.PublishFile(context.Background(), key, "file", bytes.NewReader(data))
}

func TestAPublishIsIndexedAsPythonIndexesAPiece(t *testing.T) {
	h := newHarness(t)
	ctx := context.Background()
	data := bytes.Repeat([]byte("a plugin's bytes "), 100)
	if err := h.publish("plugin-mirror-x", data); err != nil {
		t.Fatal(err)
	}
	got := h.blobRow("plugin-mirror-x")
	sha := got["sha256"].(string)
	object := ObjectKey("t", "piece", sha)
	stored, ok := h.object(object)
	if !ok || sum(stored) != sha {
		t.Fatal(ok, len(stored))
	}
	want := map[string]any{"key": "plugin-mirror-x", "sha256": sha, "kind": "piece", "backend": "s3",
		"backend_id": object, "size": int64(len(stored)), "slug": nil, "meta": map[string]any{}, "verified": false}
	for k, v := range want {
		if !equalish(got[k], v) {
			t.Errorf("%s: %v, want %v", k, got[k], v)
		}
	}
	// It reads back as the blob's one file.
	read, found, err := h.blobs.ReadFile(ctx, "plugin-mirror-x", "file", 1<<20)
	if err != nil || !found || !bytes.Equal(read, data) {
		t.Fatal(found, err, len(read))
	}
	if _, found, err := h.blobs.ReadFile(ctx, "plugin-mirror-x", "other", 1<<20); err != nil || found {
		t.Fatal(found, err)
	}
	if held, err := h.blobs.Claim(ctx, "plugin-mirror-x"); err != nil || !held {
		t.Fatal(held, err)
	}
}

// ReadFile holds a blob in memory, so a file past the caller's bound is refused, and so is a
// stored zip too large to hold such a file, before its bytes are read.
func TestReadFileRefusesAFilePastItsBound(t *testing.T) {
	h := newHarness(t)
	ctx := context.Background()
	data := bytes.Repeat([]byte("x"), 1000)
	if err := h.publish("plugin-mirror-big", data); err != nil {
		t.Fatal(err)
	}
	if read, found, err := h.blobs.ReadFile(ctx, "plugin-mirror-big", "file", 1000); err != nil || !found || !bytes.Equal(read, data) {
		t.Fatal(found, err, len(read))
	}
	if _, found, err := h.blobs.ReadFile(ctx, "plugin-mirror-big", "file", 999); !errors.Is(err, ErrTooLarge) || found {
		t.Fatal(found, err)
	}
	// A row whose stored zip is far larger than the bound allows: refused from the row alone.
	h.insertRow("plugin-mirror-huge", sum([]byte("x")), "s3", "t/piece/huge", 10<<20)
	h.putObject("t/piece/huge", []byte("x"))
	if _, found, err := h.blobs.ReadFile(ctx, "plugin-mirror-huge", "file", 1000); !errors.Is(err, ErrTooLarge) || found {
		t.Fatal(found, err)
	}
	if got := h.blobRow("plugin-mirror-huge"); got["key"] != "plugin-mirror-huge" {
		t.Fatal("a refused read forgot the row", got)
	}
}

func equalish(a, b any) bool {
	ra, _ := json.Marshal(a)
	rb, _ := json.Marshal(b)
	return bytes.Equal(ra, rb)
}

// A key that names a blob by the time the publish indexes is stale: nothing is indexed,
// and the object this publish uploaded, which no row names, is removed.
func TestAPublishToAKeyThatNamesABlobIsStale(t *testing.T) {
	h := newHarness(t)
	other := []byte("someone else's")
	h.insertRow("plugin-mirror-y", sum(other), "s3", ObjectKey("t", "piece", sum(other)), len(other))
	h.putObject(ObjectKey("t", "piece", sum(other)), other)
	if err := h.publish("plugin-mirror-y", []byte("mine")); !errors.Is(err, ErrStale) {
		t.Fatal(err)
	}
	if got := h.blobRow("plugin-mirror-y"); got["sha256"] != sum(other) {
		t.Fatal(got)
	}
	h.s3.mu.Lock()
	objects := len(h.s3.objects)
	h.s3.mu.Unlock()
	if objects != 1 {
		t.Fatalf("%d objects: the stale publish's own was kept", objects)
	}
}

// The same bytes another key names are that key's object, reused and never uploaded twice;
// a stale publish of them leaves the object, which a row names.
func TestAPublishReusesAnObjectAnotherRowNames(t *testing.T) {
	h := newHarness(t)
	data := []byte("shared")
	if err := h.publish("plugin-mirror-1", data); err != nil {
		t.Fatal(err)
	}
	first := h.blobRow("plugin-mirror-1")
	puts := h.s3.puts
	if err := h.publish("plugin-mirror-2", data); err != nil {
		t.Fatal(err)
	}
	if second := h.blobRow("plugin-mirror-2"); second["backend_id"] != first["backend_id"] || h.s3.puts != puts {
		t.Fatal(second, h.s3.puts, puts)
	}
	if err := h.publish("plugin-mirror-2", data); !errors.Is(err, ErrStale) {
		t.Fatal(err)
	}
	if _, ok := h.object(first["backend_id"].(string)); !ok {
		t.Fatal("a stale publish removed an object rows name")
	}
}

// A stale publish's object that a stager holds is left for the stager.
func TestAStalePublishLeavesAnObjectAStagerHolds(t *testing.T) {
	h := newHarness(t)
	ctx := context.Background()
	mine := []byte("held by a stager")
	h.insertRow("plugin-mirror-z", sum([]byte("x")), "s3", "t/piece/x", 1)
	// Python's zip of the same bytes is another object: hold the one core will pack.
	spool := must(os.CreateTemp(t.TempDir(), "p"))
	if _, err := packFile(spool, "file", bytes.NewReader(mine)); err != nil {
		t.Fatal(err)
	}
	sha, _, err := digest(spool)
	if err != nil {
		t.Fatal(err)
	}
	stager, err := h.pool.Acquire(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer stager.Release()
	if _, err := stager.Exec(ctx, "SELECT pg_advisory_lock_shared(hashtextextended($1, 0))", objectLock(backendS3, sha)); err != nil {
		t.Fatal(err)
	}
	defer stager.Exec(ctx, "SELECT pg_advisory_unlock_all()")
	if err := h.publish("plugin-mirror-z", mine); !errors.Is(err, ErrStale) {
		t.Fatal(err)
	}
	if _, ok := h.object(ObjectKey("t", "piece", sha)); !ok {
		t.Fatal("the stale publish removed an object a stager holds")
	}
}

func TestTheStoresCapsRefuseAPublishThatUploads(t *testing.T) {
	h := newHarness(t)
	env := map[string]string{"SCADBUDDY_STORE_MAX_COUNT": "1"}
	for k, v := range h.env {
		env[k] = v
	}
	h.blobs.LookupEnv = lookup(env)
	if err := h.publish("plugin-mirror-1", []byte("one")); err != nil {
		t.Fatal(err)
	}
	var problem *httpx.Problem
	if err := h.publish("plugin-mirror-2", []byte("two")); !errors.As(err, &problem) || problem.Status != http.StatusInsufficientStorage {
		t.Fatal(err)
	}
	// A reuse uploads nothing, and is never refused.
	if err := h.publish("plugin-mirror-3", []byte("one")); err != nil {
		t.Fatal(err)
	}
}

func TestAReadOfALostOrAlteredObjectForgetsTheRow(t *testing.T) {
	h := newHarness(t)
	ctx := context.Background()
	for name, spoil := range map[string]func(object string){
		"lost":    h.dropObject,
		"altered": func(object string) { h.putObject(object, []byte("other bytes")) },
	} {
		key := "plugin-mirror-" + name
		if err := h.publish(key, []byte(name)); err != nil {
			t.Fatal(err)
		}
		spoil(h.blobRow(key)["backend_id"].(string))
		if data, found, err := h.blobs.ReadFile(ctx, key, "file", 1<<20); err != nil || found {
			t.Fatal(name, data, found, err)
		}
		if got := h.blobRow(key); got != nil {
			t.Fatal(name, got)
		}
	}
	// No row, and a row of another backend, which stays: not found.
	if _, found, err := h.blobs.ReadFile(ctx, "plugin-mirror-none", "file", 1<<20); err != nil || found {
		t.Fatal(found, err)
	}
	h.insertRow("plugin-mirror-bb", sum([]byte("b")), "bambuddy", "7", 1)
	if _, found, err := h.blobs.ReadFile(ctx, "plugin-mirror-bb", "file", 1<<20); err != nil || found {
		t.Fatal(found, err)
	}
	if h.blobRow("plugin-mirror-bb") == nil {
		t.Fatal("another backend's row was forgotten")
	}
}

func TestOnlyTheS3StoreServesTheCallsInProcess(t *testing.T) {
	h := newHarness(t)
	h.blobs.LookupEnv = lookup(map[string]string{"SCADBUDDY_STORE_BACKEND": "local"})
	ctx := context.Background()
	refused := func(err error) bool {
		var problem *httpx.Problem
		return errors.As(err, &problem) && problem.Status == http.StatusServiceUnavailable
	}
	if _, err := h.blobs.Claim(ctx, "k"); !refused(err) {
		t.Error(err)
	}
	if err := h.publish("k", []byte("x")); !refused(err) {
		t.Error(err)
	}
	if _, _, err := h.blobs.ReadFile(ctx, "k", "file", 1<<20); !refused(err) {
		t.Error(err)
	}
}
