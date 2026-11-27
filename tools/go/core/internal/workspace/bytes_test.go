package workspace

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"mime/multipart"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync"
	"testing"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/auth"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/seal"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/testdb"
)

// The tests of backend/tests/crud/test_crud_agent_workspace.py that cover the routes of
// bytes.go, over a content store double (memStore) in place of internal/blobstore's.

func TestMain(m *testing.M) { testdb.Main(m) }

const base = "/api/v1/internal/crud/agent"

var (
	kek = seal.NewKEK(bytes.Repeat([]byte{0x41}, 32))
	old = seal.NewKEK(bytes.Repeat([]byte{0x42}, 32))
)

// memStore is the content store double: blobs by index key, a sealed blob's key its
// sha256, as ContentStore.put names a kind's blob.
type memStore struct {
	mu      sync.Mutex
	backend string
	blobs   map[string][]byte
	puts    int
	failPut error
	// onGet runs as a read starts, before its bytes.
	onGet func()
	// corrupt makes a read's bytes not their sha256.
	corrupt bool
}

func (m *memStore) Backend(context.Context) (string, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.backend, nil
}

func (m *memStore) Put(_ context.Context, kind, name string, data []byte) (string, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if kind != blobKind || name != "file.sealed" {
		return "", fmt.Errorf("put of kind %s, name %s", kind, name)
	}
	if m.failPut != nil {
		return "", m.failPut
	}
	m.puts++
	sum := sha256.Sum256(data)
	key := kind + "-" + hex.EncodeToString(sum[:])
	m.blobs[key] = append([]byte(nil), data...)
	return key, nil
}

func (m *memStore) Get(_ context.Context, key string) (io.ReadCloser, bool, error) {
	m.mu.Lock()
	data, ok := m.blobs[key]
	onGet, corrupt := m.onGet, m.corrupt
	m.mu.Unlock()
	if !ok {
		return nil, false, nil
	}
	if onGet != nil {
		onGet()
	}
	if corrupt {
		data = append(append([]byte{}, data...), 0)
	}
	sum := sha256.Sum256(data)
	if want := strings.TrimPrefix(key, blobKind+"-"); hex.EncodeToString(sum[:]) != want {
		// BlobCorruptError, at the end of the read.
		return io.NopCloser(io.MultiReader(bytes.NewReader(data), errReader{})), true, nil
	}
	return io.NopCloser(bytes.NewReader(data)), true, nil
}

type errReader struct{}

func (errReader) Read([]byte) (int, error) { return 0, errors.New("the blob is not its sha256") }

type storeFull struct{}

func (storeFull) Error() string { return "the store is full" }
func (storeFull) Class() string { return "StoreFullError" }

type api struct {
	t     *testing.T
	pool  *pgxpool.Pool
	store *memStore
	url   string
	token string
}

func newAPI(t *testing.T, backend string, keys *seal.Keys) *api {
	t.Helper()
	return onPool(t, testdb.New(t), &memStore{backend: backend, blobs: map[string][]byte{}}, keys)
}

// onPool is an API over pool and store, with keys, as another process (or a restart) of it.
func onPool(t *testing.T, pool *pgxpool.Pool, store *memStore, keys *seal.Keys) *api {
	t.Helper()
	router := &crud.Router{
		Resolver: &auth.Resolver{Pool: pool},
		Next: http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(http.StatusTeapot)
		}),
	}
	router.Add("agent", Routes(pool, keys, store)...)
	server := httptest.NewServer(router)
	t.Cleanup(server.Close)
	a := &api{t: t, pool: pool, store: store, url: server.URL}
	a.token = a.credential("agent", "crud.agent")
	return a
}

func blobsAPI(t *testing.T) *api { return newAPI(t, "s3", &seal.Keys{Current: kek}) }
func localAPI(t *testing.T) *api { return newAPI(t, "local", &seal.Keys{Current: kek}) }

func (a *api) credential(caller string, scopes ...string) string {
	a.t.Helper()
	id, secret := make([]byte, 8), make([]byte, 32)
	_, _ = rand.Read(id)
	_, _ = rand.Read(secret)
	token := "sbsvc_" + hex.EncodeToString(id) + "." + base64.RawURLEncoding.EncodeToString(secret)
	hash := sha256.Sum256([]byte(token))
	a.sql(`INSERT INTO service_credentials (id, caller, scopes, secret_hash, expires_at, proof)
		VALUES ($1, $2, $3, $4, now() + interval '15 minutes', 'inprocess')`,
		hex.EncodeToString(id), caller, scopes, hash[:])
	return token
}

func (a *api) sql(query string, args ...any) {
	a.t.Helper()
	if _, err := a.pool.Exec(context.Background(), query, args...); err != nil {
		a.t.Fatal(err)
	}
}

func (a *api) count(query string, args ...any) int {
	a.t.Helper()
	var n int
	if err := a.pool.QueryRow(context.Background(), query, args...).Scan(&n); err != nil {
		a.t.Fatal(err)
	}
	return n
}

