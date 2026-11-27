package blobstore

// The store in process, for a handler in core (#8137: area package-mirrors): Claim,
// PublishFile and ReadFile are store/cache.py CachedBlobStore's claim, publish_fresh with
// expected None, and read_file, over the S3 store only. Their errors are *httpx.Problem
// refusals (503 not_configured, store_unavailable, store_busy; 507 store_full), ErrStale,
// or a wrapped error.

import (
	"archive/zip"
	"bytes"
	"compress/flate"
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/binary"
	"encoding/hex"
	"errors"
	"fmt"
	"hash/crc32"
	"io"
	"net/http"
	"os"
	"strconv"
	"time"

	"github.com/jackc/pgx/v5"
)

// ErrStale is store/cache.py StaleBlobError: the key named a blob when the publish went to
// index it.
var ErrStale = errors.New("blobstore: the key names another blob")

// ErrTooLarge is a ReadFile refused because the file, or the blob that holds it, is larger than
// the bound its caller gave.
var ErrTooLarge = errors.New("blobstore: the file is larger than its reader takes")

// pieceKind is the kind every published directory is stored as (store/cache.py `_publish`).
const pieceKind = "piece"

// zip64Limit is Python's zipfile.ZIP64_LIMIT: pack_dir refuses a file past it.
const zip64Limit = 1<<31 - 1

// Claim is CachedBlobStore.claim: key's row touched first, so a sweep's `delete_if_stale`
// after it finds the row fresh; then whether the row is this store's and the store holds its
// object (a HEAD, never a download). A row whose object is lost is forgotten, and false.
func (b *Blobs) Claim(ctx context.Context, key string) (bool, error) {
	store, err := b.s3Store(ctx)
	if err != nil {
		return false, err
	}
	if _, err := b.Pool.Exec(ctx, "UPDATE store_blobs SET touched_at = now() WHERE key = $1", key); err != nil {
		return false, err
	}
	ref, found, err := b.row(ctx, key)
	if err != nil || !found || ref.Backend != backendS3 {
		return false, err
	}
	held, err := b.exists(ctx, store.S3, ref.BackendID)
	if err != nil || held {
		return held, err
	}
	return false, b.forget(ctx, key)
}

// ReadFile is CachedBlobStore.read_file: the file name of the blob key, whole. found is
// false when no row of this store names key, the store has lost its object or holds other
// bytes there (the row is then forgotten), or the blob has no such file. The blob is held in
// memory, and Python sets kind piece no size limit, so the caller bounds it: a file of more
// than max bytes, or a stored zip larger than one holding such a file can be, is ErrTooLarge
// (the zip is refused from its row, before a byte is read).
func (b *Blobs) ReadFile(ctx context.Context, key, name string, max int64) ([]byte, bool, error) {
	if _, err := b.s3Store(ctx); err != nil {
		return nil, false, err
	}
	rc, ref, err := b.OpenKey(ctx, key)
	switch {
	case errors.Is(err, ErrNotConfigured):
		// The store is S3, so the row is another backend's: not this store's blob.
		return nil, false, nil
	case errors.Is(err, ErrBlobNotFound):
		// No row, or no object: Python forgets the key when its object is gone.
		return nil, false, b.forget(ctx, key)
	case err != nil:
		return nil, false, err
	}
	if most := packedMax(max, name); ref.Size > most {
		rc.Close()
		return nil, false, fmt.Errorf("%w: the blob %s is %d bytes, and a zip of a file of at most %d bytes is at most %d",
			ErrTooLarge, key, ref.Size, max, most)
	}
	archive, err := io.ReadAll(rc)
	rc.Close()
	if errors.Is(err, ErrBlobMismatch) {
		return nil, false, b.forget(ctx, key)
	}
	if err != nil {
		return nil, false, err
	}
	packed, err := zip.NewReader(bytes.NewReader(archive), int64(len(archive)))
	if err != nil {
		return nil, false, fmt.Errorf("blobstore: the blob %s is not a zip: %w", key, err)
	}
	for _, f := range packed.File {
		if f.Name != name {
			continue
		}
		if f.UncompressedSize64 > uint64(max) {
			return nil, false, fmt.Errorf("%w: the file %q of the blob %s is %d bytes, at most %d",
				ErrTooLarge, name, key, f.UncompressedSize64, max)
		}
		entry, err := f.Open()
		if err != nil {
			return nil, false, err
		}
		defer entry.Close()
		data, err := io.ReadAll(io.LimitReader(entry, max+1))
		if err == nil && int64(len(data)) > max {
			return nil, false, fmt.Errorf("%w: the file %q of the blob %s is more than %d bytes",
				ErrTooLarge, name, key, max)
		}
		return data, err == nil, err
	}
	return nil, false, nil
}

