package packages

// Area package-mirrors (#6935, #7907) and a hosted version's zip (#6932): the routes of
// backend/scadbuddy/api/crud_agent_packages.py that read or write the blob store, over
// agent_data/package_mirrors.py. The blob store is the Blobs below; core wires it in main.go.

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"regexp"
	"strconv"
	"strings"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
)

const (
	// MirrorArea is the area's name in SCADBUDDY_CORE_AREAS.
	MirrorArea = "package-mirrors"
	// MaxMirrorBytes is agent_data/package_mirrors.py MAX_MIRROR_BYTES (the agent's
	// MIRROR_MAX_BYTES): the largest mirror.
	MaxMirrorBytes = 24 * 1024 * 1024
	// MirrorFile is package_mirrors.py BLOB_FILE (and orders/attachments.py's): the one file
	// in a mirror's or an attachment's blob.
	MirrorFile = "file"

	mirrorNotFound        = "mirror-not-found"
	hostedVersionNotFound = "hosted-version-not-found"
	blobMissing           = "blob-missing" // hosted_plugins/blobs.py BLOB_MISSING

	// zipSegment is the version's path segment, "{version}.zip" as Starlette's template
	// writes it, so the span's http.route is Python's. The router names the parameter by
	// the whole segment.
	zipSegment = "{version}.zip"
	zipParam   = "version}.zip"
)

// tooLarge is main.py's BodySizeGate refusal with api/crud_agent_packages.py
// MIRROR_ROUTE_LIMIT, which answers before the route reads the body, declared or streamed.
var tooLarge = fmt.Sprintf("a plugin package's archive is at most %d MB "+
	"(fixed, agent_data/package_mirrors.py MAX_MIRROR_BYTES), and this one is larger",
	MaxMirrorBytes/(1024*1024))

var digitsPattern = regexp.MustCompile(`^[0-9]+$`)

// ErrStale is store/cache.py StaleBlobError: the key named other bytes when the publish
// tried to index it.
var ErrStale = errors.New("the blob's key was published by another")

// Blobs is the blob store as the API's process runs it (store/cache.py CachedBlobStore over
// its content store). An error that is an *httpx.Problem is answered as it is (a store out of
// reach as 503 store_unavailable, say); any other is a 500.
type Blobs interface {
	// Claim is CachedBlobStore.claim: key's row touched first, then whether the store holds
	// key's object on its backend (a HEAD, never a download). A row whose object is lost is
	// forgotten, and answers false.
	Claim(ctx context.Context, key string) (bool, error)
	// PublishFile is package_mirrors.py `_place` then `publish_fresh(key, scope=BlobScope(),
	// expected=None)`: file, whole, as the one file name of the blob key, packed as
	// store/archive.py pack_dir packs a directory (one ZIP_DEFLATED entry, dated 1980-01-01,
	// mode 0644), stored as kind "piece" under the name "piece-<key>.zip" and indexed only
	// while key names no blob. ErrStale when it named one by then.
	PublishFile(ctx context.Context, key, name string, file io.ReadSeeker) error
	// ReadFile is CachedBlobStore.read_file: the file name of the blob key, whole. found is
	// false when the store holds no blob key on its backend, or the blob holds no such file; a
	// lost or altered object is forgotten and not found. A file of more than max bytes is
	// refused (blobstore.ErrTooLarge), since it is held in memory.
	ReadFile(ctx context.Context, key, name string, max int64) (data []byte, found bool, err error)
}

// Mirrors is the routes that need the blob store.
type Mirrors struct {
	Pool  *pgxpool.Pool
	Blobs Blobs
}

// MirrorAreas are area package-mirrors' routes over blobs: the mirrors' POST and GET, and a
// hosted version's zip.
func MirrorAreas(pool *pgxpool.Pool, blobs Blobs) map[string][]crud.Route {
	m := &Mirrors{Pool: pool, Blobs: blobs}
	return map[string][]crud.Route{MirrorArea: m.Routes()}
}

