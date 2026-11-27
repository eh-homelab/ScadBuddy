package blobstore

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"net/http"
	"os"
)

// Put stores body as a blob of kind, for a Go handler in core that took the bytes itself
// (#8155: a person's page cannot PUT to the bucket, whose CORS rule and the app's CSP
// admit only the storefront). It is put with the row written as a commit writes it
// (record): the kind's limit is checked first, then put spools the bytes, holds the
// object's stage lock shared, reuses an object of the same bytes or stores them with the
// store's caps checked (stage), and refuses a content object that is not body (409
// store_object_mismatch). The object is PUT typed mediaType and private, as a commit's is.
// Its refusals are the area's (*httpx.Problem with a reason).
func (b *Blobs) Put(ctx context.Context, kind, mediaType string, body []byte) (Committed, error) {
	limit, ok := Kinds[kind]
	if !ok {
		return Committed{}, refused(http.StatusUnprocessableEntity, "kind_not_allowed",
			fmt.Sprintf("no blob of kind %q may be stored", kind))
	}
	size := int64(len(body))
	if size > limit {
		return Committed{}, refused(http.StatusRequestEntityTooLarge, "upload_too_large",
			fmt.Sprintf("a %s blob is at most %d bytes, not %d", kind, limit, size))
	}
	store, err := b.s3Store(ctx)
	if err != nil {
		return Committed{}, err
	}
	file, done, err := spool(body)
	if err != nil {
		return Committed{}, err
	}
	defer done()
	var committed Committed
	_, err = b.put(ctx, store, kind, mediaType, file, func(ctx context.Context, ref Ref) (bool, error) {
		committed = Committed{Key: IndexKey(kind, ref.SHA256), SHA256: ref.SHA256, Size: ref.Size}
		return true, b.record(ctx, committed.Key, ref)
	})
	if err != nil {
		return Committed{}, err
	}
	return committed, nil
}

// spool is data in a temporary file, for put; done closes and removes it.
func spool(data []byte) (*os.File, func(), error) {
	f, err := os.CreateTemp("", "blob-put-*")
	if err != nil {
		return nil, nil, err
	}
	done := func() {
		_ = f.Close()
		_ = os.Remove(f.Name())
	}
	if _, err := io.Copy(f, bytes.NewReader(data)); err != nil {
		done()
		return nil, nil, err
	}
	return f, done, nil
}