// packedMax is the most bytes a zip of one file name of at most max bytes takes, as packFile
// or Python's zipfile packs it: deflate's stored blocks (5 bytes per 65,535 when the bytes do
// not compress), the local header, the central directory and room for extra fields.
func packedMax(max int64, name string) int64 {
	return max + (max/65535+1)*5 + 2*int64(len(name)) + 64<<10
}

// PublishFile is package_mirrors.py `_place` then CachedBlobStore.publish_fresh(key,
// expected=None): file, whole, as the one file name of the blob key, packed as
// store/archive.py pack_dir packs a directory (packFile), stored as kind piece (Python names
// it "piece-<key>.zip", a name S3 never sees), and indexed only while key names no blob;
// ErrStale when it named one by then, and this call's own object is removed unless a row
// names it. Like ContentStore.replace it reuses an object of the same bytes that another row
// names, and checks the store's caps only when it uploads. Unlike replace, it holds the
// object's stage lock (hold) from before the lookup until the row is written, as a commit
// does, so no removal takes the object between them.
func (b *Blobs) PublishFile(ctx context.Context, key, name string, file io.ReadSeeker) error {
	store, err := b.s3Store(ctx)
	if err != nil {
		return err
	}
	spool, err := os.CreateTemp("", "blob-publish-*")
	if err != nil {
		return err
	}
	defer func() {
		_ = spool.Close()
		_ = os.Remove(spool.Name())
	}()
	if _, err := file.Seek(0, io.SeekStart); err != nil {
		return err
	}
	if _, err := packFile(spool, name, file); err != nil {
		return err
	}
	landed, err := b.put(ctx, store, pieceKind, "", spool, func(ctx context.Context, ref Ref) (bool, error) {
		tag, err := b.Pool.Exec(ctx, `INSERT INTO store_blobs (sha256, kind, backend, backend_id, size, slug, meta, key)
 VALUES ($1, $2, $3, $4, $5, NULL, '{}'::jsonb, $6) ON CONFLICT (key) DO NOTHING`,
			ref.SHA256, ref.Kind, ref.Backend, ref.BackendID, ref.Size, key)
		return err == nil && tag.RowsAffected() == 1, err
	})
	if err == nil && !landed {
		return ErrStale
	}
	return err
}

// indexer writes the rows of a stored object, in its own transaction or statement, and
// says whether it wrote one: false when the row it would write is another's (a stale swap).
type indexer func(ctx context.Context, ref Ref) (bool, error)

// put is ContentStore.put and `replace` up to their index call, for any kind: body (a
// spooled file) hashed, its object's stage lock held shared (hold) from before the lookup
// until index has written its rows, the object stored or reused (stage), then index run.
// When index writes nothing, the object goes unless a row names it (removeUnnamed, Python's
// `_release`), once this call's own hold is dropped. It answers what index answered.
// It is core's one in-process write path (#8250): Put, PublishFile and PutContent call it;
// the commit route, whose bytes come from a staging object, indexes through index/putIn.
// mediaType, when set, is the PUT's Content-Type, and the object is PUT private, as a
// commit PUTs it.
func (b *Blobs) put(ctx context.Context, store *Store, kind, mediaType string, body *os.File, index indexer) (bool, error) {
	sha, size, err := digest(body)
	if err != nil {
		return false, err
	}
	release, err := b.hold(ctx, sha)
	if err != nil {
		return false, err
	}
	defer release()
	ref, err := b.stage(ctx, store, kind, mediaType, sha, size, body)
	if err != nil {
		return false, err
	}
	landed, err := index(ctx, ref)
	if err != nil || landed {
		return landed, err
	}
	release()
	return false, b.removeUnnamed(ctx, store.S3, ref)
}