func (a *api) session() string {
	a.t.Helper()
	id := uuid()
	a.sql(`INSERT INTO ai_sessions (id, origin, owner_kind, owner_id, owner_label, creator_kind,
		creator_id, status, max_turns, budget_usd) VALUES ($1, 'chat', 'browser', 'browser',
		'You', 'browser', 'browser', 'running', 10, 1)`, id)
	return id
}

func uuid() string {
	b := make([]byte, 16)
	_, _ = rand.Read(b)
	b[6], b[8] = b[6]&0x0f|0x40, b[8]&0x3f|0x80
	h := hex.EncodeToString(b)
	return h[:8] + "-" + h[8:12] + "-" + h[12:16] + "-" + h[16:20] + "-" + h[20:]
}

type answer struct {
	status int
	header http.Header
	body   map[string]any
	raw    string
	err    error
}

func (a *api) request(method, path string, body io.Reader, kind string) answer {
	a.t.Helper()
	req, err := http.NewRequest(method, a.url+base+path, body)
	if err != nil {
		a.t.Fatal(err)
	}
	if kind != "" {
		req.Header.Set("Content-Type", kind)
	}
	req.Header.Set("Authorization", "Bearer "+a.token)
	res, err := http.DefaultClient.Do(req)
	if err != nil {
		a.t.Fatal(err)
	}
	defer res.Body.Close()
	raw, err := io.ReadAll(res.Body)
	out := answer{status: res.StatusCode, header: res.Header, raw: string(raw), err: err}
	_ = json.Unmarshal(raw, &out.body)
	return out
}

func (a *api) post(path string, body any) answer {
	a.t.Helper()
	raw, _ := json.Marshal(body)
	return a.request("POST", path, bytes.NewReader(raw), "application/json")
}

func (a *api) get(path string, query ...string) answer {
	a.t.Helper()
	values := url.Values{}
	for i := 0; i < len(query); i += 2 {
		values.Set(query[i], query[i+1])
	}
	return a.request("GET", path+"?"+values.Encode(), nil, "")
}

func (a *api) read(workspace, path string, expected ...string) answer {
	a.t.Helper()
	q := []string{"workspace", workspace, "path", path}
	if len(expected) > 0 {
		q = append(q, "expected_sha256", expected[0])
	}
	return a.get("/workspace/file-bytes", q...)
}

type write struct {
	expect, expected, mediaType string
	events                      []map[string]any
	source                      any
	meta                        map[string]any
}

func shaOf(data []byte) string {
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}

func (a *api) write(workspace, path string, data []byte, w ...write) answer {
	a.t.Helper()
	opt := write{expect: "any", mediaType: "application/octet-stream"}
	if len(w) > 0 {
		opt = w[0]
		if opt.expect == "" {
			opt.expect = "any"
		}
		if opt.mediaType == "" {
			opt.mediaType = "application/octet-stream"
		}
	}
	events := []string{}
	for _, e := range opt.events {
		raw, _ := json.Marshal(e)
		events = append(events, string(raw))
	}
	meta := map[string]any{
		"workspace": workspace, "path": path, "media_type": opt.mediaType, "sha256": shaOf(data),
		"size": len(data), "source": opt.source, "expect": opt.expect, "events": events,
	}
	if opt.expected != "" {
		meta["expected_sha256"] = opt.expected
	}
	for k, v := range opt.meta {
		meta[k] = v
	}
	raw, _ := json.Marshal(meta)
	return a.form([][3]string{{"meta", "", string(raw)}, {"file", "file", string(data)}})
}

// form is a multipart body of parts: name, filename ("" for a field) and content.
func (a *api) form(parts [][3]string) answer {
	a.t.Helper()
	var b bytes.Buffer
	mw := multipart.NewWriter(&b)
	for _, p := range parts {
		if p[1] == "" {
			_ = mw.WriteField(p[0], p[2])
			continue
		}
		part, _ := mw.CreateFormFile(p[0], p[1])
		_, _ = part.Write([]byte(p[2]))
	}
	_ = mw.Close()
	return a.request("POST", "/workspace/file-writes", &b, mw.FormDataContentType())
}

func event(session, path, change string) map[string]any {
	return map[string]any{
		"v": 1, "type": "workspace.file", "sessionId": session, "path": path, "change": change,
		"by": map[string]any{"kind": "browser", "id": "browser", "label": "You"},
	}
}

func (a *api) holds() map[string]bool {
	a.t.Helper()
	rows, err := a.pool.Query(context.Background(),
		`SELECT key || ' ' || holder_id FROM blob_refs WHERE holder_kind = 'ai_session_file'`)
	if err != nil {
		a.t.Fatal(err)
	}
	defer rows.Close()
	out := map[string]bool{}
	for rows.Next() {
		var s string
		_ = rows.Scan(&s)
		out[s] = true
	}
	return out
}

func expect[T comparable](t *testing.T, got, want T) {
	t.Helper()
	if got != want {
		t.Fatalf("got %v, want %v", got, want)
	}
}

