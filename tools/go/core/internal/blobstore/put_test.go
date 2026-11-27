package blobstore

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"net/http"
	"testing"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
)

func TestAPutIndexesTheBlobAsACommitDoes(t *testing.T) {
	h := newHarness(t)
	body := []byte(`{"uid":"9CC69CE3"}`)
	sum := sha256.Sum256(body)
	digest := hex.EncodeToString(sum[:])
	ctx := context.Background()
	key, err := h.blobs.Put(ctx, "tag_dump", "application/json", body)
	if err != nil {
		t.Fatal(err)
	}
	if key != IndexKey("tag_dump", digest) {
		t.Fatalf("key = %s", key)
	}
	if got, ok := h.object(ObjectKey("t", "tag_dump", digest)); !ok || string(got) != string(body) {
		t.Fatalf("object = %q, %v", got, ok)
	}
	if verifiedAt(t, h, key).IsZero() {
		t.Error("the row is not verified")
	}
	// The same bytes again write no second object.
	puts := h.s3.puts
	if again, err := h.blobs.Put(ctx, "tag_dump", "application/json", body); err != nil || again != key {
		t.Fatalf("again = %s, %v", again, err)
	}
	if h.s3.puts != puts {
		t.Error("a content object there already was written again")
	}
	if len(h.stagingKeys()) != 0 {
		t.Error("a put left a staging object")
	}
}

func TestAPutRefusesAnUnknownKindAndTooManyBytes(t *testing.T) {
	h := newHarness(t)
	ctx := context.Background()
	for _, c := range []struct {
		kind   string
		size   int
		reason string
	}{{"render", 1, "kind_not_allowed"}, {"tag_dump", 64<<10 + 1, "upload_too_large"}} {
		_, err := h.blobs.Put(ctx, c.kind, "application/json", make([]byte, c.size))
		var p *httpx.Problem
		if !errors.As(err, &p) || p.Status == http.StatusOK {
			t.Fatalf("%s: %v", c.kind, err)
		}
		if len(p.Extensions) != 1 || p.Extensions[0].Value != c.reason {
			t.Errorf("%s: %v, want reason %s", c.kind, p.Extensions, c.reason)
		}
	}
}
