package blobstore

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"hash"
	"io"
	"net/http"
	"time"

	"github.com/jackc/pgx/v5"
)

var (
	// ErrBlobNotFound is a blob with no row in the content index, or no object in the store.
	ErrBlobNotFound = errors.New("blobstore: blob not found")
	// ErrNotConfigured is a store that is not a fully set S3 store, or a row on another backend.
	ErrNotConfigured = errors.New("blobstore: the S3 store is not configured")
	// ErrBlobMismatch is an object whose bytes are not the SHA-256 and size of its row. A
	// read gives it at the end of the stream.
	ErrBlobMismatch = errors.New("blobstore: the object is not the blob's bytes")
)

// Open reads kind's blob of sha256, as OpenKey reads its content-index key
// (IndexKey, store/content_models.py `content_index_key`).
func (b *Blobs) Open(ctx context.Context, kind, sha256 string) (io.ReadCloser, Ref, error) {
	return b.OpenKey(ctx, IndexKey(kind, sha256))
}

// OpenKey is the blob the store_blobs row key names, for a Go handler in core. Python's
// records (attachments' and presets' `blob_key`, `blob_refs`) name a blob by this key, so
// this is the main form; Open is the content-addressed one. It looks up the row, then sends
// the object a SigV4-signed GET (the S3 settings, `presign`) and waits at most readTTL for
// the response's headers. The bytes then stream for as long as the caller's ctx allows; the
// SHA-256 and size are checked as they go, and the read ends with ErrBlobMismatch when the
// object is not the row's. The caller closes the stream. Errors: ErrBlobNotFound (no row, or
// no object), ErrNotConfigured (the store is not S3, or the row is on another backend), else
// an error wrapped with a "blobstore:" prefix.
func (b *Blobs) OpenKey(ctx context.Context, key string) (io.ReadCloser, Ref, error) {
	store, err := loadStore(ctx, b.Pool, b.Keys, b.LookupEnv)
	if err != nil {
		return nil, Ref{}, fmt.Errorf("blobstore: reading the store's settings: %w", err)
	}
	s3, err := storeS3(store)
	if err != nil {
		return nil, Ref{}, err
	}
	var ref Ref
	err = b.Pool.QueryRow(ctx,
		"SELECT sha256, kind, backend, backend_id, size FROM store_blobs WHERE key = $1", key).
		Scan(&ref.SHA256, &ref.Kind, &ref.Backend, &ref.BackendID, &ref.Size)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return nil, Ref{}, ErrBlobNotFound
		}
		return nil, Ref{}, fmt.Errorf("blobstore: reading the blob's row: %w", err)
	}
	rc, err := b.open(ctx, s3, ref)
	if err != nil {
		return nil, Ref{}, err
	}
	return rc, ref, nil
}

// storeS3 is store's S3 settings, or ErrNotConfigured when it is not a fully set S3 store.
func storeS3(store *Store) (*S3, error) {
	if store.Backend != "s3" || store.S3 == nil {
		return nil, ErrNotConfigured
	}
	return store.S3, nil
}

// open fetches ref's object from s3, or ErrNotConfigured when the row is on another backend.
func (b *Blobs) open(ctx context.Context, s3 *S3, ref Ref) (io.ReadCloser, error) {
	if ref.Backend != backendS3 {
		return nil, ErrNotConfigured
	}
	return b.fetch(ctx, s3, ref)
}

// fetch sends the signed GET of ref's object and answers its body, verified as it is read.
// Only the wait for the headers is bounded here (HeaderWait, else readTTL); the body is the
// caller's to read within its ctx, and Close ends the request.
func (b *Blobs) fetch(ctx context.Context, s3 *S3, ref Ref) (io.ReadCloser, error) {
	signed, err := s3.presign(http.MethodGet, ref.BackendID, nil, readTTL, b.Now())
	if err != nil {
		return nil, fmt.Errorf("%w: %v", ErrNotConfigured, err)
	}
	wait := readTTL
	if b.HeaderWait > 0 {
		wait = b.HeaderWait
	}
	ctx, cancel := context.WithCancel(ctx)
	timer := time.AfterFunc(wait, cancel)
	// do's errors never name the URL, which carries a signature.
	res, err := do(ctx, b.Stream, http.MethodGet, signed, nil, nil)
	if !timer.Stop() {
		if err == nil {
			res.Body.Close()
		}
		cancel()
		return nil, fmt.Errorf("blobstore: fetching the blob: %w",
			unavailable(fmt.Sprintf("the S3 store sent no answer within %s", wait)))
	}
	if err != nil {
		cancel()
		return nil, fmt.Errorf("blobstore: fetching the blob: %w", err)
	}
	switch {
	case res.StatusCode == http.StatusNotFound:
		res.Body.Close()
		cancel()
		return nil, ErrBlobNotFound
	case res.StatusCode != http.StatusOK:
		res.Body.Close()
		cancel()
		return nil, fmt.Errorf("blobstore: the S3 store answered %d", res.StatusCode)
	}
	return &verified{src: res.Body, cancel: cancel, limited: io.LimitReader(res.Body, ref.Size+1),
		sum: sha256.New(), ref: ref}, nil
}

// verified hashes what it reads and, at the end, compares it with the row.
type verified struct {
	src     io.Closer
	cancel  context.CancelFunc
	limited io.Reader
	sum     hash.Hash
	n       int64
	ref     Ref
	done    error
}

func (v *verified) Read(p []byte) (int, error) {
	if v.done != nil {
		return 0, v.done
	}
	n, err := v.limited.Read(p)
	v.sum.Write(p[:n])
	v.n += int64(n)
	switch {
	case err == io.EOF || (err == nil && v.n > v.ref.Size):
		if v.n != v.ref.Size || hex.EncodeToString(v.sum.Sum(nil)) != v.ref.SHA256 {
			err = fmt.Errorf("%w: %s", ErrBlobMismatch, v.ref.SHA256)
		} else {
			err = io.EOF
		}
		v.done = err
	case err != nil:
		err = fmt.Errorf("blobstore: reading the blob: %w", err)
		v.done = err
	}
	return n, err
}

func (v *verified) Close() error {
	err := v.src.Close()
	v.cancel()
	return err
}