// stage is ContentStore._store for kind's blob of sha256 and size: an object of the same
// bytes and kind a row names and the store holds is reused; otherwise the caps are checked
// (room, which runs no aggregate with no cap set) and the bytes PUT, unless the content
// object is there already (store/s3.py `upload`). Unlike Python, which trusts an object it
// finds, every object stage would use is read and hashed first: one that is not sha256's
// bytes is a 409 store_object_mismatch, left as it is and never indexed (held), for every
// caller (#8250).
func (b *Blobs) stage(ctx context.Context, store *Store, kind, mediaType, sha string, size int64, body *os.File) (Ref, error) {
	var existing Ref
	err := b.Pool.QueryRow(ctx, `SELECT sha256, kind, backend, backend_id, size FROM store_blobs
 WHERE sha256 = $1 AND kind = $2 AND backend = $3 LIMIT 1`, sha, kind, backendS3).
		Scan(&existing.SHA256, &existing.Kind, &existing.Backend, &existing.BackendID, &existing.Size)
	switch {
	case err == nil:
		there, err := b.held(ctx, store.S3, kind, existing.BackendID, sha, size)
		if err != nil {
			return Ref{}, err
		}
		if there {
			return existing, nil
		}
	case !errors.Is(err, pgx.ErrNoRows):
		return Ref{}, err
	}
	if err := b.room(ctx, store, size); err != nil {
		return Ref{}, err
	}
	objectKey := ObjectKey(store.S3.Prefix, kind, sha)
	ref := Ref{SHA256: sha, Kind: kind, Backend: backendS3, BackendID: objectKey, Size: size}
	there, err := b.held(ctx, store.S3, kind, objectKey, sha, size)
	if err != nil || there {
		return ref, err
	}
	sum, _ := hex.DecodeString(sha)
	headers := map[string]string{"x-amz-checksum-sha256": base64.StdEncoding.EncodeToString(sum)}
	if mediaType != "" {
		headers["Content-Type"] = mediaType
		headers["x-amz-acl"] = acl
	}
	signed := map[string]string{"content-length": strconv.FormatInt(size, 10)}
	for k, v := range headers {
		signed[k] = v
	}
	url, err := store.S3.presign(http.MethodPut, objectKey, signed, readTTL, b.Now())
	if err != nil {
		return Ref{}, notConfigured(err.Error())
	}
	if _, err := body.Seek(0, io.SeekStart); err != nil {
		return Ref{}, err
	}
	res, err := doBody(ctx, b.Stream, http.MethodPut, url, body, size, headers)
	if err != nil {
		return Ref{}, err
	}
	res.Body.Close()
	if res.StatusCode != http.StatusOK {
		return Ref{}, unavailable(fmt.Sprintf("the S3 store answered %d to the blob's PUT", res.StatusCode))
	}
	return ref, nil
}

// held is whether the store holds objectKey as kind's blob of sha256 and size, read whole
// and hashed as it streams (fetch): false when there is no object, and a 409
// store_object_mismatch (mismatched) when the object is other bytes.
func (b *Blobs) held(ctx context.Context, s3 *S3, kind, objectKey, sha string, size int64) (bool, error) {
	rc, err := b.fetch(ctx, s3, Ref{SHA256: sha, Kind: kind, Backend: backendS3, BackendID: objectKey, Size: size})
	switch {
	case errors.Is(err, ErrBlobNotFound):
		return false, nil
	case errors.Is(err, ErrNotConfigured):
		return false, notConfigured(err.Error())
	case err != nil:
		return false, storeFailed(ctx, err)
	}
	defer rc.Close()
	_, err = io.Copy(io.Discard, rc)
	switch {
	case errors.Is(err, ErrBlobMismatch):
		return false, mismatched(ctx, kind, sha)
	case err != nil:
		return false, storeFailed(ctx, err)
	}
	return true, nil
}

// storeFailed is err as a 503 store_unavailable, unless it is a refusal already or the
// caller's ctx ended. err never names a URL (fetch's errors do not).
func storeFailed(ctx context.Context, err error) error {
	if Reason(err) != "" || ctx.Err() != nil {
		return err
	}
	return unavailable(err.Error())
}