func (a *api) refused(got answer, status int, reason string) {
	a.t.Helper()
	if got.status != status || (reason != "" && got.body["reason"] != reason) {
		a.t.Fatalf("got %d %s, want %d %s", got.status, got.raw, status, reason)
	}
}

// --- the guard -----------------------------------------------------------------------

func TestOnlyTheAgentCredentialReachesTheByteRoutes(t *testing.T) {
	a := localAPI(t)
	for _, path := range []string{"/workspace/status", "/workspace/file-bytes?workspace=x&path=y"} {
		req, _ := http.NewRequest("GET", a.url+base+path, nil)
		res, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		res.Body.Close()
		expect(t, res.StatusCode, 401)
	}
	a.token = a.credential("render", "crud.render")
	expect(t, a.get("/workspace/status").status, 403)
	expect(t, a.write("principal:flow:x", "a", []byte("x")).status, 403)
}

func TestAPathOrWorkspaceThatClimbsIsRefusedBeforeAnySQL(t *testing.T) {
	a := localAPI(t)
	sid := a.session()
	for _, path := range []string{"../x", "a/../b", "/etc/passwd", ".hidden", "a//b"} {
		a.refused(a.read("session:"+sid, path), 422, "invalid_path")
		a.refused(a.write("session:"+sid, path, []byte("x")), 422, "invalid_path")
	}
	for _, ws := range []string{"session:../x", "session:" + sid + "/..", "principal:root:x", "other"} {
		a.refused(a.write(ws, "a", []byte("x")), 422, "invalid_path")
		a.refused(a.read(ws, "a"), 422, "invalid_path")
	}
	expect(t, a.count(`SELECT count(*) FROM ai_session_files`), 0)
}

// --- Postgres (store_backend local) -----------------------------------------------------

func TestAFileKeptInPostgresIsWrittenReadAndDeleted(t *testing.T) {
	a := localAPI(t)
	sid := a.session()
	key := "session:" + sid
	written := a.write(key, "./notes/a.txt", []byte("hello"), write{
		mediaType: " text/plain ", meta: map[string]any{"source": json.RawMessage(`{"kind": "upload", "n": 1.0, "s": 1.50, "big": 1e20, "ok": true, "z": null}`)},
		expect: "absent", events: []map[string]any{event(sid, "notes/a.txt", "uploaded")},
	})
	expect(t, written.status, 200)
	file := written.body["file"].(map[string]any)
	expect(t, file["path"].(string), "notes/a.txt")
	expect(t, file["media_type"].(string), "text/plain")
	expect(t, len(written.body["seqs"].([]any)), 1)
	// The source as Python stores it: json.dumps of what pydantic read.
	var source string
	if err := a.pool.QueryRow(context.Background(), `SELECT source::text FROM ai_session_files`).Scan(&source); err != nil {
		t.Fatal(err)
	}
	expect(t, source, `{"n": 1.0, "s": 1.5, "z": null, "ok": true, "big": 100000000000000000000, "kind": "upload"}`)
	expect(t, a.count(`SELECT count(*) FROM ai_session_files WHERE data IS NULL AND blob_key IS NULL
		AND bytes_sha256 = $1`, shaOf([]byte("hello"))), 1)
	expect(t, a.count(`SELECT count(*) FROM ai_session_events WHERE session_id = $1
		AND event::jsonb ->> 'path' = 'notes/a.txt'`, sid), 1)

	read := a.read(key, "notes/a.txt")
	expect(t, read.status, 200)
	expect(t, read.raw, "hello")
	expect(t, read.header.Get("X-ScadBuddy-Sha256"), shaOf([]byte("hello")))
	expect(t, read.header.Get("X-ScadBuddy-Size"), "5")
	expect(t, read.header.Get("Content-Type"), "application/octet-stream")
	expect(t, read.header.Get("X-ScadBuddy-Crud"), "1")
	a.refused(a.read(key, "notes/a.txt", strings.Repeat("0", 64)), 409, "conflict")
	a.refused(a.read(key, "notes/b.txt"), 404, "not_found")
	expect(t, a.read(key, "notes/a.txt", "X").status, 422)

	// An empty file reads as no bytes.
	expect(t, a.write(key, "empty", nil).status, 200)
	empty := a.read(key, "empty")
	expect(t, empty.status, 200)
	expect(t, empty.raw, "")

	// Bytes past one segment come in segments.
	big := bytes.Repeat([]byte("0123456789abcdef"), segmentBytes/16+3)
	expect(t, a.write(key, "big.bin", big).status, 200)
	expect(t, a.read(key, "big.bin").raw, string(big))

	// A replaced file's bytes go once no file names them.
	expect(t, a.write(key, "notes/a.txt", []byte("other")).status, 200)
	expect(t, a.count(`SELECT count(*) FROM ai_session_file_bytes WHERE sha256 = $1`, shaOf([]byte("hello"))), 0)
}

