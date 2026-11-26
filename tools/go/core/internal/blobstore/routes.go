// Package blobstore is the shared CRUD area blobs (#7958): a service stores bytes in the
// blob store by URL, under /api/v1/internal/crud/blobs, with any credential whose scopes
// hold crud.blobs. It asks for an upload, PUTs the bytes to the URL it gets, and commits
// the upload, which indexes the blob. The caller holds no store credential and writes no
// key of the store's layout; no byte passes through core on the way in.
//
// The store is Python's (backend/scadbuddy/store/): the object key and the content index's
// row are the ones its ContentStore writes (ObjectKey, IndexKey, putIn), so Python reads
// what core indexed; backend/tests/fixtures/blob-upload-vectors.json pins both sides. Only
// store_backend s3 takes uploads: the URL is S3's presigned PUT, its length, type, SHA-256
// checksum and canned ACL signed in, and the commit reads the object back and hashes it.
// Any other backend is a 503 not_configured.
//
// A committed blob of a swept kind (store/content_models.py SWEPT_KINDS) is collected unless
// its caller holds it in blob_refs within the sweep's grace; an object PUT and never
// committed is an object no row names, which the unrecorded sweep removes.
package blobstore

import (
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"regexp"
	"strconv"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/seal"
)

// Area is the shared area's name: its routes are under /api/v1/internal/crud/blobs, its
// route class crud.blobs.
const Area = "blobs"

// Kinds are the blob kinds a service may upload, each with the most bytes one blob of it
// holds. A Bambu tag's raw dump (#7905) is about 1 KiB.
var Kinds = map[string]int64{"tag_dump": 64 << 10}

const (
	// urlTTL is how long an upload's URL may start (S3 checks the expiry when a request starts).
	urlTTL = 15 * time.Minute
	// commitGrace is how long after its URL's expiry an upload may still be committed: a
	// PUT that started in time may end after it.
	commitGrace = time.Hour
	// readTTL bounds the commit's own read of the object.
	readTTL = time.Minute
	// stageLockTimeout is how long a commit waits for an object a removal holds
	// (store/index.py STAGE_LOCK_TIMEOUT_MS waits per try).
	stageLockTimeout = "5000ms"
	// bodyMax bounds a request's JSON.
	bodyMax = 4 << 10
	// The canned ACL every object is written with (store/s3.py STAGED_ACL).
	acl = "private"
)

var (
	kindPattern      = regexp.MustCompile(`^[a-z_]+$`)
	sha256Pattern    = regexp.MustCompile(`^[0-9a-f]{64}$`)
	mediaTypePattern = regexp.MustCompile(`^[a-z0-9][a-z0-9!#$&^_.+-]{0,63}/[a-z0-9][a-z0-9!#$&^_.+-]{0,63}$`)
	uploadIDPattern  = regexp.MustCompile(`^[A-Za-z0-9_=.-]+$`)
)

// Blobs serves the area.
type Blobs struct {
	Pool *pgxpool.Pool
	Keys *seal.Keys
	Env  func(string) string
	// HTTP reads an object back at the commit.
	// TODO(#7851): wrap its transport in tools/go/telemetry tel.Transport once that is on main.
	HTTP *http.Client
	Now  func() time.Time
}

// Routes are the area's routes. keys may not be nil: an upload's id is MACed under the KEK.
func Routes(pool *pgxpool.Pool, keys *seal.Keys, env func(string) string) []crud.Route {
	b := &Blobs{Pool: pool, Keys: keys, Env: env, HTTP: &http.Client{Timeout: readTTL}, Now: time.Now}
	return b.Routes()
}

// Routes are b's routes.
func (b *Blobs) Routes() []crud.Route {
	return []crud.Route{
		{Method: "POST", Path: "/uploads", Handle: b.begin, MaxBody: bodyMax},
		{Method: "POST", Path: "/uploads/{upload_id}/commit", Handle: b.commit},
	}
}

// upload is the answer to POST /uploads. Its URL and headers are a bearer credential.
type upload struct {
	UploadID  string            `json:"upload_id"`
	URL       string            `json:"url"`
	Headers   map[string]string `json:"headers"`
	ExpiresAt crud.Time         `json:"expires_at"`
}