// removeUnnamed is ContentStore._release over the index's `claim_unnamed`: ref's object is
// removed only while this call holds its lock exclusively (never waiting for it) and no row
// names it, as its own or as its migrated copy.
func (b *Blobs) removeUnnamed(ctx context.Context, s3 *S3, ref Ref) error {
	conn, err := b.Pool.Acquire(ctx)
	if err != nil {
		return err
	}
	defer conn.Release()
	name := objectLock(backendS3, ref.SHA256)
	var got bool
	if err := conn.QueryRow(ctx, "SELECT pg_try_advisory_lock(hashtextextended($1, 0))", name).Scan(&got); err != nil || !got {
		return err
	}
	defer func() {
		unlocking, cancel := context.WithTimeout(context.WithoutCancel(ctx), 5*time.Second)
		defer cancel()
		if _, err := conn.Exec(unlocking, "SELECT pg_advisory_unlock(hashtextextended($1, 0))", name); err != nil {
			// A closed session holds no lock; the pool discards a closed connection.
			_ = conn.Conn().Close(unlocking)
		}
	}()
	var named bool
	err = conn.QueryRow(ctx, `SELECT EXISTS (SELECT 1 FROM store_blobs
 WHERE (backend, backend_id) = ($1, $2) OR (migrated_backend, migrated_backend_id) = ($1, $2))`,
		ref.Backend, ref.BackendID).Scan(&named)
	if err != nil || named {
		return err
	}
	url, err := s3.presign(http.MethodDelete, ref.BackendID, nil, readTTL, b.Now())
	if err != nil {
		return notConfigured(err.Error())
	}
	res, err := b.send(ctx, http.MethodDelete, url, nil, nil)
	if err != nil {
		return err
	}
	res.Body.Close()
	if res.StatusCode/100 != 2 && res.StatusCode != http.StatusNotFound {
		return unavailable(fmt.Sprintf("the S3 store answered %d to the blob's DELETE", res.StatusCode))
	}
	return nil
}

// row is key's row.
func (b *Blobs) row(ctx context.Context, key string) (Ref, bool, error) {
	var ref Ref
	err := b.Pool.QueryRow(ctx, "SELECT sha256, kind, backend, backend_id, size FROM store_blobs WHERE key = $1", key).
		Scan(&ref.SHA256, &ref.Kind, &ref.Backend, &ref.BackendID, &ref.Size)
	if errors.Is(err, pgx.ErrNoRows) {
		return Ref{}, false, nil
	}
	return ref, err == nil, err
}

// forget is ContentStore.forget: key's row dropped, whichever backend it names; the object
// stays.
func (b *Blobs) forget(ctx context.Context, key string) error {
	_, err := b.Pool.Exec(ctx, "DELETE FROM store_blobs WHERE key = $1", key)
	return err
}

// exists is store/s3.py `exists`: a HEAD of the object.
func (b *Blobs) exists(ctx context.Context, s3 *S3, objectKey string) (bool, error) {
	url, err := s3.presign(http.MethodHead, objectKey, nil, readTTL, b.Now())
	if err != nil {
		return false, notConfigured(err.Error())
	}
	res, err := b.send(ctx, http.MethodHead, url, nil, nil)
	if err != nil {
		return false, err
	}
	res.Body.Close()
	switch res.StatusCode {
	case http.StatusOK:
		return true, nil
	case http.StatusNotFound:
		return false, nil
	}
	return false, unavailable(fmt.Sprintf("the S3 store answered %d to a HEAD", res.StatusCode))
}

// digest is the SHA-256 and size of f's bytes.
func digest(f *os.File) (string, int64, error) {
	if _, err := f.Seek(0, io.SeekStart); err != nil {
		return "", 0, err
	}
	h := sha256.New()
	n, err := io.Copy(h, f)
	return hex.EncodeToString(h.Sum(nil)), n, err
}

// zipEntry is the one entry of a packed file.
type zipEntry struct {
	name  string
	crc   uint32
	size  int64
	csize int64
}

// flags is the entry's general purpose bits, as zipfile's `_encodeFilenameFlags`: UTF-8
// (0x800) for a name that is not ASCII, and no data descriptor (the file is seekable).
func (e zipEntry) flags() uint16 {
	for i := 0; i < len(e.name); i++ {
		if e.name[i] >= 0x80 {
			return 0x800
		}
	}
	return 0
}

const (
	zipVersion = 20     // zipfile.DEFAULT_VERSION: what deflate needs
	zipMadeBy  = 0x0314 // create_system 3 (Unix), create_version 20
	zipDate    = 0x0021 // 1980-01-01 in DOS form; the time is 00:00:00, 0
	zipAttrs   = 0o644 << 16
)

// localHeader is the entry's local file header, as zipfile writes it once the entry is
// closed (it seeks back over the first one).
func localHeader(e zipEntry) []byte {
	b := make([]byte, 0, 30+len(e.name))
	b = binary.LittleEndian.AppendUint32(b, 0x04034b50)
	b = binary.LittleEndian.AppendUint16(b, zipVersion)
	b = binary.LittleEndian.AppendUint16(b, e.flags())
	b = binary.LittleEndian.AppendUint16(b, zip.Deflate)
	b = binary.LittleEndian.AppendUint16(b, 0)
	b = binary.LittleEndian.AppendUint16(b, zipDate)
	b = binary.LittleEndian.AppendUint32(b, e.crc)
	b = binary.LittleEndian.AppendUint32(b, uint32(e.csize))
	b = binary.LittleEndian.AppendUint32(b, uint32(e.size))
	b = binary.LittleEndian.AppendUint16(b, uint16(len(e.name)))
	b = binary.LittleEndian.AppendUint16(b, 0)
	return append(b, e.name...)
}

