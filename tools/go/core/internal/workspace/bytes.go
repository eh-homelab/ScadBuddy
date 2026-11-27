package workspace

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"math"
	"mime"
	"mime/multipart"
	"net/http"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/seal"
)

// The routes of crud_agent_workspace.py that need the blob store or the KEK: the status,
// a file's bytes, a write, a fork's copy and the move to blobs. Python's handlers stay
// until these serve prod; the KEK rotation's rewrap stays Python's.

const (
	// fileBytesCeiling is the largest file the API takes, whatever the setting says.
	fileBytesCeiling = 100 * 1024 * 1024
	// metaBytesMax is the largest meta part of a write, framingBytesMax what its framing adds.
	metaBytesMax    = 32 * 1024
	framingBytesMax = 16 * 1024
	// multipartBodyMax is the API's body gate for multipart (api/limits.py
	// MAX_MULTIPART_BODY_BYTES): no RouteLimit raises it for a write.
	multipartBodyMax = 32 * 1024 * 1024
	sourceBytesMax   = 8 * 1024
	maxFiles         = 5_000
	moveBatchMax     = 200
	copyAttempts     = 3
	fileMaxSetting   = "workspace_file_max_bytes"
	wsMaxSetting     = "workspace_max_bytes"
	defaultFileMax   = 25 * 1024 * 1024
	defaultWsMax     = 200 * 1024 * 1024
	// blobKind is the content store's kind of a workspace file (store/content_models.py).
	blobKind     = "workspace"
	sha256Header = "X-ScadBuddy-Sha256"
	sizeHeader   = "X-ScadBuddy-Size"
)

// Store is the content store as these routes use it: Python's ContentStore over its index
// (store/content.py), on the backend the API runs. Its errors never carry a file's bytes.
type Store interface {
	// Backend is store_backend: on "local" the API has no content store, and a file's
	// bytes stay in Postgres.
	Backend(ctx context.Context) (string, error)
	// Put stores data as a blob of kind named name in the folder "work", as
	// `ContentStore.put(kind, data, name=name, scope=BlobScope(folder="work"))` with no key
	// (the room check, a re-put reusing its object, the index row at kind-<sha256>), then
	// `touch`es that row. It answers the row's key.
	Put(ctx context.Context, kind, name string, data []byte) (string, error)
	// Get is the bytes of the blob whose index row is key, as `stat` then `get`: found
	// false when no row names it on this backend or its object is gone (stat's None). An
	// error here is stat's, before the answer starts; its reader's are get's, which end the
	// stream short, as a download that fails, and at the end when the bytes are not the
	// row's sha256 (BlobCorruptError).
	Get(ctx context.Context, key string) (body io.ReadCloser, found bool, err error)
}

// Bytes serves the routes that move a file's bytes.
type Bytes struct {
	Pool  *pgxpool.Pool
	Keys  *seal.Keys
	Store Store
}

// Routes are area workspace's: every route of crud_agent_workspace.py.
func Routes(pool *pgxpool.Pool, keys *seal.Keys, store Store) []crud.Route {
	ws := &Workspace{Pool: pool}
	b := &Bytes{Pool: pool, Keys: keys, Store: store}
	return append(append(ws.Reads(), ws.Writes()...), b.Routes()...)
}

// Routes are b's routes.
func (b *Bytes) Routes() []crud.Route {
	return []crud.Route{
		{Method: "GET", Path: "/workspace/status", Handle: b.status},
		{Method: "GET", Path: "/workspace/file-bytes", Handle: b.fileBytes},
		{Method: "POST", Path: "/workspace/file-writes", Handle: b.write, MaxBody: multipartBodyMax},
		{Method: "POST", Path: "/workspace/copies", Handle: b.copy, MaxBody: bodyMax},
		{Method: "POST", Path: "/workspace/blob-moves", Handle: b.moveToBlobs, MaxBody: bodyMax},
	}
}

func (b *Bytes) current() *seal.KEK {
	if b.Keys == nil {
		return nil
	}
	return b.Keys.Current
}

// local says whether the API keeps no blob store, and the backend's name.
func (b *Bytes) local(ctx context.Context) (bool, string, error) {
	backend, err := b.Store.Backend(ctx)
	return backend == "local", backend, err
}

func unavailableWrite(why string) error {
	return refused("unavailable", why+"; nothing was written")
}

func noKEKToSeal(backend string) error {
	return unavailableWrite(fmt.Sprintf("the backend keeps files in its blob store (store_backend %s),"+
		" and the API has no SCADBUDDY_SECRET_KEY_FILE to seal them with", backend))
}

type bytesStatus struct {
	Store            string  `json:"store"`
	Backend          *string `json:"backend"`
	Reason           *string `json:"reason"`
	UnreadableFiles  *int64  `json:"unreadable_files"`
	SealedUnreadable *string `json:"sealed_unreadable"`
}

func text(s string) *string { return &s }

func (b *Bytes) status(r *http.Request, _ crud.Params) (any, error) {
	ctx := r.Context()
	local, backend, err := b.local(ctx)
	if err != nil {
		return nil, err
	}
	var out bytesStatus
	switch {
	case local:
		out = bytesStatus{
			Store:  "postgres",
			Reason: text("the backend has no blob store (store_backend local)"),
			SealedUnreadable: text("its bytes are in the blob store, and the backend now keeps files" +
				" locally (store_backend local)"),
		}
	case b.current() == nil:
		out = bytesStatus{
			Store: "refused",
			Reason: text(fmt.Sprintf("the backend keeps files in its blob store (store_backend %s), and"+
				" the API has no SCADBUDDY_SECRET_KEY_FILE to seal them with", backend)),
			SealedUnreadable: text("its bytes are sealed in the blob store, and the API has no" +
				" SCADBUDDY_SECRET_KEY_FILE to open them"),
		}
	default:
		return bytesStatus{Store: "blobs", Backend: &backend}, nil
	}
	var unreadable int64
	if err := b.Pool.QueryRow(ctx,
		`SELECT count(*)::int FROM ai_session_files WHERE blob_key IS NOT NULL`).Scan(&unreadable); err != nil {
		return nil, err
	}
	if unreadable > 0 {
		out.UnreadableFiles = &unreadable
	}
	return out, nil
}

