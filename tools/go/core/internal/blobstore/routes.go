// Package blobstore is the shared CRUD area blobs (#7958): a service stores bytes in the
// blob store by URL, under /api/v1/internal/crud/blobs, with any credential whose scopes
// hold crud.blobs. It asks for an upload, PUTs the bytes to the URL it gets, and commits
// the upload, which indexes the blob. The caller holds no store credential and writes no
// key of the store's layout.
//
// The store is Python's (backend/scadbuddy/store/): the object key and the content index's
// row are the ones its ContentStore writes (ObjectKey, IndexKey, putIn), so Python reads
// what core indexed; backend/tests/fixtures/blob-upload-vectors.json pins both sides. Only
// store_backend s3 takes uploads: the URL is S3's presigned PUT of the upload's own staging
// object (StagingKey), a random key no row names, its length, type, SHA-256 checksum and
// canned ACL signed in. The content key is never presigned. The commit reads the content
// object; when there is none, it reads the staging object, checks its size and SHA-256, and
// PUTs those verified bytes to the content key itself, so a PUT to the staging key after
// the check changes nothing indexed. It never overwrites a content key, as store/s3.py
// `upload` does not: a content object that is not its key's bytes is a 503
// store_object_mismatch, with nothing indexed and the staging object kept. Any other
// backend is a 503 not_configured.
//
// A committed blob of a swept kind (store/content_models.py SWEPT_KINDS) is collected unless
// its caller holds it in blob_refs within the sweep's grace. The commit deletes the staging
// object; one left behind goes with the storefront's staging backstop (StagingKey).
package blobstore

import (
	"bytes"
	"context"
	"crypto/rand"
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
	"sync"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/seal"
	"github.com/eh-homelab/ScadBuddy/tools/go/telemetry"
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
	// readTTL bounds each of the commit's own requests to the store.
	readTTL = time.Minute
	// stageLockTimeout and stageLockTries are how long a commit waits for an object a
	// removal holds, a try, and how many tries (store/index.py STAGE_LOCK_TIMEOUT_MS,
	// STAGE_LOCK_TRIES); then 503 store_busy, as Python's StageLockBusyError.
	stageLockTimeout = 5 * time.Second
	stageLockTries   = 6
	// stageSlots is how many commits may hold a lock connection at once, each its own
	// connection outside the pool (store/index.py STAGE_LOCK_SLOTS).
	stageSlots = 3
	// slotWait is how long a commit waits for one of stageSlots; then 503 store_busy, so
	// no queue grows behind 3 busy slots. Python's semaphore waits unbounded; this is its
	// lock's own budget (stageLockTries × stageLockTimeout, 30 s), so a commit waits for a
	// slot no longer than one slot's holder may wait for the lock.
	slotWait = stageLockTries * stageLockTimeout
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
	noncePattern     = regexp.MustCompile(`^[0-9a-f]{32}$`)
)

// Blobs serves the area.
type Blobs struct {
	Pool *pgxpool.Pool
	Keys *seal.Keys
	// LookupEnv is the process's environment, os.LookupEnv's shape.
	LookupEnv func(string) (string, bool)
	// HTTP sends the commit's requests to the store. Routes traces it with no trace context
	// sent (httpx.Outside); its spans carry the method, the status and the peer "s3", never
	// the URL, whose query is the presigned signature.
	HTTP *http.Client
	Now  func() time.Time
	// LockTimeout, LockTries and SlotWait, when set, replace stageLockTimeout,
	// stageLockTries and slotWait.
	LockTimeout time.Duration
	LockTries   int
	SlotWait    time.Duration

	slotsOnce sync.Once
	slots     chan struct{}
}

// storePeer is the server.address of the store's spans: the endpoint is a setting read at
// each request, and the transport is made once.
const storePeer = "s3"