func TestAWriteIsRefusedOnConflictCapsAndADirectoryClash(t *testing.T) {
	a := localAPI(t)
	key := "session:" + a.session()
	first := a.write(key, "a.txt", []byte("one"))
	expect(t, first.status, 200)
	sha := first.body["file"].(map[string]any)["sha256"].(string)
	refused := a.write(key, "a.txt", []byte("two"), write{expect: "absent"})
	a.refused(refused, 409, "conflict")
	expect(t, refused.body["detail"].(string), "a.txt already exists (sha256 "+sha+"); nothing was written")
	stale := a.write(key, "a.txt", []byte("two"), write{expect: "sha256", expected: strings.Repeat("0", 64)})
	a.refused(stale, 409, "conflict")
	expect(t, stale.body["detail"].(string), "a.txt changed since it was read: its sha256 is "+sha+", not "+
		strings.Repeat("0", 64)+"; read it again. Nothing was written")
	a.refused(a.write(key, "gone", []byte("x"), write{expect: "sha256", expected: sha}), 409, "conflict")
	expect(t, a.write(key, "a.txt", []byte("x"), write{expect: "sha256"}).body["detail"].(string),
		"expect sha256 names expected_sha256")
	expect(t, a.write(key, "a.txt", []byte("two"), write{expect: "sha256", expected: sha}).status, 200)
	clash := a.write(key, "a.txt/b", []byte("x"))
	a.refused(clash, 409, "conflict")
	expect(t, clash.body["detail"].(string), "a.txt is a file, so a.txt/b cannot be under it; nothing was written")
	expect(t, a.write(key, "dir/b", []byte("x")).status, 200)
	expect(t, a.write(key, "dir", []byte("x")).body["detail"].(string),
		"dir is a directory (it holds dir/b), so it cannot be a file too; nothing was written")
	a.sql(`INSERT INTO ai_settings (key, value) VALUES ('workspace_file_max_bytes', '4'::jsonb),
		('workspace_max_bytes', '6'::jsonb)`)
	tooLarge := a.write(key, "big", []byte("12345"))
	a.refused(tooLarge, 413, "too_large")
	expect(t, tooLarge.body["detail"].(string), "big would be 5 bytes; a workspace file holds at most 4")
	quota := a.write(key, "more", []byte("1234"))
	a.refused(quota, 409, "quota")
	expect(t, quota.body["detail"].(string), "the workspace would hold 8 bytes; it holds at most 6."+
		" Delete files with workspace_delete first")
	// A value that is no positive integer counts as unset.
	a.sql(`UPDATE ai_settings SET value = '4.0'::jsonb WHERE key = 'workspace_file_max_bytes'`)
	a.refused(a.write(key, "big", []byte("12345")), 409, "quota")
	// The sha256 the meta names is the bytes'.
	lying := a.write(key, "c", []byte("x"), write{meta: map[string]any{"sha256": strings.Repeat("0", 64)}})
	expect(t, lying.status, 422)
	expect(t, lying.body["detail"].(string), "the file is not the sha256 and size meta names")
}

func TestAWriteBodyIsTwoPartsAndItsMetaIsChecked(t *testing.T) {
	a := localAPI(t)
	key := "principal:flow:x"
	notForm := a.request("POST", "/workspace/file-writes", strings.NewReader("{}"), "application/json")
	expect(t, notForm.status, 422)
	expect(t, notForm.body["detail"].(string), "a write comes as multipart/form-data")
	noFile := a.form([][3]string{{"meta", "", "{}"}})
	expect(t, noFile.body["detail"].(string), "a write is the parts meta (JSON) and file")
	twoFields := a.form([][3]string{{"meta", "", "{}"}, {"other", "", "x"}, {"file", "f", "x"}})
	expect(t, twoFields.body["detail"].(string), "the write's body was refused: Too many fields. Maximum number of fields is 1.")
	twoFiles := a.form([][3]string{{"meta", "", "{}"}, {"file", "f", "x"}, {"file", "g", "x"}})
	expect(t, twoFiles.body["detail"].(string), "the write's body was refused: Too many files. Maximum number of files is 1.")
	bigMeta := a.form([][3]string{{"meta", "", strings.Repeat(" ", metaBytesMax+1)}, {"file", "f", "x"}})
	expect(t, bigMeta.body["detail"].(string), "the write's body was refused: Part exceeded maximum size of 32KB.")
	extra := a.write(key, "a", []byte("x"), write{meta: map[string]any{"other": 1}})
	expect(t, extra.body["detail"].(string), "meta: Extra inputs are not permitted")
	badExpect := a.write(key, "a", []byte("x"), write{expect: "never"})
	expect(t, badExpect.status, 422)
	expect(t, strings.HasPrefix(badExpect.body["detail"].(string), "meta: "), true)
	badSize := a.write(key, "a", []byte("x"), write{meta: map[string]any{"size": fileBytesCeiling + 1}})
	expect(t, badSize.body["detail"].(string), fmt.Sprintf("meta: Input should be less than or equal to %d", fileBytesCeiling))
	// A body of no stated length (chunked) is refused before it is read, as _write_body refuses it.
	var chunked bytes.Buffer
	mw := multipart.NewWriter(&chunked)
	_ = mw.WriteField("meta", "{}")
	_ = mw.Close()
	noLength := a.request("POST", "/workspace/file-writes", io.MultiReader(&chunked), mw.FormDataContentType())
	expect(t, noLength.status, 411)
	expect(t, noLength.body["detail"].(string), "a write names its Content-Length")
	expect(t, noLength.body["reason"], nil)
	listSource := a.write(key, "a", []byte("x"), write{source: map[string]any{"a": []int{1}}})
	expect(t, listSource.status, 422)
	bigSource := a.write(key, "a", []byte("x"), write{source: map[string]any{"a": strings.Repeat("é", 2000)}})
	expect(t, bigSource.body["detail"].(string), "a source is at most 8192 bytes of JSON")
	expect(t, a.count(`SELECT count(*) FROM ai_session_files`), 0)
}

