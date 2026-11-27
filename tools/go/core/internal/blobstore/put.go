package blobstore

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"net/http"
)

// Put stores body as a blob of kind, for a Go handler in core that took the bytes itself
// (#8155: a person's page cannot PUT to the bucket, whose CORS rule and the app's CSP
// admit only the storefront). It is a commit without the staging object: the kind's limit
// and the store's caps are checked, the object's stage lock is held shared, a content
// object that is there already is never written (one that is not body is a 409
// store_object_mismatch), and the row is written as a commit writes it. Its refusals are
// the area's (*httpx.Problem with a reason).
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
	if err := b.room(ctx, store, size); err != nil {
		return Committed{}, err
	}
	digest := sha256.Sum256(body)
	t := ticket{Kind: kind, SHA256: hex.EncodeToString(digest[:]), Size: size, MediaType: mediaType}
	objectKey := ObjectKey(store.S3.Prefix, kind, t.SHA256)
	key := IndexKey(kind, t.SHA256)
	release, err := b.hold(ctx, t.SHA256)
	if err != nil {
		return Committed{}, err
	}
	defer release()
	there, found, err := b.read(ctx, store.S3, objectKey, limit)
	switch {
	case err != nil:
		return Committed{}, err
	case found:
		if err := stored(ctx, there, t); err != nil {
			return Committed{}, err
		}
	default:
		if err := b.write(ctx, store.S3, objectKey, t, body); err != nil {
			return Committed{}, err
		}
	}
	ref := Ref{SHA256: t.SHA256, Kind: kind, Backend: backendS3, BackendID: objectKey, Size: size}
	if err := b.record(ctx, key, ref); err != nil {
		return Committed{}, err
	}
	return Committed{Key: key, SHA256: t.SHA256, Size: size}, nil
}