// committed is the answer to a commit.
type committed struct {
	Key    string `json:"key"`
	SHA256 string `json:"sha256"`
	Size   int64  `json:"size"`
}

func refused(status int, reason, detail string) error {
	return httpx.WithReason(status, detail, reason)
}

func notConfigured(detail string) error {
	return refused(http.StatusServiceUnavailable, "not_configured", detail)
}

// s3Store is the S3 store, or the 503 of a store that takes no upload.
func (b *Blobs) s3Store(ctx context.Context) (*Store, error) {
	store, err := loadStore(ctx, b.Pool, b.Keys, b.Env)
	if err != nil {
		return nil, err
	}
	if store.Backend != "s3" {
		return nil, notConfigured("blob uploads need store_backend s3, not " + store.Backend)
	}
	if store.S3 == nil {
		return nil, notConfigured("the S3 store needs an endpoint, a bucket, a prefix and both keys")
	}
	return store, nil
}

func (b *Blobs) begin(r *http.Request, _ crud.Params) (any, error) {
	body, err := crud.ReadBody(r, "kind", "size", "sha256", "media_type")
	if err != nil {
		return nil, err
	}
	kind := body.Match("kind", 1, 64, kindPattern)
	size := body.Int("size", 0)
	sum := body.Match("sha256", 64, 64, sha256Pattern)
	mediaType := body.Match("media_type", 3, 129, mediaTypePattern)
	if err := body.Err(); err != nil {
		return nil, err
	}
	limit, ok := Kinds[kind]
	if !ok {
		return nil, refused(http.StatusUnprocessableEntity, "kind_not_allowed",
			fmt.Sprintf("no service may upload a blob of kind %q", kind))
	}
	if size > limit {
		return nil, refused(http.StatusRequestEntityTooLarge, "upload_too_large",
			fmt.Sprintf("a %s blob is at most %d bytes, not %d", kind, limit, size))
	}
	store, err := b.s3Store(r.Context())
	if err != nil {
		return nil, err
	}
	if err := b.room(r.Context(), store, size); err != nil {
		return nil, err
	}
	now := b.Now()
	t := ticket{
		Caller: crud.ServiceOf(r).Caller, Kind: kind, SHA256: sum, Size: size, MediaType: mediaType,
		Expires: now.Add(urlTTL).Truncate(time.Second).UTC(),
	}
	digest, _ := hex.DecodeString(sum)
	headers := map[string]string{
		"Content-Type":          mediaType,
		"x-amz-acl":             acl,
		"x-amz-checksum-sha256": base64.StdEncoding.EncodeToString(digest),
	}
	signed := map[string]string{"content-length": strconv.FormatInt(size, 10)}
	for name, value := range headers {
		signed[name] = value
	}
	url, err := store.S3.presign("PUT", ObjectKey(store.S3.Prefix, kind, sum), signed, urlTTL, now)
	if err != nil {
		return nil, notConfigured(err.Error())
	}
	return upload{UploadID: t.sign(b.Keys.Current), URL: url, Headers: headers, ExpiresAt: crud.Time(t.Expires)}, nil
}

// room refuses an upload that would take the store past its caps (store/content.py
// `_require_room`): checked, not reserved.
func (b *Blobs) room(ctx context.Context, store *Store, size int64) error {
	count, total, err := usage(ctx, b.Pool, backendS3)
	if err != nil {
		return err
	}
	if store.MaxCount > 0 && count+1 > store.MaxCount {
		return refused(http.StatusInsufficientStorage, "store_full",
			fmt.Sprintf("the store holds %d blobs, the most SCADBUDDY_STORE_MAX_COUNT (%d) allows", count, store.MaxCount))
	}
	if store.MaxTotalBytes > 0 && total+size > store.MaxTotalBytes {
		return refused(http.StatusInsufficientStorage, "store_full",
			fmt.Sprintf("storing %d bytes would take the store past SCADBUDDY_STORE_MAX_TOTAL_BYTES (%d)", size, store.MaxTotalBytes))
	}
	return nil
}