func TestAnEventOfAnotherSessionOrKindIsRefused(t *testing.T) {
	a := localAPI(t)
	sid, other := a.session(), a.session()
	key := "session:" + sid
	expect(t, a.write(key, "a", []byte("x"), write{events: []map[string]any{event(other, "a", "uploaded")}}).status, 422)
	turn := event(sid, "a", "uploaded")
	turn["type"] = "turn.done"
	expect(t, a.write(key, "a", []byte("x"), write{events: []map[string]any{turn}}).status, 422)
	expect(t, a.write("principal:bearer:token:1", "a", []byte("x"),
		write{events: []map[string]any{event(sid, "a", "uploaded")}}).status, 422)
	expect(t, a.count(`SELECT count(*) FROM ai_session_events`), 0)
}

func TestAForkSharesTheBytesKeptInPostgres(t *testing.T) {
	a := localAPI(t)
	parent, child := a.session(), a.session()
	expect(t, a.write("session:"+parent, "a.txt", []byte("shared")).status, 200)
	expect(t, a.post("/workspace/copies", map[string]any{"from_session": parent, "to_session": child}).raw, `{"copied":1}`)
	expect(t, a.read("session:"+child, "a.txt").raw, "shared")
	expect(t, a.count(`SELECT count(*) FROM ai_session_file_bytes`), 1)
	// A row Python kept inline (data) is moved to the shared bytes, then shared.
	a.sql(`INSERT INTO ai_session_files (workspace, session_id, path, media_type, data, sha256, size)
		VALUES ($1, $2::text::uuid, 'inline.txt', 'text/plain', 'abc'::bytea, $3, 3)`,
		"session:"+parent, parent, shaOf([]byte("abc")))
	third := a.session()
	expect(t, a.post("/workspace/copies", map[string]any{"from_session": parent, "to_session": third}).raw, `{"copied":2}`)
	expect(t, a.read("session:"+third, "inline.txt").raw, "abc")
	expect(t, a.count(`SELECT count(*) FROM ai_session_files WHERE data IS NOT NULL`), 0)
	same := a.post("/workspace/copies", map[string]any{"from_session": parent, "to_session": parent})
	expect(t, same.body["detail"].(string), "a fork's workspace is another session's")
	expect(t, a.post("/workspace/copies", map[string]any{"from_session": "X", "to_session": parent}).status, 422)
}

func TestStatusSaysPostgresOnStoreBackendLocal(t *testing.T) {
	a := localAPI(t)
	expect(t, a.get("/workspace/status").raw, `{"store":"postgres","backend":null,"reason":"the backend has no`+
		` blob store (store_backend local)","unreadable_files":null,"sealed_unreadable":"its bytes are in the`+
		` blob store, and the backend now keeps files locally (store_backend local)"}`)
	expect(t, a.post("/workspace/blob-moves", map[string]any{}).raw, `{"moved":0,"local":true,"more":false}`)
	// A sealed file cannot be read from here.
	a.sql(`INSERT INTO ai_session_files (workspace, path, media_type, blob_key, key_sealed, key_kek_id, sha256, size)
		VALUES ('principal:flow:x', 'a', 'text/plain', $1, '\x00'::bytea, 'k', $2, 1)`,
		"workspace-"+strings.Repeat("2", 64), strings.Repeat("3", 64))
	expect(t, a.get("/workspace/status").body["unreadable_files"].(float64), 1.0)
	read := a.read("principal:flow:x", "a")
	a.refused(read, 503, "unavailable")
	expect(t, read.body["detail"].(string), "a is in the blob store and cannot be read now: the backend keeps"+
		" files locally (store_backend local)")
	expect(t, a.post("/workspace/blob-moves", map[string]any{"batch": 0}).status, 422)
}

