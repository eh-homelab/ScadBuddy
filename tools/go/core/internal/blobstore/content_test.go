package blobstore

import (
	"bytes"
	"context"
	"errors"
	"io"
	"net/http"
	"testing"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
)

func (h *harness) withEnv(more map[string]string) {
	env := map[string]string{}
	for k, v := range h.env {
		env[k] = v
	}
	for k, v := range more {
		env[k] = v
	}
	h.blobs.LookupEnv = lookup(env)
}

// A put is ContentStore.put with no key, then touch: the object at the kind's content key,
// the row Python's put_in writes (no slug, meta {}, not verified) at kind-<sha256>.
func TestAPutIsIndexedAsPythonsContentStorePutsIt(t *testing.T) {
	h := newHarness(t)
	ctx := context.Background()
	data := []byte("a sealed workspace file")
	key, err := h.blobs.PutContent(ctx, "workspace", data)
	if err != nil {
		t.Fatal(err)
	}
	sha := sum(data)
	if key != "workspace-"+sha {
		t.Fatal(key)
	}
	object := ObjectKey("t", "workspace", sha)
	if stored, ok := h.object(object); !ok || !bytes.Equal(stored, data) {
		t.Fatal(ok, stored)
	}
	want := map[string]any{"key": key, "sha256": sha, "kind": "workspace", "backend": "s3",
		"backend_id": object, "size": int64(len(data)), "slug": nil, "meta": map[string]any{}, "verified": false}
	got := h.blobRow(key)
	for k, v := range want {
		if !equalish(got[k], v) {
			t.Errorf("%s: %v, want %v", k, got[k], v)
		}
	}
	// A re-put uploads nothing and touches the row.
	if _, err := h.pool.Exec(ctx, "UPDATE store_blobs SET touched_at = now() - interval '1 day' WHERE key = $1", key); err != nil {
		t.Fatal(err)
	}
	puts := h.s3.puts
	again, err := h.blobs.PutContent(ctx, "workspace", data)
	if err != nil || again != key || h.s3.puts != puts {
		t.Fatal(again, err, h.s3.puts, puts)
	}
	if !h.touchedRecently(key) {
		t.Fatal("a re-put did not touch the row")
	}
	// It reads back by key.
	body, found, err := h.blobs.Fetch(ctx, key)
	if err != nil || !found {
		t.Fatal(found, err)
	}
	read, err := io.ReadAll(body)
	body.Close()
	if err != nil || !bytes.Equal(read, data) {
		t.Fatal(err, read)
	}
}

// A kind no index key could hold is refused before any byte is stored.
func TestAPutOfABadKindIsRefused(t *testing.T) {
	h := newHarness(t)
	if _, err := h.blobs.PutContent(context.Background(), "Work/../x", []byte("x")); err == nil {
		t.Fatal("a bad kind was stored")
	}
	if h.s3.puts != 0 {
		t.Fatal(h.s3.puts)
	}
}

// The caps refuse a put that uploads (StoreFullError, 507); a re-put is never refused.
func TestTheStoresCapsRefuseAPutThatUploads(t *testing.T) {
	h := newHarness(t)
	ctx := context.Background()
	h.withEnv(map[string]string{"SCADBUDDY_STORE_MAX_COUNT": "1"})
	if _, err := h.blobs.PutContent(ctx, "workspace", []byte("one")); err != nil {
		t.Fatal(err)
	}
	var problem *httpx.Problem
	if _, err := h.blobs.PutContent(ctx, "workspace", []byte("two")); !errors.As(err, &problem) ||
		problem.Status != http.StatusInsufficientStorage || Reason(err) != "store_full" {
		t.Fatal(err)
	}
	if _, err := h.blobs.PutContent(ctx, "workspace", []byte("one")); err != nil {
		t.Fatal(err)
	}
}

// A key that named another object of this store (an old prefix) moves to the new one, and
// the old object, which no row names now, is released (release_replaced).
func TestAPutReleasesTheObjectItsKeyNamedBefore(t *testing.T) {
	h := newHarness(t)
	ctx := context.Background()
	data := []byte("moved")
	sha := sum(data)
	h.insertRow("workspace-"+sha, sha, "s3", "old/workspace/"+sha, len(data))
	h.putObject("old/workspace/"+sha, data)
	if _, err := h.blobs.PutContent(ctx, "workspace", data); err != nil {
		t.Fatal(err)
	}
	if got := h.blobRow("workspace-" + sha); got["backend_id"] != ObjectKey("t", "workspace", sha) || got["kind"] != "workspace" {
		t.Fatal(got)
	}
	if _, ok := h.object("old/workspace/" + sha); ok {
		t.Fatal("the replaced object was kept")
	}
}

// Fetch is stat then get: no row, a row on another backend (which stays) and a lost object
// (whose row is forgotten) are not found; an altered object fails at the end of its read.
func TestAFetchIsStatThenGet(t *testing.T) {
	h := newHarness(t)
	ctx := context.Background()
	if _, found, err := h.blobs.Fetch(ctx, "workspace-none"); err != nil || found {
		t.Fatal(found, err)
	}
	h.insertRow("workspace-bb", sum([]byte("b")), "bambuddy", "7", 1)
	if _, found, err := h.blobs.Fetch(ctx, "workspace-bb"); err != nil || found {
		t.Fatal(found, err)
	}
	if h.blobRow("workspace-bb") == nil {
		t.Fatal("another backend's row was forgotten")
	}
	lost, err := h.blobs.PutContent(ctx, "workspace", []byte("lost"))
	if err != nil {
		t.Fatal(err)
	}
	h.dropObject(ObjectKey("t", "workspace", sum([]byte("lost"))))
	if _, found, err := h.blobs.Fetch(ctx, lost); err != nil || found {
		t.Fatal(found, err)
	}
	if got := h.blobRow(lost); got != nil {
		t.Fatal("a lost object's row was kept", got)
	}
	altered, err := h.blobs.PutContent(ctx, "workspace", []byte("altered"))
	if err != nil {
		t.Fatal(err)
	}
	h.putObject(ObjectKey("t", "workspace", sum([]byte("altered"))), []byte("other bytes"))
	body, found, err := h.blobs.Fetch(ctx, altered)
	if err != nil || !found {
		t.Fatal(found, err)
	}
	_, err = io.ReadAll(body)
	body.Close()
	if !errors.Is(err, ErrBlobMismatch) {
		t.Fatal(err)
	}
}

// Backend is store_backend; on any store but S3, a put and a fetch are 503 not_configured.
func TestOnlyTheS3StorePutsAndFetches(t *testing.T) {
	h := newHarness(t)
	ctx := context.Background()
	if got, err := h.blobs.Backend(ctx); err != nil || got != "s3" {
		t.Fatal(got, err)
	}
	h.blobs.LookupEnv = lookup(map[string]string{"SCADBUDDY_STORE_BACKEND": "local"})
	if got, err := h.blobs.Backend(ctx); err != nil || got != "local" {
		t.Fatal(got, err)
	}
	refused := func(err error) bool {
		var problem *httpx.Problem
		return errors.As(err, &problem) && problem.Status == http.StatusServiceUnavailable
	}
	if _, err := h.blobs.PutContent(ctx, "workspace", []byte("x")); !refused(err) {
		t.Error(err)
	}
	if _, _, err := h.blobs.Fetch(ctx, "workspace-x"); !refused(err) {
		t.Error(err)
	}
}
