package blobstore

import (
	"context"
	"errors"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

// The S3 backend's name in store_blobs.backend (store/s3.py `S3ContentBackend.backend`).
const backendS3 = "s3"

// ObjectKey is the S3 object of kind's blob of sha256 (store/s3.py `object_key`).
func ObjectKey(prefix, kind, sha256 string) string { return prefix + "/" + kind + "/" + sha256 }

// IndexKey is the content index's key of kind's blob of sha256
// (store/content_models.py `content_index_key`).
func IndexKey(kind, sha256 string) string { return kind + "-" + sha256 }

// objectLock is the advisory lock's text of the objects of sha256 on backend
// (store/index.py `object_lock_name`, #7477): a stager holds it shared from before its
// upload until its row commits, and a removal of an object no row names holds it
// exclusively, so neither the blob sweep nor a failed save's release removes an object a
// commit is indexing.
func objectLock(backend, sha256 string) string { return "blob-object:" + backend + ":" + sha256 }

// Ref is an object of the store (store/content_models.py BlobRef).
type Ref struct {
	SHA256    string
	Kind      string
	Backend   string
	BackendID string
	Size      int64
}

// keepCopy is store/index.py `_KEEP_COPY`: a row that goes on naming the same object keeps
// its migrated copy and its verification; one that names another object drops them.
const keepCopy = `migrated_backend = CASE WHEN (sha256, backend, backend_id) = ($2, $3, $4) THEN migrated_backend END,
 migrated_backend_id = CASE WHEN (sha256, backend, backend_id) = ($2, $3, $4) THEN migrated_backend_id END,
 migrated_error = CASE WHEN (sha256, backend, backend_id) = ($2, $3, $4) THEN migrated_error END,
 migrated_error_at = CASE WHEN (sha256, backend, backend_id) = ($2, $3, $4) THEN migrated_error_at END,
 verified_at = CASE WHEN (sha256, backend, backend_id) = ($2, $3, $4) THEN verified_at END`

// putIn points key at ref in tx, as store/index.py `BlobIndex.put_in` does with no slug,
// meta {} and no reuse: the row's own lock first, then an update that keeps an `output`
// mark, or an insert. It answers the object the key named before, nil for none.
func putIn(ctx context.Context, tx pgx.Tx, key string, ref Ref) (*Ref, error) {
	for {
		var before Ref
		err := tx.QueryRow(ctx,
			"SELECT sha256, kind, backend, backend_id, size FROM store_blobs WHERE key = $1 FOR UPDATE",
			key).Scan(&before.SHA256, &before.Kind, &before.Backend, &before.BackendID, &before.Size)
		switch {
		case err == nil:
			_, err = tx.Exec(ctx, "UPDATE store_blobs SET "+keepCopy+`,
 sha256 = $2, backend = $3, backend_id = $4, kind = $5, size = $6, slug = NULL,
 meta = '{}'::jsonb || jsonb_strip_nulls(jsonb_build_object('output', store_blobs.meta -> 'output')),
 touched_at = now() WHERE key = $1`,
				key, ref.SHA256, ref.Backend, ref.BackendID, ref.Kind, ref.Size)
			if err != nil {
				return nil, err
			}
			return &before, nil
		case !errors.Is(err, pgx.ErrNoRows):
			return nil, err
		}
		var tag pgconn.CommandTag
		tag, err = tx.Exec(ctx, `INSERT INTO store_blobs (sha256, kind, backend, backend_id, size, slug, meta, key)
 VALUES ($1, $2, $3, $4, $5, NULL, '{}'::jsonb, $6) ON CONFLICT (key) DO NOTHING`,
			ref.SHA256, ref.Kind, ref.Backend, ref.BackendID, ref.Size, key)
		if err != nil {
			return nil, err
		}
		if tag.RowsAffected() == 1 {
			return nil, nil
		}
		// Another put made the key meanwhile: lock its row and update it.
	}
}

// usage is the distinct objects on backend and their bytes (store/index.py `usage`).
func usage(ctx context.Context, q interface {
	QueryRow(context.Context, string, ...any) pgx.Row
}, backend string) (count, total int64, err error) {
	err = q.QueryRow(ctx, `SELECT count(*), coalesce(sum(size), 0)::bigint FROM (
 SELECT DISTINCT ON (backend_id) backend_id, size FROM store_blobs WHERE backend = $1) AS objects`,
		backend).Scan(&count, &total)
	return count, total, err
}