func TestStatusSaysRefusedOnAStoreBackendThisAPIDoesNotServe(t *testing.T) {
	a := newAPI(t, "bambuddy", &seal.Keys{Current: kek})
	expect(t, a.get("/workspace/status").raw, `{"store":"refused","backend":"bambuddy","reason":"the backend`+
		` keeps files in its blob store (store_backend bambuddy), and this API serves workspace files only on`+
		` store_backend s3 or local","unreadable_files":null,"sealed_unreadable":"its bytes are in the blob`+
		` store (store_backend bambuddy), and this API reads them only on store_backend s3"}`)
	a.sql(`INSERT INTO ai_session_files (workspace, path, media_type, blob_key, key_sealed, key_kek_id, sha256, size)
		VALUES ('principal:flow:x', 'a', 'text/plain', $1, '\x00'::bytea, 'k', $2, 1)`,
		"workspace-"+strings.Repeat("2", 64), strings.Repeat("3", 64))
	expect(t, a.get("/workspace/status").body["unreadable_files"].(float64), 1.0)
}

// --- sealed blobs -----------------------------------------------------------------------

func TestASessionFileIsSealedHeldAndOpenedAgain(t *testing.T) {
	a := blobsAPI(t)
	expect(t, a.get("/workspace/status").raw, `{"store":"blobs","backend":"s3","reason":null,`+
		`"unreadable_files":null,"sealed_unreadable":null}`)
	sid := a.session()
	key := "session:" + sid
	// Past one segment, so it is sealed and opened in three.
	data := make([]byte, segmentBytes*2+17)
	_, _ = rand.Read(data)
	expect(t, a.write(key, "big.bin", data).status, 200)
	var blobKey string
	var keySealed []byte
	var kekID, inline, bytesSha *string
	if err := a.pool.QueryRow(context.Background(), `SELECT blob_key, key_sealed, key_kek_id,
		encode(data, 'hex'), bytes_sha256 FROM ai_session_files`).Scan(&blobKey, &keySealed, &kekID, &inline, &bytesSha); err != nil {
		t.Fatal(err)
	}
	expect(t, strings.HasPrefix(blobKey, "workspace-"), true)
	expect(t, kekID == nil && inline == nil && bytesSha == nil, true)
	expect(t, len(a.holds()), 1)
	expect(t, a.holds()[blobKey+" "+key+"/big.bin"], true)
	// The file key is wrapped under the session's payload key, which the API made.
	expect(t, a.count(`SELECT count(*) FROM ai_payload_keys WHERE subject = $1`, "session-"+sid), 1)
	// The blob is the file sealed segment by segment under that file key, in Python's format.
	var dekSealed []byte
	if err := a.pool.QueryRow(context.Background(), `SELECT dek_sealed FROM ai_payload_keys`).Scan(&dekSealed); err != nil {
		t.Fatal(err)
	}
	dek, err := seal.OpenBytes(kek.Key(), dekSealed, seal.PayloadKeyContext("session-"+sid))
	if err != nil {
		t.Fatal(err)
	}
	fileKey, err := seal.OpenBytes(dek, keySealed, fileKeyContext(key, "big.bin", blobKey))
	if err != nil {
		t.Fatal(err)
	}
	first := a.store.blobs[blobKey][:sealedSegment]
	plain, err := seal.OpenBytes(fileKey, first, "workspace-file:"+shaOf(data)+":0:more")
	if err != nil || !bytes.Equal(plain, data[:segmentBytes]) {
		t.Fatal("the first segment is not sealed in its own context")
	}
	expect(t, a.read(key, "big.bin").raw, string(data))
	expect(t, a.read(key, "big.bin", shaOf(data)).raw, string(data))
	listed := a.post("/workspace/session-files", map[string]any{"session_ids": []string{sid}}).body["sessions"].(map[string]any)
	expect(t, listed[sid].([]any)[0].(map[string]any)["sealed"].(bool), true)

	expect(t, a.write(key, "big.bin", []byte("small")).status, 200)
	var replaced string
	_ = a.pool.QueryRow(context.Background(), `SELECT blob_key FROM ai_session_files`).Scan(&replaced)
	expect(t, len(a.holds()), 1)
	expect(t, a.holds()[replaced+" "+key+"/big.bin"], true)
	expect(t, a.post("/workspace/file-deletes", map[string]any{"workspace": key, "path": "big.bin"}).status, 200)
	expect(t, len(a.holds()), 0)
}