// directory is the central directory of the one entry and its end record.
func directory(e zipEntry) []byte {
	b := make([]byte, 0, 46+len(e.name)+22)
	b = binary.LittleEndian.AppendUint32(b, 0x02014b50)
	b = binary.LittleEndian.AppendUint16(b, zipMadeBy)
	b = binary.LittleEndian.AppendUint16(b, zipVersion)
	b = binary.LittleEndian.AppendUint16(b, e.flags())
	b = binary.LittleEndian.AppendUint16(b, zip.Deflate)
	b = binary.LittleEndian.AppendUint16(b, 0)
	b = binary.LittleEndian.AppendUint16(b, zipDate)
	b = binary.LittleEndian.AppendUint32(b, e.crc)
	b = binary.LittleEndian.AppendUint32(b, uint32(e.csize))
	b = binary.LittleEndian.AppendUint32(b, uint32(e.size))
	b = binary.LittleEndian.AppendUint16(b, uint16(len(e.name)))
	b = binary.LittleEndian.AppendUint16(b, 0) // extra
	b = binary.LittleEndian.AppendUint16(b, 0) // comment
	b = binary.LittleEndian.AppendUint16(b, 0) // disk
	b = binary.LittleEndian.AppendUint16(b, 0) // internal attributes
	b = binary.LittleEndian.AppendUint32(b, zipAttrs)
	b = binary.LittleEndian.AppendUint32(b, 0) // the local header's offset
	b = append(b, e.name...)
	start := uint32(30 + len(e.name) + int(e.csize))
	b = binary.LittleEndian.AppendUint32(b, 0x06054b50)
	b = binary.LittleEndian.AppendUint16(b, 0)
	b = binary.LittleEndian.AppendUint16(b, 0)
	b = binary.LittleEndian.AppendUint16(b, 1)
	b = binary.LittleEndian.AppendUint16(b, 1)
	b = binary.LittleEndian.AppendUint32(b, uint32(46+len(e.name)))
	b = binary.LittleEndian.AppendUint32(b, start)
	return binary.LittleEndian.AppendUint16(b, 0)
}

// counter counts and checksums what passes through it.
type counter struct {
	w   io.Writer
	n   int64
	crc uint32
}

func (c *counter) Write(p []byte) (int, error) {
	n, err := c.w.Write(p)
	c.n += int64(n)
	c.crc = crc32.Update(c.crc, crc32.IEEETable, p[:n])
	return n, err
}

// packFile writes src as store/archive.py pack_dir packs a directory holding one file name:
// one ZIP_DEFLATED entry dated 1980-01-01 00:00, mode 0644, made on Unix, no data
// descriptor, no extra field, no comment. Every byte but the deflate stream (and the sizes
// and offset that follow from its length) is Python's (pack-dir-vectors.json). The stream is
// compress/flate's at zlib's default level, 6, which is not zlib's output, so Python and Go
// pack the same file to different bytes; each unpacks the other's.
func packFile(dst io.WriteSeeker, name string, src io.Reader) (zipEntry, error) {
	e := zipEntry{name: name}
	if _, err := dst.Write(localHeader(e)); err != nil {
		return e, err
	}
	compressed := &counter{w: dst}
	deflate, err := flate.NewWriter(compressed, flate.DefaultCompression)
	if err != nil {
		return e, err
	}
	plain := &counter{w: deflate}
	if _, err := io.Copy(plain, src); err != nil {
		return e, err
	}
	if err := deflate.Close(); err != nil {
		return e, err
	}
	if plain.n > zip64Limit || compressed.n > zip64Limit {
		return e, fmt.Errorf("blobstore: a packed file is at most %d bytes (zipfile.ZIP64_LIMIT)", zip64Limit)
	}
	e.crc, e.size, e.csize = plain.crc, plain.n, compressed.n
	if _, err := dst.Write(directory(e)); err != nil {
		return e, err
	}
	if _, err := dst.Seek(0, io.SeekStart); err != nil {
		return e, err
	}
	_, err = dst.Write(localHeader(e))
	return e, err
}
