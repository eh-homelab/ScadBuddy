package blobstore

// The content store in process for a handler that keeps whole blobs by content (#8131:
// area workspace): Backend, PutContent and Fetch are store/content.py ContentStore's
// backend name, put with no key then touch, and stat then get, over the S3 store only.

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"os"

	"github.com/jackc/pgx/v5"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
)

// Backend is store_backend, as the API runs it: local, bambuddy or s3.
func (b *Blobs) Backend(ctx context.Context) (string, error) {
	store, err := loadStore(ctx, b.Pool, b.Keys, b.LookupEnv)
	if err != nil {
		return "", err
	}
	return store.Backend, nil
}

// Reason is the reason of a store refusal (store_full, store_busy, store_unavailable,
// not_configured), "" for any other error.
func Reason(err error) string {
	var problem *httpx.Problem
	if !errors.As(err, &problem) {
		return ""
	}
	for _, f := range problem.Extensions {
		if s, ok := f.Value.(string); ok && f.Key == "reason" {
			return s
		}
	}
	return ""
}

// PutContent is `ContentStore.put(kind, data, name=…, scope=BlobScope(folder="work"))` with no key (as `_place` calls it), then
// `touch` of its row: data stored or reused through put (the caps checked only when it
// uploads), its row at IndexKey(kind, sha256) written as BlobIndex.put_in writes it (no slug,
// meta {}), and the object the key named before released when it was another of this
// store's (`release_replaced`). The S3 backend never sees Python's `name`, so there is none.
// It answers the row's key.
func (b *Blobs) PutContent(ctx context.Context, kind string, data []byte) (string, error) {
	if !kindPattern.MatchString(kind) || len(kind) > 64 {
		return "", fmt.Errorf("blobstore: %q is not a blob kind", kind)
	}
	store, err := b.s3Store(ctx)
	if err != nil {
		return "", err
	}
	spool, err := os.CreateTemp("", "blob-put-*")
	if err != nil {
		return "", err
	}
	defer func() {
		_ = spool.Close()
		_ = os.Remove(spool.Name())
	}()
	if _, err := io.Copy(spool, bytes.NewReader(data)); err != nil {
		return "", err
	}
	var key string
	var before, stored *Ref
	_, err = b.put(ctx, store, kind, spool, func(ctx context.Context, ref Ref) (bool, error) {
		key, stored = IndexKey(kind, ref.SHA256), &ref
		return true, pgx.BeginFunc(ctx, b.Pool, func(tx pgx.Tx) error {
			var err error
			before, err = putIn(ctx, tx, key, ref)
			return err
		})
	})
	if err != nil {
		return "", err
	}
	if before != nil && (before.Backend != stored.Backend || before.BackendID != stored.BackendID) {
		if before.Backend != backendS3 {
			// As release_replaced: another backend's object is left there, untracked.
			slog.WarnContext(ctx, "left a replaced blob on another backend", "key", key, "backend", before.Backend)
		} else if err := b.removeUnnamed(ctx, store.S3, *before); err != nil {
			return "", err
		}
	}
	if _, err := b.Pool.Exec(ctx, "UPDATE store_blobs SET touched_at = now() WHERE key = $1", key); err != nil {
		return "", err
	}
	return key, nil
}

// Fetch is `ContentStore.stat(key)` then `get`: found is false when no row of this store
// names key (a row on another backend stays) or its object is gone (the row is then
// forgotten). The reader ends with ErrBlobMismatch when the bytes are not the row's
// (BlobCorruptError). Any store but S3 is a 503 not_configured.
func (b *Blobs) Fetch(ctx context.Context, key string) (io.ReadCloser, bool, error) {
	if _, err := b.s3Store(ctx); err != nil {
		return nil, false, err
	}
	rc, _, err := b.OpenKey(ctx, key)
	switch {
	case errors.Is(err, ErrNotConfigured):
		// The store is S3, so the row is another backend's.
		return nil, false, nil
	case errors.Is(err, ErrBlobNotFound):
		return nil, false, b.forget(ctx, key)
	case err != nil:
		return nil, false, err
	}
	return rc, true, nil
}