// Routes are the area's routes.
func (m *Mirrors) Routes() []crud.Route {
	return []crud.Route{
		// No MaxBody: Python's gate takes a body with no Content-Length and counts it.
		{Method: "POST", Path: "/package-mirrors/{name}", Handle: m.post},
		{Method: "GET", Path: "/package-mirrors/{name}/{sha256}", Handle: m.get},
		{Method: "GET", Path: "/hosted-plugins/{name}/versions/" + zipSegment, Handle: m.hostedZip},
	}
}

// mirrorKey is package_mirrors.py blob_key.
func mirrorKey(sha256 string) string { return "plugin-mirror-" + sha256 }

// attachmentKey is orders/attachments.py blob_key: a hosted plugin's bytes.
func attachmentKey(sha256 string) string { return "attachment-" + sha256 }

// holderID is package_mirrors.py holder_id: a package name never holds a "/".
func holderID(name, sha256 string) string { return name + "/" + sha256 }

func contentTooLarge() error {
	return &httpx.Problem{Status: http.StatusRequestEntityTooLarge, Detail: tooLarge}
}

// post is crud_agent_package_mirror_post: the raw bytes of a file the agent fetched for the
// package name, spooled and hashed as they stream, kept under their sha256 and held for
// name, and every hold no row names past its grace released, in one transaction.
func (m *Mirrors) post(r *http.Request, params crud.Params) (any, error) {
	name, err := params.Match("name", namePattern)
	if err != nil {
		return nil, err
	}
	if r.ContentLength > MaxMirrorBytes {
		return nil, contentTooLarge()
	}
	spool, err := os.CreateTemp("", "package-mirror-*")
	if err != nil {
		return nil, err
	}
	defer func() {
		_ = spool.Close()
		_ = os.Remove(spool.Name())
	}()
	digest := sha256.New()
	n, err := io.Copy(io.MultiWriter(spool, digest), io.LimitReader(r.Body, MaxMirrorBytes+1))
	if err != nil {
		return nil, err
	}
	if n > MaxMirrorBytes {
		return nil, contentTooLarge()
	}
	sum := hex.EncodeToString(digest.Sum(nil))
	ctx := r.Context()
	// The blob is published before the hold's transaction. If that transaction fails, the
	// blob has no hold, and the blob sweep removes it once its grace runs out.
	if err := keep(ctx, m.Blobs, spool, sum); err != nil {
		return nil, err
	}
	err = pgx.BeginFunc(ctx, m.Pool, func(tx pgx.Tx) error {
		if err := hold(ctx, tx, name, sum); err != nil {
			return err
		}
		return releaseUnnamed(ctx, tx)
	})
	if err != nil {
		return nil, err
	}
	return struct {
		BytesSHA256 string `json:"bytes_sha256"`
	}{sum}, nil
}

// keep is package_mirrors.py keep: bytes the store holds already are its blob as it is
// (Claim touches it, so the sweep keeps it until the hold commits); else file is published.
func keep(ctx context.Context, blobs Blobs, file io.ReadSeeker, sha256 string) error {
	key := mirrorKey(sha256)
	if held, err := blobs.Claim(ctx, key); err != nil || held {
		return err
	}
	err := blobs.PublishFile(ctx, key, MirrorFile, file)
	if errors.Is(err, ErrStale) {
		held, claimErr := blobs.Claim(ctx, key)
		if claimErr != nil {
			return claimErr
		}
		if held {
			return nil
		}
	}
	return err
}

// hold is package_mirrors.py hold: the package name's hold of the mirror, its grace counted
// from now.
func hold(ctx context.Context, tx pgx.Tx, name, sha256 string) error {
	_, err := tx.Exec(ctx, `INSERT INTO blob_refs (key, holder_kind, holder_id) VALUES ($1, $2, $3)
 ON CONFLICT (key, holder_kind, holder_id) DO UPDATE SET created_at = now()`,
		mirrorKey(sha256), mirrorHolder, holderID(name, sha256))
	return err
}