// --- the bytes ---------------------------------------------------------------------------

type stored struct {
	sha256    string
	size      int64
	blobKey   *string
	keySealed []byte
	keyKEKID  *string
}

func (b *Bytes) stored(ctx context.Context, workspace, path string) (*stored, error) {
	var s stored
	err := b.Pool.QueryRow(ctx, `SELECT sha256, size, media_type, blob_key, key_sealed, key_kek_id
		FROM ai_session_files WHERE workspace = $1 AND path = $2`, workspace, path).
		Scan(&s.sha256, &s.size, new(string), &s.blobKey, &s.keySealed, &s.keyKEKID)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	return &s, err
}

func (b *Bytes) shaNow(ctx context.Context, workspace, path string) (*string, error) {
	var sha string
	err := b.Pool.QueryRow(ctx, `SELECT sha256 FROM ai_session_files WHERE workspace = $1 AND path = $2`,
		workspace, path).Scan(&sha)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	return &sha, err
}

// changed is a file replaced while it was being sent: past the status line, the stream
// ends short, which the agent reads as a conflict.
func changed(path string) error { return refused("conflict", path+" changed while it was being sent") }

func (b *Bytes) fileBytes(r *http.Request, _ crud.Params) (any, error) {
	workspace, err := crud.QueryText(r, "workspace", workspaceMax)
	if err != nil {
		return nil, err
	}
	path, err := crud.QueryText(r, "path", pathMax+64)
	if err != nil {
		return nil, err
	}
	expected, err := crud.QueryOptMatch(r, "expected_sha256", sha256Hex)
	if err != nil {
		return nil, err
	}
	if err := checkWorkspace(workspace); err != nil {
		return nil, err
	}
	if path, err = checkPath(path); err != nil {
		return nil, err
	}
	ctx := r.Context()
	s, err := b.stored(ctx, workspace, path)
	if err != nil {
		return nil, err
	}
	if s == nil {
		return nil, refused("not_found", fmt.Sprintf("no file %s in the workspace; workspace_list lists them", path))
	}
	if expected != nil && s.sha256 != *expected {
		return nil, refused("conflict", fmt.Sprintf("%s changed: its sha256 is %s, not %s", path, s.sha256, *expected))
	}
	var send func(ctx context.Context, w io.Writer) error
	if s.blobKey != nil {
		if send, err = b.blobStream(ctx, workspace, path, s); err != nil {
			return nil, err
		}
	} else {
		send = func(ctx context.Context, w io.Writer) error { return b.postgresStream(ctx, w, workspace, path, s) }
	}
	return crud.Stream(func(w http.ResponseWriter) {
		w.Header().Set("Content-Type", "application/octet-stream")
		w.Header().Set(sha256Header, s.sha256)
		w.Header().Set(sizeHeader, strconv.FormatInt(s.size, 10))
		w.WriteHeader(http.StatusOK)
		// The status line goes now, as Starlette sends it before the body: a failure after
		// it ends the stream short rather than leaving no answer.
		if f, ok := w.(http.Flusher); ok {
			f.Flush()
		}
		if err := send(r.Context(), flushing{w}); err != nil {
			// Past the status line: the stream ends short, which the agent reads as a
			// conflict. Never the error's bytes: it names no file content.
			slog.InfoContext(r.Context(), "workspace file stream ended short", "why", reasonOf(err))
			panic(http.ErrAbortHandler)
		}
	}), nil
}

// reasonOf is an error for the log: a refusal's words, else its type, never a value.
func reasonOf(err error) string {
	var p *httpx.Problem
	if errors.As(err, &p) {
		return p.Detail
	}
	var s *seal.Error
	if errors.As(err, &s) {
		return s.Error()
	}
	if errors.Is(err, errCutShort) {
		return err.Error()
	}
	return fmt.Sprintf("%T", err)
}

type flushing struct{ w http.ResponseWriter }

func (f flushing) Write(p []byte) (int, error) {
	n, err := f.w.Write(p)
	if fl, ok := f.w.(http.Flusher); ok {
		fl.Flush()
	}
	return n, err
}

// blobStream is crud_agent_workspace.py `_blob_stream`: what may fail before the answer
// (the store, the file key, the blob's row) fails here; the send reads the blob and opens
// it a segment at a time, reading the row again between segments.
func (b *Bytes) blobStream(ctx context.Context, workspace, path string, s *stored) (func(context.Context, io.Writer) error, error) {
	local, _, err := b.local(ctx)
	if err != nil {
		return nil, err
	}
	if local {
		return nil, refused("unavailable", path+" is in the blob store and cannot be read now: the backend"+
			" keeps files locally (store_backend local)")
	}
	fileKey, err := unwrapKey(ctx, b.Pool, b.Keys, workspace, path, *s.blobKey, s.keySealed, s.keyKEKID)
	if err != nil {
		return nil, err
	}
	body, found, err := b.Store.Get(ctx, *s.blobKey)
	if err != nil {
		return nil, err
	}
	if !found {
		return nil, refused("unavailable", path+" is in the blob store and cannot be read now")
	}
	return func(ctx context.Context, w io.Writer) error {
		defer body.Close()
		sent := 0
		return openFile(fileKey, body, s.sha256, func(part []byte) error {
			if sent > 0 {
				now, err := b.shaNow(ctx, workspace, path)
				if err != nil {
					return err
				}
				if now == nil || *now != s.sha256 {
					return changed(path)
				}
			}
			sent++
			if len(part) == 0 {
				return nil
			}
			_, err := w.Write(part)
			return err
		})
	}, nil
}

