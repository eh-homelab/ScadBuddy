package filamentpresets

import (
	"archive/zip"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"io"
	"sync"
	"testing"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/blobstore"
)

// blob is one key of fakeBlobs: its bytes, or the error its read gives.
type blob struct {
	body []byte
	// size, when set, is the row's size in place of len(body).
	size int64
	// sizeErr is Size's error; openErr OpenKey's; readErr the stream's at its end.
	sizeErr, openErr, readErr error
}

// fakeBlobs is the store in memory. It records each key OpenKey was asked for: the GETs.
type fakeBlobs struct {
	mu     sync.Mutex
	blobs  map[string]blob
	opened []string
}

func newFake() *fakeBlobs { return &fakeBlobs{blobs: map[string]blob{}} }

func (f *fakeBlobs) put(key string, b blob) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.blobs[key] = b
}

func (f *fakeBlobs) drop(key string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	delete(f.blobs, key)
}

func (f *fakeBlobs) sizeOf(b blob) int64 {
	if b.size != 0 {
		return b.size
	}
	return int64(len(b.body))
}

func (f *fakeBlobs) Size(_ context.Context, key string) (int64, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	b, ok := f.blobs[key]
	if !ok {
		return 0, blobstore.ErrBlobNotFound
	}
	if b.sizeErr != nil {
		return 0, b.sizeErr
	}
	return f.sizeOf(b), nil
}

func (f *fakeBlobs) OpenKey(_ context.Context, key string) (io.ReadCloser, blobstore.Ref, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.opened = append(f.opened, key)
	b, ok := f.blobs[key]
	if !ok {
		return nil, blobstore.Ref{}, blobstore.ErrBlobNotFound
	}
	if b.openErr != nil {
		return nil, blobstore.Ref{}, b.openErr
	}
	var r io.Reader = bytes.NewReader(b.body)
	if b.readErr != nil {
		r = io.MultiReader(r, failing{b.readErr})
	}
	return io.NopCloser(r), blobstore.Ref{Size: f.sizeOf(b), Backend: "s3"}, nil
}

type failing struct{ err error }

func (f failing) Read([]byte) (int, error) { return 0, f.err }

// packed is a document as Python keeps it: a zip whose one member is "file"
// (orders/attachments.py BLOB_FILE, store/archive.py pack_dir).
func packed(t *testing.T, members map[string]string) []byte {
	t.Helper()
	var buf bytes.Buffer
	w := zip.NewWriter(&buf)
	for name, body := range members {
		f, err := w.Create(name)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := io.WriteString(f, body); err != nil {
			t.Fatal(err)
		}
	}
	if err := w.Close(); err != nil {
		t.Fatal(err)
	}
	return buf.Bytes()
}

// keyOf is the document's blob key and sha256 (orders/attachments.py blob_key).
func keyOf(doc string) (key, sha string) {
	sum := sha256.Sum256([]byte(doc))
	sha = hex.EncodeToString(sum[:])
	return "attachment-" + sha, sha
}
