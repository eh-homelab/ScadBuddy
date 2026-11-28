package main

import (
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/seal"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/testdb"
)

// objects is an S3 bucket in memory: a presigned PUT, GET, HEAD and DELETE by path.
type objects struct {
	mu   sync.Mutex
	kept map[string][]byte
}

func (o *objects) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	o.mu.Lock()
	defer o.mu.Unlock()
	if r.URL.Query().Get("X-Amz-Signature") == "" {
		w.WriteHeader(http.StatusForbidden)
		return
	}
	body, ok := o.kept[r.URL.Path]
	switch r.Method {
	case http.MethodPut:
		o.kept[r.URL.Path], _ = io.ReadAll(r.Body)
	case http.MethodGet, http.MethodHead:
		if !ok {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		if r.Method == http.MethodGet {
			_, _ = w.Write(body)
		}
	case http.MethodDelete:
		delete(o.kept, r.URL.Path)
		w.WriteHeader(http.StatusNoContent)
	}
}

// Area package-mirrors mounts over the blob store in process (#8137): a mirror the agent
// sends is published, held, and read back once its package names it; without the KEK the
// area refuses to start.
func TestThePackageMirrorsAreaMountsOverTheBlobStore(t *testing.T) {
	pool := testdb.New(t)
	if _, err := newRouter(pool, nil, nil, "package-mirrors", "", callerPackages, nil, nil, nil); err == nil ||
		!strings.Contains(err.Error(), seal.Variable) {
		t.Fatal(err)
	}
	key := filepath.Join(t.TempDir(), "kek")
	if err := os.WriteFile(key, []byte(base64.StdEncoding.EncodeToString(make([]byte, 32))+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	keys, err := seal.LoadKeys(func(name string) string { return map[string]string{seal.Variable: key}[name] })
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
	router, err := newRouter(pool, http.NotFoundHandler(), keys, "package-mirrors", "", callerPackages, nil, lookupEnv, nil)
	if err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(router)
	t.Cleanup(server.Close)
	a := &api{t: t, pool: pool, url: server.URL}
	a.token = a.credential("agent", "api", "crud.agent")

	file := "an archive the agent fetched"
	digest := sha256.Sum256([]byte(file))
	sum := hex.EncodeToString(digest[:])
	posted := a.do("POST", base+"/package-mirrors/arch", file,
		func(r *http.Request) { r.Header.Set("Content-Type", "application/octet-stream") })
	if posted.status != http.StatusOK || posted.body["bytes_sha256"] != sum {
		t.Fatal(posted.status, posted.raw)
	}
	if got := a.rows(`SELECT kind || ' ' || backend FROM store_blobs WHERE key = $1`, "plugin-mirror-"+sum); len(got) != 1 || got[0] != "piece s3" {
		t.Fatal(got)
	}
	if got := a.rows(`SELECT holder_id FROM blob_refs WHERE key = $1`, "plugin-mirror-"+sum); len(got) != 1 || got[0] != "arch/"+sum {
		t.Fatal(got)
	}
	expect(t, a.do("GET", base+"/package-mirrors/arch/"+sum, "").status, http.StatusNotFound)
	a.sql(`INSERT INTO ai_plugin_packages (name, source_kind, source_url, source_ref, fetch_url,
		fetch_path, pin_kind, bytes_sha256, content_hash, files, review)
		VALUES ('arch', 'archive', 'https://files.test/arch.zip', '', 'https://files.test/arch.zip',
		'', 'bytes', $1, $2, '{}', '{}')`, sum, h1)
	got := a.do("GET", base+"/package-mirrors/arch/"+sum, "")
	if got.status != http.StatusOK || got.raw != file {
		t.Fatal(got.status, got.raw)
	}
	// The same bytes again are the blob as it is: claimed, not published twice.
	bucket.mu.Lock()
	before := len(bucket.kept)
	bucket.mu.Unlock()
	if again := a.do("POST", base+"/package-mirrors/arch", file); again.status != http.StatusOK {
		t.Fatal(again.status, again.raw)
	}
	bucket.mu.Lock()
	defer bucket.mu.Unlock()
	if len(bucket.kept) != before {
		t.Fatal(len(bucket.kept), before)
	}
}