// Routes are the area's routes. keys may not be nil: an upload's id is MACed under the KEK.
// tel is the process's telemetry, nil for none.
func Routes(pool *pgxpool.Pool, keys *seal.Keys, lookupEnv func(string) (string, bool),
	tel *telemetry.Telemetry) []crud.Route {
	client := &http.Client{Timeout: readTTL, Transport: tel.Transport(httpx.Outside(nil), storePeer)}
	b := &Blobs{Pool: pool, Keys: keys, LookupEnv: lookupEnv, HTTP: client, Now: time.Now}
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

func unavailable(detail string) error {
	return refused(http.StatusServiceUnavailable, "store_unavailable", detail)
}

func busy(detail string) error {
	return refused(http.StatusServiceUnavailable, "store_busy", detail)
}

// s3Store is the S3 store, or the 503 of a store that takes no upload.
func (b *Blobs) s3Store(ctx context.Context) (*Store, error) {
	store, err := loadStore(ctx, b.Pool, b.Keys, b.LookupEnv)
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

// putHeaders are the headers a PUT of t's bytes carries, each signed.
func putHeaders(t ticket) map[string]string {
	digest, _ := hex.DecodeString(t.SHA256)
	return map[string]string{
		"Content-Type":          t.MediaType,
		"x-amz-acl":             acl,
		"x-amz-checksum-sha256": base64.StdEncoding.EncodeToString(digest),
	}
}

// presignPut is the URL that PUTs exactly t's bytes to key.
func (b *Blobs) presignPut(s3 *S3, key string, t ticket, expires time.Duration) (string, map[string]string, error) {
	headers := putHeaders(t)
	signed := map[string]string{"content-length": strconv.FormatInt(t.Size, 10)}
	for name, value := range headers {
		signed[name] = value
	}
	url, err := s3.presign("PUT", key, signed, expires, b.Now())
	return url, headers, err
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
	nonce := make([]byte, 16)
	_, _ = rand.Read(nonce)
	t := ticket{
		Caller: crud.ServiceOf(r).Caller, Kind: kind, SHA256: sum, Size: size, MediaType: mediaType,
		Expires: b.Now().Add(urlTTL).Truncate(time.Second).UTC(), Nonce: hex.EncodeToString(nonce),
	}
	url, headers, err := b.presignPut(store.S3, StagingKey(store.S3.Prefix, t.Nonce), t, urlTTL)
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
	ctx := r.Context()
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
	store, err := b.s3Store(ctx)
	if err != nil {
		return nil, err
	}
	objectKey := ObjectKey(store.S3.Prefix, t.Kind, t.SHA256)
	staging := StagingKey(store.S3.Prefix, t.Nonce)
	ref := Ref{SHA256: t.SHA256, Kind: t.Kind, Backend: backendS3, BackendID: objectKey, Size: t.Size}
	key := IndexKey(t.Kind, t.SHA256)
	// The object's stage lock, shared, from before the content object is written until its
	// row commits: no removal takes the object between them. A session lock on a connection
	// of its own, so no transaction stays open across the store's requests.
	release, err := b.hold(ctx, t.SHA256)
	if err != nil {
		return nil, err
	}
	defer release()
	if err := b.place(ctx, store.S3, objectKey, staging, t, limit); err != nil {
		return nil, err
	}
	err = pgx.BeginFunc(ctx, b.Pool, func(tx pgx.Tx) error {
		before, err := index(ctx, tx, key, ref, true)
		if err != nil {
			return err
		}
		if before != nil && (before.Backend != ref.Backend || before.BackendID != ref.BackendID) {
			// No row names it now: the unrecorded sweep removes it on S3; another backend's
			// stays, as Python's release_replaced leaves it.
			slog.WarnContext(ctx, "a blob's key moved to another object", "key", key, "backend", before.Backend)
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	release()
	b.remove(ctx, store.S3, staging)
	return committed{Key: key, SHA256: t.SHA256, Size: t.Size}, nil
}

// hold takes the stage lock of the objects of sha256 shared (store/index.py `StageHold`,
// `_lock_shared`), on a connection of its own outside the pool, once one of stageSlots is
// free (at most slotWait; then 503 store_busy). Each try is a transaction whose `set_config(…, true)` ends with it, so the lock
// timeout does not outlive the try; the session lock outlives the commit. A removal that
// holds the lock through every try is a 503 store_busy. release closes the connection,
// which drops the lock; it may be called more than once.
func (b *Blobs) hold(ctx context.Context, sha256 string) (release func(), err error) {
	b.slotsOnce.Do(func() { b.slots = make(chan struct{}, stageSlots) })
	wait := b.SlotWait
	if wait <= 0 {
		wait = slotWait
	}
	timer := time.NewTimer(wait)
	defer timer.Stop()
	select {
	case b.slots <- struct{}{}:
	case <-timer.C:
		return nil, busy("every commit slot stayed busy; commit the upload again")
	case <-ctx.Done():
		return nil, ctx.Err()
	}
	conn, err := pgx.ConnectConfig(ctx, b.Pool.Config().ConnConfig.Copy())
	if err != nil {
		<-b.slots
		return nil, err
	}
	var once sync.Once
	release = func() {
		once.Do(func() {
			closing, cancel := context.WithTimeout(context.WithoutCancel(ctx), 5*time.Second)
			defer cancel()
			_ = conn.Close(closing)
			<-b.slots
		})
	}
	timeout, tries := b.LockTimeout, b.LockTries
	if timeout <= 0 {
		timeout = stageLockTimeout
	}
	if tries <= 0 {
		tries = stageLockTries
	}
	name := objectLock(backendS3, sha256)
	for range tries {
		err = pgx.BeginFunc(ctx, conn, func(tx pgx.Tx) error {
			if _, err := tx.Exec(ctx, "SELECT set_config('lock_timeout', $1, true)",
				strconv.FormatInt(timeout.Milliseconds(), 10)+"ms"); err != nil {
				return err
			}
			_, err := tx.Exec(ctx, "SELECT pg_advisory_lock_shared(hashtextextended($1, 0))", name)
			return err
		})
		if err == nil {
			return release, nil
		}
		var pg *pgconn.PgError
		if !errors.As(err, &pg) || pg.Code != "55P03" {
			release()
			return nil, err
		}
		slog.InfoContext(ctx, "an object's stage lock is claimed by a removal; trying again")
	}
	release()
	return nil, busy("a removal held the blob's object for every try; commit the upload again")
}

// matches says whether body is t's bytes.
func matches(body []byte, t ticket) bool {
	sum := sha256.Sum256(body)
	return int64(len(body)) == t.Size && hex.EncodeToString(sum[:]) == t.SHA256
}

// place makes the content object hold t's bytes. A content object that is there already
// is never written (store/s3.py `upload`); one that is not t's bytes is a 503
// store_object_mismatch. Otherwise the staging object is read, checked, and its verified
// bytes, not the staging object, are PUT to the content key. On every refusal the staging
// object stays, so the commit may be sent again.
func (b *Blobs) place(ctx context.Context, s3 *S3, objectKey, staging string, t ticket, limit int64) error {
	body, found, err := b.read(ctx, s3, objectKey, limit)
	if err != nil {
		return err
	}
	if found {
		return stored(ctx, body, t)
	}
	body, found, err = b.read(ctx, s3, staging, limit)
	switch {
	case err != nil:
		return err
	case !found:
		// Another commit of this upload may have written the content object and deleted
		// the staging object since the first read: read the content object again.
		body, found, err = b.read(ctx, s3, objectKey, limit)
		if err != nil {
			return err
		}
		if found {
			return stored(ctx, body, t)
		}
		return refused(http.StatusConflict, "upload_not_received", "nothing was uploaded for this upload")
	case !matches(body, t):
		// S3 should have refused bytes that are not the signed checksum. The staging
		// object stays for a commit after a PUT of the right bytes, or the staging sweep.
		return refused(http.StatusUnprocessableEntity, "upload_mismatch",
			"what was uploaded is not the size and sha256 asked for")
	}
	url, headers, err := b.presignPut(s3, objectKey, t, readTTL)
	if err != nil {
		return notConfigured(err.Error())
	}
	res, err := b.send(ctx, http.MethodPut, url, body, headers)
	if err != nil {
		return err
	}
	res.Body.Close()
	if res.StatusCode != http.StatusOK {
		return unavailable(fmt.Sprintf("the S3 store answered %d to the blob's PUT", res.StatusCode))
	}
	return nil
}

// stored refuses a content object that is not t's bytes: the store's object is wrong, and
// the commit neither overwrites it nor indexes it, as Python's `_verified` (#6233) refuses
// a ref whose object is not its SHA-256 and size and writes no row. A row that named it
// before stays as it is.
func stored(ctx context.Context, body []byte, t ticket) error {
	if matches(body, t) {
		return nil
	}
	slog.WarnContext(ctx, "the store's object is not the bytes its key names; it is left as it is",
		"kind", t.Kind, "sha256", t.SHA256)
	return refused(http.StatusServiceUnavailable, "store_object_mismatch",
		"the store's object at this blob's key is not its bytes; nothing was indexed")
}

// send is one request to the store. Its error never names the URL, which carries a signature.
func (b *Blobs) send(ctx context.Context, method, url string, body []byte, headers map[string]string) (*http.Response, error) {
	var reader io.Reader
	if body != nil {
		reader = bytes.NewReader(body)
	}
	req, err := http.NewRequestWithContext(ctx, method, url, reader)
	if err != nil {
		return nil, unavailable("the S3 store's URL is not a URL")
	}
	for name, value := range headers {
		req.Header.Set(name, value)
	}
	res, err := b.HTTP.Do(req)
	if err != nil {
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
		return nil, unavailable("the S3 store did not answer")
	}
	return res, nil
}

// read is the object at key, at most limit+1 bytes of it; found is false when there is none.
func (b *Blobs) read(ctx context.Context, s3 *S3, key string, limit int64) (body []byte, found bool, err error) {
	url, err := s3.presign("GET", key, nil, readTTL, b.Now())
	if err != nil {
		return nil, false, notConfigured(err.Error())
	}
	res, err := b.send(ctx, http.MethodGet, url, nil, nil)
	if err != nil {
		return nil, false, err
	}
	defer res.Body.Close()
	switch {
	case res.StatusCode == http.StatusNotFound:
		return nil, false, nil
	case res.StatusCode != http.StatusOK:
		return nil, false, unavailable(fmt.Sprintf("the S3 store answered %d", res.StatusCode))
	}
	body, err = io.ReadAll(io.LimitReader(res.Body, limit+1))
	if err != nil {
		if ctx.Err() != nil {
			return nil, false, ctx.Err()
		}
		return nil, false, unavailable("the S3 store's answer broke off")
	}
	return body, true, nil
}

// remove deletes the upload's staging object. One it cannot delete is logged and left to
// the staging sweep (StagingKey): the blob is indexed already.
func (b *Blobs) remove(ctx context.Context, s3 *S3, staging string) {
	url, err := s3.presign("DELETE", staging, nil, readTTL, b.Now())
	if err == nil {
		var res *http.Response
		if res, err = b.send(ctx, http.MethodDelete, url, nil, nil); err == nil {
			res.Body.Close()
			if res.StatusCode/100 != 2 && res.StatusCode != http.StatusNotFound {
				err = fmt.Errorf("the S3 store answered %d", res.StatusCode)
			}
		}
	}
	if err != nil {
		slog.WarnContext(ctx, "could not delete an upload's staging object; the staging sweep removes it", "err", err)
	}
}