// A file Python sealed and wrote opens here: the vectors' blobs and rows, as Python wrote them.
func TestAFilePythonSealedIsReadHere(t *testing.T) {
	v := loadVectors(t)
	vkek, err := seal.FromBase64(v.KEKB64)
	if err != nil {
		t.Fatal(err)
	}
	a := newAPI(t, "s3", &seal.Keys{Current: vkek})
	a.sql(`INSERT INTO ai_sessions (id, origin, owner_kind, owner_id, owner_label, creator_kind,
		creator_id, status, max_turns, budget_usd) VALUES ($1, 'chat', 'browser', 'browser',
		'You', 'browser', 'browser', 'running', 10, 1)`, v.Session)
	a.sql(`INSERT INTO ai_payload_keys (subject, dek_sealed, kek_id) VALUES ($1, $2, $3)`,
		v.PayloadKey.Subject, b64(t, v.PayloadKey.DEKSealedB64), v.KEKID)
	for _, w := range v.Wraps {
		f := v.Files[w.File]
		a.store.blobs[w.BlobKey] = pythonSealed(f)
		var session, kekID any
		if id, ok := sessionOf(w.Workspace); ok {
			session = id
		} else {
			kekID = v.KEKID
		}
		a.sql(`INSERT INTO ai_session_files (workspace, session_id, path, media_type, blob_key, key_sealed,
			key_kek_id, sha256, size) VALUES ($1, $2::text::uuid, $3, 'application/octet-stream', $4, $5, $6, $7, $8)`,
			w.Workspace, session, w.Path, w.BlobKey, b64(t, w.WrappedB64), kekID, f.SHA256, f.Size)
		read := a.read(w.Workspace, w.Path)
		expect(t, read.status, 200)
		if read.raw != string(vectorPlaintext(f.Size, f.Seed)) {
			t.Fatalf("%s %s: read %d bytes that are not Python's file", w.Workspace, w.Path, len(read.raw))
		}
	}
	// A Python-sealed parent forks here: the child's key is wrapped again, the blob shared.
	child := a.session()
	expect(t, a.post("/workspace/copies", map[string]any{"from_session": v.Session, "to_session": child}).raw, `{"copied":2}`)
	expect(t, a.read("session:"+child, "downloads/part.3mf").raw, string(vectorPlaintext(v.Files[3].Size, v.Files[3].Seed)))
}

func TestAPrincipalFileOpensUnderThePreviousKEK(t *testing.T) {
	pool := testdb.New(t)
	store := &memStore{backend: "s3", blobs: map[string][]byte{}}
	principal := "principal:oidc:https://idp.example/#sub"
	before := onPool(t, pool, store, &seal.Keys{Current: old})
	expect(t, before.write(principal, "a.txt", []byte("before")).status, 200)
	sid := before.session()
	expect(t, before.write("session:"+sid, "s.txt", []byte("session")).status, 200)
	expect(t, before.count(`SELECT count(*) FROM ai_session_files WHERE key_kek_id = $1`, old.ID), 1)
	// Rotated, with the previous KEK still held: both open.
	rotated := onPool(t, pool, store, &seal.Keys{Current: kek, Previous: old})
	expect(t, rotated.read(principal, "a.txt").raw, "before")
	expect(t, rotated.read("session:"+sid, "s.txt").raw, "session")
	// Without it, neither does.
	without := onPool(t, pool, store, &seal.Keys{Current: kek})
	gone := without.read(principal, "a.txt")
	without.refused(gone, 503, "unavailable")
	expect(t, gone.body["detail"].(string), "a workspace file's key was sealed with key "+old.ID+", which the API does not hold")
	session := without.read("session:"+sid, "s.txt")
	without.refused(session, 503, "unavailable")
	expect(t, session.body["detail"].(string), "the payload key of session-"+sid+" was sealed with key "+old.ID+
		", which the API does not hold")
}

func TestWithoutAKEKAWriteIsRefusedAndNothingIsStored(t *testing.T) {
	a := newAPI(t, "s3", nil)
	status := a.get("/workspace/status")
	expect(t, status.body["store"].(string), "refused")
	expect(t, strings.Contains(status.body["reason"].(string), "SCADBUDDY_SECRET_KEY_FILE"), true)
	refused := a.write("session:"+a.session(), "a", []byte("x"))
	a.refused(refused, 503, "unavailable")
	expect(t, refused.body["detail"].(string), "the backend keeps files in its blob store (store_backend s3),"+
		" and the API has no SCADBUDDY_SECRET_KEY_FILE to seal them with; nothing was written")
	a.refused(a.post("/workspace/blob-moves", map[string]any{}), 503, "unavailable")
	expect(t, a.store.puts, 0)
	expect(t, a.count(`SELECT count(*) FROM ai_session_files`), 0)
}

func TestARefusedWriteStoresNoBlob(t *testing.T) {
	a := blobsAPI(t)
	key := "session:" + a.session()
	expect(t, a.write(key, "a", []byte("x")).status, 200)
	a.refused(a.write(key, "a", []byte("y"), write{expect: "absent"}), 409, "conflict")
	a.sql(`INSERT INTO ai_settings (key, value) VALUES ('workspace_max_bytes', '2'::jsonb)`)
	a.refused(a.write(key, "b", []byte("yy")), 409, "quota")
	expect(t, a.store.puts, 1)
	a.store.failPut = storeFull{}
	full := a.write(key, "c", []byte("z"))
	a.refused(full, 503, "unavailable")
	expect(t, full.body["detail"].(string), "the blob store could not take the file (StoreFullError); nothing was written")
}

func TestASessionGoneGetsNoKeyAndNoFile(t *testing.T) {
	a := blobsAPI(t)
	refused := a.write("session:"+uuid(), "a", []byte("x"))
	a.refused(refused, 503, "unavailable")
	expect(t, a.count(`SELECT count(*) FROM ai_payload_keys`), 0)
	expect(t, a.count(`SELECT count(*) FROM ai_session_files`), 0)
}

