package blobstore

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"net/http"
	"testing"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
)

func reasonOf(err error) (int, string) {
	var p *httpx.Problem
	if !errors.As(err, &p) {
		return 0, ""
	}
	for _, f := range p.Extensions {
		if f.Key == "reason" {
			reason, _ := f.Value.(string)
			return p.Status, reason
		}
	}
	return p.Status, ""
}

// A Put indexes the blob as a commit does, with no staging object, and writes an object
// that is there already never again.
func TestAPutStoresTheBlobAsACommitDoes(t *testing.T) {
	h := newHarness(t)
	ctx := context.Background()
	body := []byte("a scan's photo")
	sum := sha256.Sum256(body)
	sha := hex.EncodeToString(sum[:])
	for range 2 {
		got, err := h.blobs.Put(ctx, "scan_image", "image/jpeg", body)
		if err != nil || got != (Committed{Key: "scan_image-" + sha, SHA256: sha, Size: int64(len(body))}) {
			t.Fatal(got, err)
		}
	}
	if h.s3.puts != 1 {
		t.Errorf("%d PUTs", h.s3.puts)
	}
	if got := h.s3.lastPut; got.Get("Content-Type") != "image/jpeg" || got.Get("x-amz-acl") != "private" {
		t.Errorf("PUT headers %v", got)
	}
	if got, ok := h.object("t/scan_image/" + sha); !ok || !bytes.Equal(got, body) {
		t.Fatalf("content object %q", got)
	}
	if staged := h.stagingKeys(); len(staged) != 0 {
		t.Errorf("staged %v", staged)
	}
	r := row(t, h.pool, "scan_image-"+sha)
	if r["kind"] != "scan_image" || r["backend_id"] != "t/scan_image/"+sha || r["verified"] != true {
		t.Fatal(r)
	}
}

func TestAPutIsRefusedAsAnUploadIs(t *testing.T) {
	h := newHarness(t)
	ctx := context.Background()
	if status, reason := reasonOf(errOf(h.blobs.Put(ctx, "piece", "image/jpeg", []byte("x")))); status != 422 || reason != "kind_not_allowed" {
		t.Errorf("kind: %d %s", status, reason)
	}
	big := make([]byte, Kinds["tag_dump"]+1)
	if status, reason := reasonOf(errOf(h.blobs.Put(ctx, "tag_dump", "application/octet-stream", big))); status != http.StatusRequestEntityTooLarge || reason != "upload_too_large" {
		t.Errorf("size: %d %s", status, reason)
	}
	body := []byte("the photo")
	sum := sha256.Sum256(body)
	h.s3.objects["/"+h.bucket+"/t/scan_image/"+hex.EncodeToString(sum[:])] = []byte("not it")
	if status, reason := reasonOf(errOf(h.blobs.Put(ctx, "scan_image", "image/png", body))); status != 409 || reason != "store_object_mismatch" {
		t.Errorf("mismatch: %d %s", status, reason)
	}
}

// A row whose object is not its bytes, and is not the content object, is passed over: the
// bytes go to the content object and the row moves there, with no 409 (#8250).
func TestAPutPassesOverARowNamedObjectOfOtherBytes(t *testing.T) {
	h := newHarness(t)
	ctx := context.Background()
	body := []byte("the scan's photo")
	sha := sum(body)
	h.insertRow("scan_image-"+sha, sha, "s3", "old/scan_image/"+sha, len(body))
	h.putObject("old/scan_image/"+sha, []byte("not it"))
	if _, err := h.blobs.Put(ctx, "scan_image", "image/jpeg", body); err != nil {
		t.Fatal(err)
	}
	object := ObjectKey("t", "scan_image", sha)
	if got, ok := h.object(object); !ok || !bytes.Equal(got, body) {
		t.Fatalf("content object %q", got)
	}
	if got := h.blobRow("scan_image-" + sha); got["backend_id"] != object {
		t.Fatal(got)
	}
}

// The caps refuse a Put that uploads (507 store_full); the same bytes stored again are not.
func TestTheStoresCapsRefuseAPutOfNewBytes(t *testing.T) {
	h := newHarness(t)
	ctx := context.Background()
	h.withEnv(map[string]string{"SCADBUDDY_STORE_MAX_COUNT": "1"})
	if _, err := h.blobs.Put(ctx, "scan_image", "image/jpeg", []byte("one")); err != nil {
		t.Fatal(err)
	}
	if status, reason := reasonOf(errOf(h.blobs.Put(ctx, "scan_image", "image/jpeg", []byte("two")))); status != http.StatusInsufficientStorage || reason != "store_full" {
		t.Fatalf("new bytes: %d %s", status, reason)
	}
	if _, err := h.blobs.Put(ctx, "scan_image", "image/jpeg", []byte("one")); err != nil {
		t.Fatal(err)
	}
}

func errOf(_ Committed, err error) error { return err }