// postgresStream is `_postgres_stream`: a segment at a time by substring, each read
// checking the row is still the file it started as.
func (b *Bytes) postgresStream(ctx context.Context, w io.Writer, workspace, path string, s *stored) error {
	var offset int64
	for {
		var part []byte
		var size int64
		var sha string
		err := b.Pool.QueryRow(ctx, `SELECT substring(coalesce(f.data, b.data) FROM $1::int FOR $2::int) AS part,
			 f.size, f.sha256
			 FROM ai_session_files f LEFT JOIN ai_session_file_bytes b
			 ON b.sha256 = f.bytes_sha256 WHERE f.workspace = $3 AND f.path = $4`,
			offset+1, segmentBytes, workspace, path).Scan(&part, &size, &sha)
		if errors.Is(err, pgx.ErrNoRows) {
			return changed(path)
		}
		if err != nil {
			return err
		}
		// Gone, changed, or moved to a blob mid-send: the stream ends short.
		if sha != s.sha256 || part == nil {
			return changed(path)
		}
		if len(part) > 0 {
			if _, err := w.Write(part); err != nil {
				return err
			}
		}
		offset += int64(len(part))
		if len(part) == 0 || offset >= size {
			return nil
		}
	}
}

// --- writes ------------------------------------------------------------------------------

type writeMeta struct {
	workspace, path, mediaType, sha256 string
	size                               int64
	// source is the file's source as Python stores it (json.dumps of the dict), nil for none.
	source   *string
	expect   string
	expected *string
	events   []string
}

// writeBody is `_write_body`: the meta part and the file part of a multipart write.
func writeBody(r *http.Request) (*writeMeta, []byte, error) {
	kind := r.Header.Get("Content-Type")
	if strings.TrimSpace(strings.Split(kind, ";")[0]) != "multipart/form-data" {
		return nil, nil, unprocessable("a write comes as multipart/form-data")
	}
	if r.ContentLength > fileBytesCeiling+metaBytesMax+framingBytesMax {
		return nil, nil, httpx.WithReason(http.StatusRequestEntityTooLarge,
			fmt.Sprintf("a workspace file holds at most %d bytes", fileBytesCeiling), "too_large")
	}
	_, params, _ := mime.ParseMediaType(kind)
	meta, file, err := parseWrite(r.Body, params["boundary"])
	if err != nil {
		var max *http.MaxBytesError
		if errors.As(err, &max) {
			return nil, nil, err
		}
		return nil, nil, unprocessable("the write's body was refused: " + err.Error())
	}
	if meta == nil || file == nil {
		return nil, nil, unprocessable("a write is the parts meta (JSON) and file")
	}
	m, err := readMeta(*meta)
	if err != nil {
		return nil, nil, err
	}
	if len(file) > fileBytesCeiling {
		return nil, nil, httpx.WithReason(http.StatusRequestEntityTooLarge,
			fmt.Sprintf("a workspace file holds at most %d bytes", fileBytesCeiling), "too_large")
	}
	return m, file, nil
}

// parseWrite reads the parts as Starlette's form(max_files=1, max_fields=1,
// max_part_size=META_BYTES_MAX) does, then as FormData.get: the last part of each name, a
// field as text and a part with a filename as a file.
func parseWrite(body io.Reader, boundary string) (meta *string, file []byte, err error) {
	if boundary == "" {
		return nil, nil, errors.New("Missing boundary in multipart.")
	}
	reader := multipart.NewReader(body, boundary)
	fields, files := 0, 0
	for {
		p, err := reader.NextPart()
		if errors.Is(err, io.EOF) {
			return meta, file, nil
		}
		if err != nil {
			return nil, nil, err
		}
		_, params, _ := mime.ParseMediaType(p.Header.Get("Content-Disposition"))
		name := params["name"]
		_, isFile := params["filename"]
		if isFile {
			if files++; files > 1 {
				return nil, nil, errors.New("Too many files. Maximum number of files is 1.")
			}
			data, err := io.ReadAll(io.LimitReader(p, fileBytesCeiling+1))
			if err != nil {
				return nil, nil, err
			}
			// The last part of a name wins: a file named meta is no meta.
			if name == "file" {
				file = data
			} else if name == "meta" {
				meta = nil
			}
			continue
		}
		if fields++; fields > 1 {
			return nil, nil, errors.New("Too many fields. Maximum number of fields is 1.")
		}
		data, err := io.ReadAll(io.LimitReader(p, metaBytesMax+1))
		if err != nil {
			return nil, nil, err
		}
		if len(data) > metaBytesMax {
			return nil, nil, fmt.Errorf("Part exceeded maximum size of %dKB.", metaBytesMax/1024)
		}
		if name == "meta" {
			s := strings.ToValidUTF8(string(data), "�")
			meta = &s
		} else if name == "file" {
			file = nil
		}
	}
}

func metaIssue(msg string) error { return unprocessable("meta: " + msg) }

// readMeta is WriteMeta.model_validate_json: the first issue, as "meta: <msg>".
func readMeta(raw string) (*writeMeta, error) {
	body, err := crud.ParseBody([]byte(raw), "workspace", "path", "media_type", "sha256", "size", "source",
		"expect", "expected_sha256", "events")
	if err != nil {
		if issues := crud.Issues(err); len(issues) > 0 {
			return nil, metaIssue(issues[0].Msg)
		}
		return nil, err
	}
	m := &writeMeta{
		workspace: body.Text("workspace", 1, workspaceMax),
		path:      body.Text("path", 1, pathMax+64),
		mediaType: body.Text("media_type", 1, 255),
		sha256:    body.Match("sha256", 0, math.MaxInt, sha256Hex),
		size:      body.IntIn("size", 0, fileBytesCeiling, nil),
	}
	source := body.Get("source")
	if !source.Null() {
		dumped, msg := pySource(source.Raw())
		if msg != "" {
			crud.Fail(source, msg)
		} else {
			m.source = &dumped
		}
	}
	m.expect = body.ChoiceOr("expect", "any", "any", "absent", "sha256")
	m.expected = body.OptText("expected_sha256", 0, math.MaxInt, sha256Hex)
	m.events = body.Strings("events", true, 0, eventsMax, 0, eventBytesMax)
	if msg := body.First(); msg != "" {
		return nil, metaIssue(msg)
	}
	return m, nil
}