func TestAForkRewrapsTheParentKeysAndHoldsTheSameBlob(t *testing.T) {
	a := blobsAPI(t)
	parent, child := a.session(), a.session()
	expect(t, a.write("session:"+parent, "a.bin", []byte("sealed")).status, 200)
	expect(t, a.post("/workspace/copies", map[string]any{"from_session": parent, "to_session": child}).raw, `{"copied":1}`)
	expect(t, a.count(`SELECT count(DISTINCT blob_key) FROM ai_session_files`), 1)
	expect(t, a.count(`SELECT count(DISTINCT key_sealed) FROM ai_session_files`), 2)
	expect(t, len(a.holds()), 2)
	expect(t, a.read("session:"+child, "a.bin").raw, "sealed")
	// Copied again, it adds nothing.
	expect(t, a.post("/workspace/copies", map[string]any{"from_session": parent, "to_session": child}).raw, `{"copied":0}`)
	// A forgotten parent's keys are gone: nothing is copied.
	a.sql(`DELETE FROM ai_payload_keys WHERE subject = $1`, "session-"+parent)
	a.refused(a.post("/workspace/copies", map[string]any{"from_session": parent, "to_session": a.session()}), 503, "unavailable")
}

func TestFilesKeptInPostgresMoveToBlobs(t *testing.T) {
	pool := testdb.New(t)
	store := &memStore{backend: "local", blobs: map[string][]byte{}}
	a := onPool(t, pool, store, &seal.Keys{Current: kek})
	sid := a.session()
	expect(t, a.write("session:"+sid, "a.txt", []byte("inline")).status, 200)
	store.backend = "s3"
	expect(t, a.post("/workspace/blob-moves", map[string]any{"batch": 10}).raw, `{"moved":1,"local":false,"more":true}`)
	expect(t, a.post("/workspace/blob-moves", map[string]any{}).raw, `{"moved":0,"local":false,"more":false}`)
	var blobKey string
	_ = pool.QueryRow(context.Background(), `SELECT blob_key FROM ai_session_files`).Scan(&blobKey)
	expect(t, len(a.holds()), 1)
	expect(t, a.holds()[blobKey+" session:"+sid+"/a.txt"], true)
	expect(t, a.count(`SELECT count(*) FROM ai_session_file_bytes`), 0)
	expect(t, a.read("session:"+sid, "a.txt").raw, "inline")
}

func TestAStreamEndsShortWhenTheFileChangesOrItsBlobIsNotItsBytes(t *testing.T) {
	a := blobsAPI(t)
	key := "session:" + a.session()
	data := make([]byte, segmentBytes*2+1)
	_, _ = rand.Read(data)
	expect(t, a.write(key, "a.bin", data).status, 200)
	// Replaced between the first segment and the next: the stream ends short.
	a.store.onGet = func() { a.sql(`UPDATE ai_session_files SET sha256 = $1`, strings.Repeat("4", 64)) }
	short := a.read(key, "a.bin")
	expect(t, short.status, 200)
	expect(t, short.err != nil, true)
	expect(t, len(short.raw) < len(data), true)
	a.store.onGet = nil
	a.sql(`UPDATE ai_session_files SET sha256 = $1`, shaOf(data))
	expect(t, a.read(key, "a.bin").raw, string(data))
	// A blob that is not its bytes ends short too.
	a.store.corrupt = true
	expect(t, a.read(key, "a.bin").err != nil, true)
	a.store.corrupt = false
	// A blob gone from the store cannot be read now.
	clear(a.store.blobs)
	gone := a.read(key, "a.bin")
	a.refused(gone, 503, "unavailable")
	expect(t, gone.body["detail"].(string), "a.bin is in the blob store and cannot be read now")
}

func TestAPostgresStreamEndsShortWhenTheFileChanges(t *testing.T) {
	a := localAPI(t)
	key := "principal:flow:x"
	data := bytes.Repeat([]byte("x"), segmentBytes+1)
	expect(t, a.write(key, "a.bin", data).status, 200)
	// The bytes row the file names is gone: its first slice reads none.
	a.sql(`ALTER TABLE ai_session_files DROP CONSTRAINT IF EXISTS ai_session_files_bytes_sha256_fkey`)
	a.sql(`DELETE FROM ai_session_file_bytes`)
	short := a.read(key, "a.bin")
	expect(t, short.status, 200)
	expect(t, short.err != nil, true)
}

func TestAFloatIsWrittenAsPythonsRepr(t *testing.T) {
	// Python 3.12's repr of each.
	for f, want := range map[float64]string{
		1.5e-05: "1.5e-05", 1e16: "1e+16", 9999999999999998.0: "9999999999999998.0", 0.0001: "0.0001",
		123.0: "123.0", 1e22: "1e+22", 0.1: "0.1", 5e-324: "5e-324",
		1.7976931348623157e308: "1.7976931348623157e+308", 12345678.9: "12345678.9",
	} {
		expect(t, pyFloat(f), want)
	}
	expect(t, pyFloat(math.Copysign(0, -1)), "-0.0")
}