// get is crud_agent_package_mirror: the kept bytes of sha256, only while the package name's
// pin or re-pin names them (a bytes pin's file, or a commit pin's recorded zip, #7907).
func (m *Mirrors) get(r *http.Request, params crud.Params) (any, error) {
	name, err := params.Match("name", namePattern)
	if err != nil {
		return nil, err
	}
	sum, err := params.Match("sha256", sha256Pattern)
	if err != nil {
		return nil, err
	}
	notFound := httpx.WithReason(http.StatusNotFound,
		fmt.Sprintf("no mirror of the bytes %s is kept for %s", sum, name), mirrorNotFound)
	ctx := r.Context()
	named := false
	err = pgx.BeginFunc(ctx, m.Pool, func(tx pgx.Tx) error {
		var one int
		err := tx.QueryRow(ctx, `SELECT 1 FROM ai_plugin_packages WHERE name = $1
 AND $2 IN (bytes_sha256, pending_bytes_sha256, mirror_sha256, pending_mirror_sha256)`,
			name, sum).Scan(&one)
		if errors.Is(err, pgx.ErrNoRows) {
			return nil
		}
		named = err == nil
		return err
	})
	if err != nil {
		return nil, err
	}
	if !named {
		return nil, notFound
	}
	data, found, err := m.Blobs.ReadFile(ctx, mirrorKey(sum), MirrorFile, MaxMirrorBytes)
	if err != nil {
		return nil, err
	}
	if !found {
		return nil, notFound
	}
	return &crud.Reply{Status: http.StatusOK, MediaType: "application/octet-stream", Body: data}, nil
}

// hostedZip is crud_agent_hosted_plugin_zip: a published version's zip, as published, its
// sha256 and content hash in the headers.
func (m *Mirrors) hostedZip(r *http.Request, params crud.Params) (any, error) {
	name, err := params.Match("name", namePattern)
	if err != nil {
		return nil, err
	}
	segment := params[zipParam]
	text, isZip := strings.CutSuffix(segment, ".zip")
	if !isZip {
		return nil, &httpx.Problem{Status: http.StatusNotFound, Detail: "Not Found"}
	}
	version, err := zipVersion(text)
	if err != nil {
		return nil, err
	}
	ctx := r.Context()
	var zipSHA256, contentHash string
	err = pgx.BeginFunc(ctx, m.Pool, func(tx pgx.Tx) error {
		var id int64
		err := tx.QueryRow(ctx, `SELECT id FROM ai_hosted_plugins WHERE name = $1`, name).Scan(&id)
		if err == nil {
			err = tx.QueryRow(ctx, `SELECT zip_sha256, content_hash FROM ai_hosted_plugin_versions
 WHERE plugin_id = $1 AND version = $2`, id, version).Scan(&zipSHA256, &contentHash)
		}
		if errors.Is(err, pgx.ErrNoRows) {
			return httpx.WithReason(http.StatusNotFound,
				fmt.Sprintf(`the hosted plugin "%s" has no version %d`, name, version), hostedVersionNotFound)
		}
		return err
	})
	if err != nil {
		return nil, err
	}
	data, found, err := m.Blobs.ReadFile(ctx, attachmentKey(zipSHA256), MirrorFile, MaxMirrorBytes)
	if err != nil {
		return nil, err
	}
	if !found {
		return nil, httpx.WithReason(http.StatusInternalServerError,
			"the store has lost a file's bytes", blobMissing)
	}
	if got := sha256.Sum256(data); hex.EncodeToString(got[:]) != zipSHA256 {
		return nil, httpx.WithReason(http.StatusInternalServerError,
			"a file's bytes in the store do not match their sha256", blobMissing)
	}
	return &crud.Reply{
		Status: http.StatusOK, MediaType: "application/zip", Body: data,
		Headers: map[string]string{
			"X-ScadBuddy-Zip-Sha256":   zipSHA256,
			"X-ScadBuddy-Content-Hash": contentHash,
		},
	}, nil
}

// zipVersion is the path's version, an integer of at least 1 (Path(ge=1)).
func zipVersion(text string) (int64, error) {
	issue := func(msg string) error {
		return httpx.Invalid(httpx.Issue{Loc: []string{"path", "version"}, Msg: msg})
	}
	if !digitsPattern.MatchString(text) {
		return 0, issue("Input should be a valid integer, unable to parse string as an integer")
	}
	version, err := strconv.ParseInt(text, 10, 64)
	if err != nil {
		return 0, issue("Input should be a valid integer, unable to parse string as an integer")
	}
	if version < 1 {
		return 0, issue("Input should be greater than or equal to 1")
	}
	return version, nil
}