// pySource is a source as Python stores it, json.dumps of the dict[str, str | int | float
// | bool | None] pydantic made of it; else the issue.
func pySource(raw json.RawMessage) (string, string) {
	var members map[string]json.RawMessage
	if json.Unmarshal(raw, &members) != nil || members == nil {
		return "", "Input should be a valid dictionary"
	}
	parts := make([]string, 0, len(members))
	for key, value := range members {
		v, ok := pyScalar(value)
		if !ok {
			return "", "Input should be a valid string"
		}
		parts = append(parts, pyJSON(key)+": "+v)
	}
	return "{" + strings.Join(parts, ", ") + "}", ""
}

var integer = regexp.MustCompile(`^-?(0|[1-9][0-9]*)$`)

// pyScalar is a JSON scalar as json.dumps writes the value Python read from it.
func pyScalar(raw json.RawMessage) (string, bool) {
	text := strings.TrimSpace(string(raw))
	switch {
	case text == "null", text == "true", text == "false":
		return text, true
	case strings.HasPrefix(text, `"`):
		var s string
		if json.Unmarshal(raw, &s) != nil {
			return "", false
		}
		return pyJSON(s), true
	case integer.MatchString(text):
		if text == "-0" {
			return "0", true
		}
		return text, true
	}
	f, err := strconv.ParseFloat(text, 64)
	if err != nil {
		return "", false
	}
	return pyFloat(f), true
}

// pyFloat is Python's repr of a float.
func pyFloat(f float64) string {
	switch {
	case math.IsInf(f, 1):
		return "Infinity"
	case math.IsInf(f, -1):
		return "-Infinity"
	}
	// repr writes the shortest digits, in exponent form when their exponent is below -4 or
	// at least 16, the exponent with at least two digits, as Go's 'e' form does.
	short := strconv.FormatFloat(f, 'e', -1, 64)
	_, power, _ := strings.Cut(short, "e")
	if exp, _ := strconv.Atoi(power); f != 0 && (exp < -4 || exp >= 16) {
		return short
	}
	s := strconv.FormatFloat(f, 'f', -1, 64)
	if !strings.Contains(s, ".") {
		s += ".0"
	}
	return s
}

// pyStrip is Python's str.strip().
func pyStrip(s string) string {
	return strings.TrimFunc(s, func(r rune) bool {
		switch r {
		case ' ', '\t', '\n', '\v', '\f', '\r', 0x1c, 0x1d, 0x1e, 0x1f, 0x85, 0xa0, 0x1680,
			0x2028, 0x2029, 0x202f, 0x205f, 0x3000:
			return true
		}
		return r >= 0x2000 && r <= 0x200a
	})
}

// limits are (the largest file, the most one workspace holds), from ai_settings or the
// defaults: a stored value counts only as a positive integer below 2**53.
func (b *Bytes) limits(ctx context.Context) (int64, int64, error) {
	rows, err := b.Pool.Query(ctx, `SELECT key, value::text FROM ai_settings WHERE key = ANY($1)`,
		[]string{fileMaxSetting, wsMaxSetting})
	if err != nil {
		return 0, 0, err
	}
	stored := map[string]string{}
	for rows.Next() {
		var key, value string
		if err := rows.Scan(&key, &value); err != nil {
			rows.Close()
			return 0, 0, err
		}
		stored[key] = value
	}
	if err := rows.Err(); err != nil {
		return 0, 0, err
	}
	positive := func(key string, fallback int64) int64 {
		text, ok := stored[key]
		if !ok || !integer.MatchString(text) {
			return fallback
		}
		n, err := strconv.ParseInt(text, 10, 64)
		if err != nil || n <= 0 || n >= 1<<53 {
			return fallback
		}
		return n
	}
	return positive(fileMaxSetting, defaultFileMax), positive(wsMaxSetting, defaultWsMax), nil
}

func checkExpected(path string, current *string, expect string, expected *string) error {
	if expect == "absent" && current != nil {
		return refused("conflict", fmt.Sprintf("%s already exists (sha256 %s); nothing was written", path, *current))
	}
	if expect == "sha256" && (current == nil || expected == nil || *current != *expected) {
		if current == nil {
			return refused("conflict", path+" does not exist any more; nothing was written")
		}
		shown := "None"
		if expected != nil {
			shown = *expected
		}
		return refused("conflict", fmt.Sprintf("%s changed since it was read: its sha256 is %s, not %s;"+
			" read it again. Nothing was written", path, *current, shown))
	}
	return nil
}

func checkRoom(heldBytes, size, heldFiles, workspaceMax int64) error {
	if total := heldBytes + size; total > workspaceMax {
		return refused("quota", fmt.Sprintf("the workspace would hold %d bytes; it holds at most %d."+
			" Delete files with workspace_delete first", total, workspaceMax))
	}
	if heldFiles+1 > maxFiles {
		return refused("quota", fmt.Sprintf("the workspace holds %d files; it holds at most %d."+
			" Delete files with workspace_delete first", heldFiles, maxFiles))
	}
	return nil
}