func unknownUpload() error {
	return refused(http.StatusNotFound, "upload_not_found", "no such upload")
}

func (b *Blobs) commit(r *http.Request, p crud.Params) (any, error) {
	id := p["upload_id"]
	if len(id) > 1024 || !uploadIDPattern.MatchString(id) {
		return nil, unknownUpload()
	}
	t, err := openTicket(b.Keys, id)
	if err != nil || t.Caller != crud.ServiceOf(r).Caller {
		return nil, unknownUpload()
	}
	limit, ok := Kinds[t.Kind]
	if !ok {
		return nil, unknownUpload()
	}
	if !b.Now().Before(t.Expires.Add(commitGrace)) {
		return nil, refused(http.StatusGone, "upload_expired", "the upload expired; ask for another")
	}
	store, err := b.s3Store(r.Context())
	if err != nil {
		return nil, err
	}
	objectKey := ObjectKey(store.S3.Prefix, t.Kind, t.SHA256)
	ref := Ref{SHA256: t.SHA256, Kind: t.Kind, Backend: backendS3, BackendID: objectKey, Size: t.Size}
	key := IndexKey(t.Kind, t.SHA256)
	err = pgx.BeginFunc(r.Context(), b.Pool, func(tx pgx.Tx) error {
		// The object's stage lock, shared, until the row commits: no removal takes the object
		// between its check and its row.
		if _, err := tx.Exec(r.Context(), "SELECT set_config('lock_timeout', $1, true)", stageLockTimeout); err != nil {
			return err
		}
		if _, err := tx.Exec(r.Context(), "SELECT pg_advisory_xact_lock_shared(hashtextextended($1, 0))",
			objectLock(backendS3, t.SHA256)); err != nil {
			return err
		}
		if err := b.check(r.Context(), store.S3, objectKey, t, limit); err != nil {
			return err
		}
		before, err := putIn(r.Context(), tx, key, ref)
		if err != nil {
			return err
		}
		if before != nil && (before.Backend != ref.Backend || before.BackendID != ref.BackendID) {
			// No row names it now: the unrecorded sweep removes it on S3; another backend's
			// stays, as Python's release_replaced leaves it.
			slog.WarnContext(r.Context(), "a blob's key moved to another object", "key", key, "backend", before.Backend)
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	return committed{Key: key, SHA256: t.SHA256, Size: t.Size}, nil
}

// check reads the object back, at most limit bytes, and refuses one that is not the size
// and SHA-256 the upload named.
func (b *Blobs) check(ctx context.Context, s3 *S3, objectKey string, t ticket, limit int64) error {
	url, err := s3.presign("GET", objectKey, nil, readTTL, b.Now())
	if err != nil {
		return notConfigured(err.Error())
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return err
	}
	res, err := b.HTTP.Do(req)
	if err != nil {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		// The error names the URL, which carries a signature: say only that it failed.
		return refused(http.StatusServiceUnavailable, "store_unavailable", "the S3 store did not answer")
	}
	defer res.Body.Close()
	switch {
	case res.StatusCode == http.StatusNotFound:
		return refused(http.StatusConflict, "upload_not_received", "nothing was uploaded for this upload")
	case res.StatusCode != http.StatusOK:
		return refused(http.StatusServiceUnavailable, "store_unavailable",
			fmt.Sprintf("the S3 store answered %d", res.StatusCode))
	}
	digest := sha256.New()
	n, err := io.Copy(digest, io.LimitReader(res.Body, limit+1))
	if err != nil {
		if errors.Is(err, context.Canceled) || ctx.Err() != nil {
			return ctx.Err()
		}
		return refused(http.StatusServiceUnavailable, "store_unavailable", "the S3 store's answer broke off")
	}
	if n != t.Size || hex.EncodeToString(digest.Sum(nil)) != t.SHA256 {
		// S3 should have refused bytes that are not the signed checksum. No row names them;
		// the unrecorded sweep removes them.
		return refused(http.StatusUnprocessableEntity, "upload_mismatch",
			"what was uploaded is not the size and sha256 asked for")
	}
	return nil
}
