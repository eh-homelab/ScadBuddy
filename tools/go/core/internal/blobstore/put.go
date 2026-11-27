package blobstore

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"net/http"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/seal"
	"github.com/eh-homelab/ScadBuddy/tools/go/telemetry"
)

// Put stores body as a blob of kind for a route of this process (#8148: a tag read's raw
// dump), as a commit does with the bytes already in hand: no staging object and no URL. It
// takes the same kinds and limits, the store's caps, the object's stage lock, and never
// overwrites a content object (a wrong one is a 409 store_object_mismatch). It answers the
// content index's key. A blob of a swept kind is collected unless the caller holds it in
// blob_refs within the sweep's grace.
func (b *Blobs) Put(ctx context.Context, kind, mediaType string, body []byte) (string, error) {
	limit, ok := Kinds[kind]
	if !ok {
		return "", refused(http.StatusUnprocessableEntity, "kind_not_allowed",
			fmt.Sprintf("no blob of kind %q is stored here", kind))
	}
	if !mediaTypePattern.MatchString(mediaType) {
		return "", fmt.Errorf("blobstore: %q is not a media type", mediaType)
	}
	size := int64(len(body))
	if size > limit {
		return "", refused(http.StatusRequestEntityTooLarge, "upload_too_large",
			fmt.Sprintf("a %s blob is at most %d bytes, not %d", kind, limit, size))
	}
	store, err := b.s3Store(ctx)
	if err != nil {
		return "", err
	}
	if err := b.room(ctx, store, size); err != nil {
		return "", err
	}
	digest := sha256.Sum256(body)
	t := ticket{Kind: kind, SHA256: hex.EncodeToString(digest[:]), Size: size, MediaType: mediaType}
	objectKey := ObjectKey(store.S3.Prefix, kind, t.SHA256)
	release, err := b.hold(ctx, t.SHA256)
	if err != nil {
		return "", err
	}
	defer release()
	there, found, err := b.read(ctx, store.S3, objectKey, limit)
	if err != nil {
		return "", err
	}
	if found {
		if err := stored(ctx, there, t); err != nil {
			return "", err
		}
	} else {
		url, headers, err := b.presignPut(store.S3, objectKey, t, readTTL)
		if err != nil {
			return "", notConfigured(err.Error())
		}
		res, err := b.send(ctx, http.MethodPut, url, body, headers)
		if err != nil {
			return "", err
		}
		res.Body.Close()
		if res.StatusCode != http.StatusOK {
			return "", unavailable(fmt.Sprintf("the S3 store answered %d to the blob's PUT", res.StatusCode))
		}
	}
	key := IndexKey(kind, t.SHA256)
	ref := Ref{SHA256: t.SHA256, Kind: kind, Backend: backendS3, BackendID: objectKey, Size: size}
	if err := pgx.BeginFunc(ctx, b.Pool, func(tx pgx.Tx) error {
		_, err := index(ctx, tx, key, ref)
		return err
	}); err != nil {
		return "", err
	}
	return key, nil
}