// precheck is the write's conflict and quota checks without the lock, before its upload,
// so a refused write stores nothing; the checks under the lock are the ones that hold.
func (b *Bytes) precheck(ctx context.Context, m *writeMeta, workspaceMax int64) error {
	var current *string
	var held, files int64
	if err := b.Pool.QueryRow(ctx, `SELECT (SELECT sha256 FROM ai_session_files WHERE workspace = $1 AND path = $2)
		 AS current, (SELECT coalesce(sum(size), 0)::bigint FROM ai_session_files
		 WHERE workspace = $1 AND path <> $2) AS held,
		 (SELECT count(*)::int FROM ai_session_files
		 WHERE workspace = $1 AND path <> $2) AS files`, m.workspace, m.path).Scan(&current, &held, &files); err != nil {
		return err
	}
	if err := checkExpected(m.path, current, m.expect, m.expected); err != nil {
		return err
	}
	return checkRoom(held, m.size, files, workspaceMax)
}

// placed is a write's bytes as the row takes them: in Postgres, or a held blob and its key.
type placed struct {
	data      []byte
	blobKey   *string
	keySealed []byte
	keyKEKID  *string
}

// storeError is a store failure as Python names its class in a write's refusal.
type storeError interface{ Class() string }

// place is `_place`: data sealed under a new file key, stored as a blob, its key wrapped
// for the row. The blob is held in the row's own transaction; until then the store's grace
// keeps it.
func (b *Bytes) place(ctx context.Context, workspace, path string, data []byte, digest string) (*placed, error) {
	fileKey := newFileKey()
	sealed := sealFile(fileKey, data, digest, rand.Reader)
	blobKey, err := b.Store.Put(ctx, blobKind, "file.sealed", sealed)
	if err != nil {
		class := "Exception"
		var named storeError
		if errors.As(err, &named) {
			class = named.Class()
		}
		slog.WarnContext(ctx, "workspace file not stored", "error", class)
		return nil, unavailableWrite(fmt.Sprintf("the blob store could not take the file (%s)", class))
	}
	keySealed, kekID, err := wrapKey(ctx, b.Pool, b.Keys, workspace, path, blobKey, fileKey)
	if err != nil {
		return nil, err
	}
	return &placed{blobKey: &blobKey, keySealed: keySealed, keyKEKID: kekID}, nil
}

type written struct {
	File fileInfo `json:"file"`
	Seqs []int64  `json:"seqs"`
}

func (b *Bytes) write(r *http.Request, _ crud.Params) (any, error) {
	m, data, err := writeBody(r)
	if err != nil {
		return nil, err
	}
	if err := checkWorkspace(m.workspace); err != nil {
		return nil, err
	}
	if m.path, err = checkPath(m.path); err != nil {
		return nil, err
	}
	sum := sha256.Sum256(data)
	digest := hex.EncodeToString(sum[:])
	if digest != m.sha256 || int64(len(data)) != m.size {
		return nil, unprocessable("the file is not the sha256 and size meta names")
	}
	if m.expect == "sha256" && m.expected == nil {
		return nil, unprocessable("expect sha256 names expected_sha256")
	}
	if m.source != nil && len([]rune(*m.source)) > sourceBytesMax {
		return nil, unprocessable(fmt.Sprintf("a source is at most %d bytes of JSON", sourceBytesMax))
	}
	if err := checkEvents(m.workspace, m.events); err != nil {
		return nil, err
	}
	ctx := r.Context()
	fileMax, workspaceMax, err := b.limits(ctx)
	if err != nil {
		return nil, err
	}
	limit := min(fileMax, fileBytesCeiling)
	if int64(len(data)) > limit {
		return nil, httpx.WithReason(http.StatusRequestEntityTooLarge, fmt.Sprintf(
			"%s would be %d bytes; a workspace file holds at most %d", m.path, len(data), limit), "too_large")
	}
	m.mediaType = pyStrip(m.mediaType)
	local, backend, err := b.local(ctx)
	if err != nil {
		return nil, err
	}
	var put *placed
	if local {
		put = &placed{data: data}
	} else {
		if b.current() == nil {
			return nil, noKEKToSeal(backend)
		}
		// Checked before the upload too, so a refused write stores nothing.
		if err := b.precheck(ctx, m, workspaceMax); err != nil {
			return nil, err
		}
		if put, err = b.place(ctx, m.workspace, m.path, data, digest); err != nil {
			return nil, err
		}
	}
	info, seqs, replaced, err := b.writeRow(ctx, m, put, workspaceMax)
	if err != nil {
		return nil, err
	}
	if replaced != nil {
		dropBestEffort(ctx, b.Pool, []string{*replaced})
	}
	return written{File: info, Seqs: seqs}, nil
}

func hold(ctx context.Context, tx pgx.Tx, blobKey, workspace, path string) error {
	_, err := tx.Exec(ctx, `INSERT INTO blob_refs (key, holder_kind, holder_id) VALUES ($1, $2, $3)
		 ON CONFLICT (key, holder_kind, holder_id) DO UPDATE SET created_at = now()`,
		blobKey, holderKind, workspace+"/"+path)
	return err
}

// putBytes locks the bytes row of digest FOR KEY SHARE (which dropUnnamedBytes' SKIP
// LOCKED skips), putting it first when it is not there.
func putBytes(ctx context.Context, tx pgx.Tx, digest string, data []byte) error {
	for {
		var one int
		err := tx.QueryRow(ctx, `SELECT 1 FROM ai_session_file_bytes WHERE sha256 = $1 FOR KEY SHARE`, digest).Scan(&one)
		if err == nil {
			return nil
		}
		if !errors.Is(err, pgx.ErrNoRows) {
			return err
		}
		if _, err := tx.Exec(ctx, `INSERT INTO ai_session_file_bytes (sha256, data) VALUES ($1, $2)
			 ON CONFLICT (sha256) DO NOTHING`, digest, data); err != nil {
			return err
		}
	}
}

