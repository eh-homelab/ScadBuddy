package main

import (
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/seal"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/testdb"
)

// Area workspace mounts over the blob store in process (#8131): a principal's file is
// written to S3, sealed, as the blob workspace-<sha256>, and read back; without the KEK
// the area refuses to start.
func TestTheWorkspaceAreaMountsOverTheBlobStore(t *testing.T) {
	pool := testdb.New(t)
	if _, err := newRouter(pool, nil, nil, "workspace", "", nil, nil, nil, nil); err == nil ||
		!strings.Contains(err.Error(), seal.Variable) {
		t.Fatal(err)
	}
	a, bucket := blobArea(t, pool, "workspace")

	status := a.do("GET", base+"/workspace/status", "")
	if status.status != http.StatusOK || status.body["store"] != "blobs" || status.body["backend"] != "s3" {
		t.Fatal(status.status, status.raw)
	}
	ws, data := "principal:flow:x", []byte("a file in the blob store")
	digest := sha256.Sum256(data)
	meta := map[string]any{"workspace": ws, "path": "a.txt", "media_type": "text/plain",
		"sha256": hex.EncodeToString(digest[:]), "size": len(data), "source": nil, "expect": "absent", "events": []string{}}
	written := a.form("/workspace/file-writes", meta, "file", data)
	if written.status != http.StatusOK {
		t.Fatal(written.status, written.raw)
	}
	keys2 := a.rows(`SELECT blob_key FROM ai_session_files WHERE workspace = $1 AND path = 'a.txt'`, ws)
	if len(keys2) != 1 || !strings.HasPrefix(keys2[0], "workspace-") {
		t.Fatal(keys2)
	}
	sealedSHA := strings.TrimPrefix(keys2[0], "workspace-")
	if got := a.rows(`SELECT kind || ' ' || backend || ' ' || backend_id FROM store_blobs WHERE key = $1`, keys2[0]); len(got) != 1 ||
		got[0] != "workspace s3 t/workspace/"+sealedSHA {
		t.Fatal(got)
	}
	bucket.mu.Lock()
	sealed, ok := bucket.kept["/b/t/workspace/"+sealedSHA]
	bucket.mu.Unlock()
	if sum := sha256.Sum256(sealed); !ok || hex.EncodeToString(sum[:]) != sealedSHA || strings.Contains(string(sealed), string(data)) {
		t.Fatal(ok, len(sealed))
	}
	q := url.Values{"workspace": {ws}, "path": {"a.txt"}}
	read := a.do("GET", base+"/workspace/file-bytes?"+q.Encode(), "")
	if read.status != http.StatusOK || read.raw != string(data) {
		raw, _ := json.Marshal(read.body)
		t.Fatal(read.status, read.raw, string(raw))
	}
}

// blobArea is a router of area over the blob store in process: an S3 store in memory, the KEK.
func blobArea(t *testing.T, pool *pgxpool.Pool, area string) (*api, *objects) {
	t.Helper()
	kek := filepath.Join(t.TempDir(), "kek")
	if err := os.WriteFile(kek, []byte(base64.StdEncoding.EncodeToString(make([]byte, 32))+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	keys, err := seal.LoadKeys(func(name string) string { return map[string]string{seal.Variable: kek}[name] })
	if err != nil {
		t.Fatal(err)
	}
	bucket := &objects{kept: map[string][]byte{}}
	store := httptest.NewServer(bucket)
	t.Cleanup(store.Close)
	env := map[string]string{
		"SCADBUDDY_STORE_BACKEND": "s3", "SCADBUDDY_STORE_S3_ENDPOINT": store.URL,
		"SCADBUDDY_STORE_S3_BUCKET": "b", "SCADBUDDY_STORE_S3_PREFIX": "t",
		"SCADBUDDY_STORE_S3_ACCESS_KEY_ID": "k", "SCADBUDDY_STORE_S3_SECRET_ACCESS_KEY": "s",
	}
	lookupEnv := func(name string) (string, bool) { v, ok := env[name]; return v, ok }
	router, err := newRouter(pool, http.NotFoundHandler(), keys, area, "", nil, nil, lookupEnv, nil)
	if err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(router)
	t.Cleanup(server.Close)
	a := &api{t: t, pool: pool, url: server.URL}
	a.token = a.credential("agent", "api", "crud.agent")
	return a, bucket
}

// A stored mirror larger than the route reads is the store's ErrTooLarge, which the area
// answers as a named 500 (blob-too-large), refused from its row before its bytes are read.
func TestAMirrorPastTheReadBoundIsANamedProblem(t *testing.T) {
	pool := testdb.New(t)
	a, bucket := blobArea(t, pool, "package-mirrors")
	sum := strings.Repeat("a", 64)
	bucket.kept["/b/t/piece/"+sum] = []byte("x")
	a.sql(`INSERT INTO store_blobs (key, sha256, kind, backend, backend_id, size, meta)
		VALUES ($1, $2, 'piece', 's3', 't/piece/' || $2, $3, '{}'::jsonb)`, "plugin-mirror-"+sum, sum, int64(1)<<30)
	a.sql(`INSERT INTO ai_plugin_packages (name, source_kind, source_url, source_ref, fetch_url,
		fetch_path, pin_kind, bytes_sha256, content_hash, files, review)
		VALUES ('arch', 'archive', 'https://files.test/arch.zip', '', 'https://files.test/arch.zip',
		'', 'bytes', $1, $2, '{}', '{}')`, sum, h1)
	got := a.do("GET", base+"/package-mirrors/arch/"+sum, "")
	if got.status != http.StatusInternalServerError || got.body["reason"] != "blob-too-large" {
		t.Fatal(got.status, got.raw)
	}
}
