package filamentpresets

import (
	"archive/zip"
	"bytes"
	"context"
	"errors"
	"io"

	"github.com/jackc/pgx/v5"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/blobstore"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/jsonv"
)

// DocumentLimit caps a document's read (plan ruling N10): the blob, and its member
// "file" once unzipped. A 3DFP preset document is tens of KiB; Python reads with no cap.
const DocumentLimit = 1 << 20

// blobFile is the one member of an attachment's blob (orders/attachments.py BLOB_FILE).
const blobFile = "file"

// Blobs reads the documents. Store makes one of *blobstore.Blobs.
type Blobs interface {
	// Size is the blob's size from its store_blobs row, read before any GET, or
	// blobstore.ErrBlobNotFound for no row.
	Size(ctx context.Context, key string) (int64, error)
	// OpenKey is internal/blobstore's read: the row, then the GET of its object.
	OpenKey(ctx context.Context, key string) (io.ReadCloser, blobstore.Ref, error)
}

// Store is b with the row's size read first. OpenKey gives the Ref only with the opened
// stream, once its GET has been sent; a blob over the cap must send none.
func Store(b *blobstore.Blobs) Blobs { return store{b} }

type store struct{ *blobstore.Blobs }

func (s store) Size(ctx context.Context, key string) (int64, error) {
	var size int64
	err := s.Pool.QueryRow(ctx, "SELECT size FROM store_blobs WHERE key = $1", key).Scan(&size)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return 0, blobstore.ErrBlobNotFound
		}
		return 0, err
	}
	return size, nil
}

// readDocument is the JSON document of blob key: a zip whose member "file" is the JSON
// (store/archive.py pack_dir, store/cache.py _member). It answers the document and an
// empty code, or no document, one of the codes below and the cause:
//   - document_missing: no row or object (blobstore.ErrBlobNotFound), or no member "file";
//   - document_too_large: the blob or its member over DocumentLimit; a blob whose row says
//     so sends no GET;
//   - document_unreadable: the wrong bytes (blobstore.ErrBlobMismatch), a bad zip, or JSON
//     that does not parse or is no object;
//   - store_not_configured: blobstore.ErrNotConfigured;
//   - store_unavailable: any other error of the store.
//
// A failed read of the row's size (the database, not the store) is no code: it answers
// an empty code and the error, which the handler answers as a server failure.
//
// A missing row or object changes no row: Python's read forgets the row, and this one
// writes nothing (plan ruling N3).
func readDocument(ctx context.Context, blobs Blobs, key string) (jsonv.Object, string, error) {
	size, err := blobs.Size(ctx, key)
	if errors.Is(err, blobstore.ErrBlobNotFound) {
		return nil, documentMissing, err
	}
	if err != nil {
		return nil, "", err
	}
	if size > DocumentLimit {
		return nil, documentTooLarge, nil
	}
	rc, ref, err := blobs.OpenKey(ctx, key)
	if err != nil {
		return nil, codeOf(err), err
	}
	defer rc.Close()
	if ref.Size > DocumentLimit {
		return nil, documentTooLarge, nil
	}
	// Read to the end: a wrong object is told only there.
	body, err := io.ReadAll(io.LimitReader(rc, DocumentLimit+1))
	if err != nil {
		return nil, codeOf(err), err
	}
	if len(body) > DocumentLimit {
		return nil, documentTooLarge, nil
	}
	packed, err := zip.NewReader(bytes.NewReader(body), int64(len(body)))
	if err != nil {
		return nil, documentUnreadable, err
	}
	var file *zip.File
	for _, f := range packed.File {
		if f.Name == blobFile {
			file = f
			break
		}
	}
	if file == nil {
		return nil, documentMissing, errors.New("the blob has no member file")
	}
	member, err := file.Open()
	if err != nil {
		return nil, documentUnreadable, err
	}
	defer member.Close()
	text, err := io.ReadAll(io.LimitReader(member, DocumentLimit+1))
	if err != nil {
		return nil, documentUnreadable, err
	}
	if len(text) > DocumentLimit {
		return nil, documentTooLarge, nil
	}
	v, err := jsonv.Parse(text)
	if err != nil {
		return nil, documentUnreadable, err
	}
	doc, ok := v.(jsonv.Object)
	if !ok {
		return nil, documentUnreadable, errors.New("the document is no JSON object")
	}
	return doc, "", nil
}

func codeOf(err error) string {
	switch {
	case errors.Is(err, blobstore.ErrBlobNotFound):
		return documentMissing
	case errors.Is(err, blobstore.ErrBlobMismatch):
		return documentUnreadable
	case errors.Is(err, blobstore.ErrNotConfigured):
		return storeNotConfigured
	default:
		return storeUnavailable
	}
}