func lockWorkspace(ctx context.Context, tx pgx.Tx, workspace string) error {
	_, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock(hashtextextended($1, 0))`,
		"scadbuddy:ai_session_files:"+workspace)
	return err
}

// writeRow is `write_row`, one transaction: the checks under the workspace's lock, the
// row, its blob's hold (and the old blob's release) and its events. It answers the file as
// stored, the events' seqs, and the bytes row the file named before, which may now be unnamed.
func (b *Bytes) writeRow(ctx context.Context, m *writeMeta, put *placed, workspaceMax int64) (fileInfo, []int64, *string, error) {
	session, isSession := sessionOf(m.workspace)
	var sessionID *string
	if isSession {
		sessionID = &session
	}
	var got fileInfo
	seqs := []int64{}
	var replacedBytes, bytesSha *string
	err := pgx.BeginFunc(ctx, b.Pool, func(tx pgx.Tx) error {
		if len(m.events) > 0 && isSession {
			// The session's row first, as a session delete takes it before it cascades.
			if _, err := tx.Exec(ctx, `SELECT 1 FROM ai_sessions WHERE id = $1::text::uuid FOR NO KEY UPDATE`,
				session); err != nil {
				return err
			}
		}
		if err := lockWorkspace(ctx, tx, m.workspace); err != nil {
			return err
		}
		var current, currentBlob, currentBytes *string
		err := tx.QueryRow(ctx, `SELECT sha256, blob_key, bytes_sha256 FROM ai_session_files
			 WHERE workspace = $1 AND path = $2 FOR UPDATE`, m.workspace, m.path).Scan(&current, &currentBlob, &currentBytes)
		if err != nil && !errors.Is(err, pgx.ErrNoRows) {
			return err
		}
		if err := checkExpected(m.path, current, m.expect, m.expected); err != nil {
			return err
		}
		var held, files int64
		if err := tx.QueryRow(ctx, `SELECT coalesce(sum(size), 0)::bigint AS bytes, count(*)::int AS files
			 FROM ai_session_files WHERE workspace = $1 AND path <> $2`, m.workspace, m.path).Scan(&held, &files); err != nil {
			return err
		}
		if err := checkRoom(held, m.size, files, workspaceMax); err != nil {
			return err
		}
		// A file and a directory never share a name (store.ts write).
		segments := strings.Split(m.path, "/")
		parents := make([]string, 0, len(segments)-1)
		for i := range len(segments) - 1 {
			parents = append(parents, strings.Join(segments[:i+1], "/"))
		}
		var other string
		err = tx.QueryRow(ctx, `SELECT path FROM ai_session_files WHERE workspace = $1
			 AND (path = ANY($2::text[]) OR starts_with(path, $3)) LIMIT 1`, m.workspace, parents, m.path+"/").Scan(&other)
		switch {
		case err == nil:
			if len([]rune(other)) < len([]rune(m.path)) {
				return refused("conflict", fmt.Sprintf("%s is a file, so %s cannot be under it; nothing was written",
					other, m.path))
			}
			return refused("conflict", fmt.Sprintf("%s is a directory (it holds %s), so it cannot be a file too;"+
				" nothing was written", m.path, other))
		case !errors.Is(err, pgx.ErrNoRows):
			return err
		}
		if put.data != nil {
			if err := putBytes(ctx, tx, m.sha256, put.data); err != nil {
				return err
			}
			bytesSha = &m.sha256
		}
		rows, err := tx.Query(ctx, `INSERT INTO ai_session_files (workspace, session_id, path, media_type, data,
			 bytes_sha256, blob_key, key_sealed, key_kek_id, sha256, size, source)
			 VALUES ($1, $2::text::uuid, $3, $4, NULL, $5, $6, $7, $8, $9, $10, $11::text::jsonb)
			 ON CONFLICT (workspace, path) DO UPDATE SET media_type = EXCLUDED.media_type,
			 data = NULL, bytes_sha256 = EXCLUDED.bytes_sha256, blob_key = EXCLUDED.blob_key,
			 key_sealed = EXCLUDED.key_sealed, key_kek_id = EXCLUDED.key_kek_id,
			 sha256 = EXCLUDED.sha256, size = EXCLUDED.size, source = EXCLUDED.source,
			 updated_at = now() RETURNING `+info,
			m.workspace, sessionID, m.path, m.mediaType, bytesSha, put.blobKey, put.keySealed, put.keyKEKID,
			m.sha256, m.size, m.source)
		if err != nil {
			return err
		}
		if got, err = pgx.CollectExactlyOneRow(rows, scanInfo); err != nil {
			return err
		}
		if currentBlob != nil && (put.blobKey == nil || *currentBlob != *put.blobKey) {
			if err := release(ctx, tx, *currentBlob, m.workspace, m.path); err != nil {
				return err
			}
		}
		if put.blobKey != nil {
			if err := hold(ctx, tx, *put.blobKey, m.workspace, m.path); err != nil {
				return err
			}
		}
		if len(m.events) > 0 && isSession {
			if seqs, err = appendEvents(ctx, tx, session, m.events); err != nil {
				return err
			}
		}
		replacedBytes = currentBytes
		return nil
	})
	if err != nil {
		return fileInfo{}, nil, nil, err
	}
	if replacedBytes != nil && bytesSha != nil && *replacedBytes == *bytesSha {
		replacedBytes = nil
	}
	return got, seqs, replacedBytes, nil
}

// --- a fork's copy -----------------------------------------------------------------------

type blobRow struct {
	info      fileInfo
	blobKey   string
	keySealed []byte
}

func (b *Bytes) blobRowsOf(ctx context.Context, q interface {
	Query(context.Context, string, ...any) (pgx.Rows, error)
}, workspace string) ([]blobRow, error) {
	rows, err := q.Query(ctx, `SELECT `+info+`, blob_key, key_sealed FROM ai_session_files
		 WHERE workspace = $1 AND blob_key IS NOT NULL`, workspace)
	if err != nil {
		return nil, err
	}
	return pgx.CollectRows(rows, func(row pgx.CollectableRow) (blobRow, error) {
		var out blobRow
		var created, updated time.Time
		err := row.Scan(&out.info.Path, &out.info.MediaType, &out.info.Size, &out.info.Sha256, &out.info.Source,
			&created, &updated, &out.blobKey, &out.keySealed)
		out.info.CreatedAt, out.info.UpdatedAt = crud.Time(created), crud.Time(updated)
		return out, err
	})
}

type wrappedRow struct {
	row    blobRow
	sealed []byte
}

// copy is `copy`, a fork's copy of its parent's files, copy-on-write (#4212): every file key
// is wrapped again for the child first, then every row is written in one transaction under
// the parent's write lock, once the parent's blob rows are still the ones wrapped.
func (b *Bytes) copy(r *http.Request, _ crud.Params) (any, error) {
	body, err := crud.ReadBody(r, "from_session", "to_session")
	if err != nil {
		return nil, err
	}
	from := body.Match("from_session", 0, math.MaxInt, sessionID)
	to := body.Match("to_session", 0, math.MaxInt, sessionID)
	if err := body.Err(); err != nil {
		return nil, err
	}
	if from == to {
		return nil, unprocessable("a fork's workspace is another session's")
	}
	ctx := r.Context()
	source, target := "session:"+from, "session:"+to
	wrapped := map[string]wrappedRow{}
	for range copyAttempts {
		rows, err := b.blobRowsOf(ctx, b.Pool, source)
		if err != nil {
			return nil, err
		}
		now := map[string]bool{}
		for _, row := range rows {
			now[row.info.Path] = true
			if kept, ok := wrapped[row.info.Path]; ok && kept.row.blobKey == row.blobKey {
				continue
			}
			fileKey, err := unwrapKey(ctx, b.Pool, b.Keys, source, row.info.Path, row.blobKey, row.keySealed, nil)
			if err != nil {
				return nil, err
			}
			sealed, _, err := wrapKey(ctx, b.Pool, b.Keys, target, row.info.Path, row.blobKey, fileKey)
			if err != nil {
				return nil, err
			}
			wrapped[row.info.Path] = wrappedRow{row, sealed}
		}
		for path := range wrapped {
			if !now[path] {
				delete(wrapped, path)
			}
		}
		copied, done, err := b.copyRows(ctx, source, target, to, wrapped)
		if err != nil {
			return nil, err
		}
		if done {
			return struct {
				Copied int64 `json:"copied"`
			}{copied}, nil
		}
	}
	return nil, refused("conflict", "the parent's files kept changing while the fork copied them; try again")
}

// copyRows is `_copy_rows`: the fork's rows in one transaction under the parent's write
// lock; done false, nothing written, when the parent's blob rows are no longer those wrapped.
func (b *Bytes) copyRows(ctx context.Context, source, target, toSession string, wrapped map[string]wrappedRow) (int64, bool, error) {
	var written int64
	done := false
	err := pgx.BeginFunc(ctx, b.Pool, func(tx pgx.Tx) error {
		// The parent's write lock, so a write to the parent waits for the fork.
		if err := lockWorkspace(ctx, tx, source); err != nil {
			return err
		}
		now, err := b.blobRowsOf(ctx, tx, source)
		if err != nil {
			return err
		}
		if len(now) != len(wrapped) {
			return nil
		}
		for _, row := range now {
			if kept, ok := wrapped[row.info.Path]; !ok || kept.row.blobKey != row.blobKey {
				return nil
			}
		}
		// Every bytes row the child will name is locked FOR KEY SHARE until this commits:
		// lock what is there, put the bytes of the parent's inline rows that are not, and
		// lock again until all are held.
		var inlinePaths []string
		for {
			rows, err := tx.Query(ctx, `SELECT path, sha256 FROM ai_session_files WHERE workspace = $1
				 AND data IS NOT NULL AND sha256 = encode(sha256(data), 'hex')`, source)
			if err != nil {
				return err
			}
			type inlineRow struct{ Path, Sha256 string }
			inline, err := pgx.CollectRows(rows, pgx.RowToStructByPos[inlineRow])
			if err != nil {
				return err
			}
			shas := []string{}
			seen := map[string]bool{}
			for _, row := range inline {
				if !seen[row.Sha256] {
					seen[row.Sha256] = true
					shas = append(shas, row.Sha256)
				}
			}
			haveRows, err := tx.Query(ctx, `SELECT sha256 FROM ai_session_file_bytes WHERE sha256 IN
				 (SELECT bytes_sha256 FROM ai_session_files WHERE workspace = $1
				 AND bytes_sha256 IS NOT NULL) OR sha256 = ANY($2::text[]) FOR KEY SHARE`, source, shas)
			if err != nil {
				return err
			}
			have, err := pgx.CollectRows(haveRows, pgx.RowTo[string])
			if err != nil {
				return err
			}
			held := map[string]bool{}
			for _, sha := range have {
				held[sha] = true
			}
			inlinePaths = inlinePaths[:0]
			var missing []string
			for _, row := range inline {
				inlinePaths = append(inlinePaths, row.Path)
				if !held[row.Sha256] {
					missing = append(missing, row.Path)
				}
			}
			if len(missing) == 0 {
				break
			}
			if _, err := tx.Exec(ctx, `INSERT INTO ai_session_file_bytes (sha256, data)
				 SELECT DISTINCT ON (sha256) sha256, data FROM ai_session_files
				 WHERE workspace = $1 AND path = ANY($2::text[]) AND data IS NOT NULL
				 ORDER BY sha256 ON CONFLICT (sha256) DO NOTHING`, source, missing); err != nil {
				return err
			}
		}
		if inlinePaths == nil {
			inlinePaths = []string{}
		}
		if _, err := tx.Exec(ctx, `UPDATE ai_session_files SET bytes_sha256 = sha256, data = NULL
			 WHERE workspace = $1 AND path = ANY($2::text[]) AND data IS NOT NULL`, source, inlinePaths); err != nil {
			return err
		}
		tag, err := tx.Exec(ctx, `INSERT INTO ai_session_files (workspace, session_id, path, media_type, data,
			 bytes_sha256, sha256, size, source, created_at, updated_at)
			 SELECT $1, $2::text::uuid, path, media_type, data, bytes_sha256, sha256, size, source,
			 created_at, updated_at FROM ai_session_files WHERE workspace = $3
			 AND (data IS NOT NULL OR bytes_sha256 IS NOT NULL)
			 ON CONFLICT (workspace, path) DO NOTHING`, target, toSession, source)
		if err != nil {
			return err
		}
		written += tag.RowsAffected()
		paths := make([]string, 0, len(wrapped))
		for path := range wrapped {
			paths = append(paths, path)
		}
		slices.Sort(paths)
		for _, path := range paths {
			w := wrapped[path]
			row := w.row
			var src any
			if len(row.info.Source) > 0 && string(row.info.Source) != "null" {
				src = string(row.info.Source)
			}
			tag, err := tx.Exec(ctx, `INSERT INTO ai_session_files (workspace, session_id, path, media_type,
				 blob_key, key_sealed, sha256, size, source, created_at, updated_at)
				 VALUES ($1, $2::text::uuid, $3, $4, $5, $6, $7, $8, $9::text::jsonb, $10, $11)
				 ON CONFLICT (workspace, path) DO NOTHING`,
				target, toSession, row.info.Path, row.info.MediaType, row.blobKey, w.sealed, row.info.Sha256,
				row.info.Size, src, time.Time(row.info.CreatedAt), time.Time(row.info.UpdatedAt))
			if err != nil {
				return err
			}
			if tag.RowsAffected() == 1 {
				if err := hold(ctx, tx, row.blobKey, target, row.info.Path); err != nil {
					return err
				}
				written++
			}
		}
		done = true
		return nil
	})
	return written, done, err
}

// --- the move to blobs -------------------------------------------------------------------

type moved struct {
	Moved int64 `json:"moved"`
	// Local is a backend that keeps no blobs (store_backend local): nothing moves.
	Local bool `json:"local"`
	// More says rows were still in Postgres: ask again.
	More bool `json:"more"`
}

func (b *Bytes) moveToBlobs(r *http.Request, _ crud.Params) (any, error) {
	body, err := crud.ReadBody(r, "batch")
	if err != nil {
		return nil, err
	}
	fallback := int64(50)
	batch := body.IntIn("batch", 1, moveBatchMax, &fallback)
	if err := body.Err(); err != nil {
		return nil, err
	}
	ctx := r.Context()
	local, backend, err := b.local(ctx)
	if err != nil {
		return nil, err
	}
	if local {
		return moved{Local: true}, nil
	}
	if b.current() == nil {
		return nil, noKEKToSeal(backend)
	}
	// Up to batch of each kind of row still kept in Postgres.
	rows, err := b.Pool.Query(ctx, `(SELECT workspace, path, sha256 FROM ai_session_files WHERE data IS NOT NULL
		 ORDER BY workspace, path LIMIT $1) UNION ALL
		 (SELECT workspace, path, sha256 FROM ai_session_files WHERE bytes_sha256 IS NOT NULL
		 ORDER BY workspace, path LIMIT $1)`, batch)
	if err != nil {
		return nil, err
	}
	type kept struct{ Workspace, Path, Sha256 string }
	all, err := pgx.CollectRows(rows, pgx.RowToStructByPos[kept])
	if err != nil {
		return nil, err
	}
	out := moved{More: len(all) > 0}
	for _, k := range all {
		var data []byte
		var named *string
		err := b.Pool.QueryRow(ctx, `SELECT coalesce(f.data, b.data) AS data, f.bytes_sha256 FROM ai_session_files f
			 LEFT JOIN ai_session_file_bytes b ON b.sha256 = f.bytes_sha256
			 WHERE f.workspace = $1 AND f.path = $2 AND f.sha256 = $3
			 AND (f.data IS NOT NULL OR f.bytes_sha256 IS NOT NULL)`, k.Workspace, k.Path, k.Sha256).Scan(&data, &named)
		if errors.Is(err, pgx.ErrNoRows) {
			continue
		}
		if err != nil {
			return nil, err
		}
		put, err := b.place(ctx, k.Workspace, k.Path, data, k.Sha256)
		if err != nil {
			return nil, err
		}
		// A row still at its sha256 and still in Postgres, now naming its blob, held in the
		// same transaction; a row written meanwhile is left as its writer left it.
		err = pgx.BeginFunc(ctx, b.Pool, func(tx pgx.Tx) error {
			tag, err := tx.Exec(ctx, `UPDATE ai_session_files SET data = NULL, bytes_sha256 = NULL, blob_key = $1,
				 key_sealed = $2, key_kek_id = $3 WHERE workspace = $4 AND path = $5
				 AND sha256 = $6 AND (data IS NOT NULL OR bytes_sha256 IS NOT NULL)`,
				put.blobKey, put.keySealed, put.keyKEKID, k.Workspace, k.Path, k.Sha256)
			if err != nil {
				return err
			}
			if tag.RowsAffected() > 0 {
				out.Moved += tag.RowsAffected()
				return hold(ctx, tx, *put.blobKey, k.Workspace, k.Path)
			}
			return nil
		})
		if err != nil {
			return nil, err
		}
		if named != nil {
			dropBestEffort(ctx, b.Pool, []string{*named})
		}
	}
	return out, nil
}
