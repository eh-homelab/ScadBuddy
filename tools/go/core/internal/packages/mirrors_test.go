package packages

import (
	"bufio"
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
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/auth"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/testdb"
)

func TestMain(m *testing.M) { testdb.Main(m) }

const (
	mirrors = "/api/v1/internal/crud/agent/package-mirrors"
	hostedP = "/api/v1/internal/crud/agent/hosted-plugins"
	c1      = "1111111111111111111111111111111111111111"
	h1      = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	h2      = "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
)

var (
	zipBytes = append([]byte("PK\x05\x06"), make([]byte, 18)...)
	other    = []byte("another archive")
)

func hexSum(b []byte) string {
	s := sha256.Sum256(b)
	return hex.EncodeToString(s[:])
}

// fakeBlobs is the blob store as the routes see it: a blob is its one file's bytes.
type fakeBlobs struct {
	mu        sync.Mutex
	files     map[string][]byte
	claims    int
	publishes int
	// stale makes the next publish lose to another that indexed want.
	stale []byte
	// tooLarge are the keys whose read is refused as larger than its bound.
	tooLarge map[string]bool
}

func newFakeBlobs() *fakeBlobs { return &fakeBlobs{files: map[string][]byte{}} }

func (f *fakeBlobs) Claim(_ context.Context, key string) (bool, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.claims++
	_, ok := f.files[key]
	return ok, nil
}

func (f *fakeBlobs) PublishFile(_ context.Context, key, name string, file io.ReadSeeker) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.publishes++
	if name != MirrorFile {
		return errors.New("not the mirror's file")
	}
	if f.stale != nil {
		f.files[key], f.stale = f.stale, nil
		return ErrStale
	}
	if _, err := file.Seek(0, io.SeekStart); err != nil {
		return err
	}
	data, err := io.ReadAll(file)
	if err != nil {
		return err
	}
	f.files[key] = data
	return nil
}

func (f *fakeBlobs) ReadFile(_ context.Context, key, name string, _ int64) ([]byte, bool, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.tooLarge[key] {
		return nil, false, fmt.Errorf("%w: the blob %s", ErrTooLarge, key)
	}
	data, ok := f.files[key]
	return data, ok && name == MirrorFile, nil
}

type mirrorAPI struct {
	t      *testing.T
	pool   *pgxpool.Pool
	blobs  *fakeBlobs
	url    string
	tokens map[string]string
}

func newMirrorAPI(t *testing.T) *mirrorAPI {
	t.Helper()
	pool := testdb.New(t)
	a := &mirrorAPI{t: t, pool: pool, blobs: newFakeBlobs(), tokens: map[string]string{}}
	router := &crud.Router{Resolver: &auth.Resolver{Pool: pool}, Next: http.NotFoundHandler()}
	router.Add("agent", MirrorAreas(pool, a.blobs)[MirrorArea]...)
	server := httptest.NewServer(router)
	t.Cleanup(server.Close)
	a.url = server.URL
	for _, caller := range []string{"agent", "print"} {
		id, secret := make([]byte, 8), make([]byte, 32)
		_, _ = rand.Read(id)
		_, _ = rand.Read(secret)
		token := "sbsvc_" + hex.EncodeToString(id) + "." + base64.RawURLEncoding.EncodeToString(secret)
		hash := sha256.Sum256([]byte(token))
		a.sql(`INSERT INTO service_credentials (id, caller, scopes, secret_hash, expires_at, proof)
			VALUES ($1, $2, ARRAY['api', 'crud.' || $2], $3, now() + interval '15 minutes', 'inprocess')`,
			hex.EncodeToString(id), caller, hash[:])
		a.tokens[caller] = token
	}
	return a
}

func (a *mirrorAPI) sql(query string, args ...any) {
	a.t.Helper()
	if _, err := a.pool.Exec(context.Background(), query, args...); err != nil {
		a.t.Fatal(err)
	}
}

func (a *mirrorAPI) strings(query string, args ...any) []string {
	a.t.Helper()
	rows, err := a.pool.Query(context.Background(), query, args...)
	if err != nil {
		a.t.Fatal(err)
	}
	defer rows.Close()
	out := []string{}
	for rows.Next() {
		var s string
		if err := rows.Scan(&s); err != nil {
			a.t.Fatal(err)
		}
		out = append(out, s)
	}
	return out
}

type reply struct {
	status int
	header http.Header
	raw    []byte
	body   map[string]any
}

func (a *mirrorAPI) send(caller, method, path string, body io.Reader, length int64) reply {
	a.t.Helper()
	req, _ := http.NewRequest(method, a.url+path, body)
	if length != 0 {
		req.ContentLength = length
	}
	req.Header.Set("Authorization", "Bearer "+a.tokens[caller])
	req.Header.Set("Content-Type", "application/octet-stream")
	res, err := http.DefaultClient.Do(req)
	if err != nil {
		a.t.Fatal(err)
	}
	defer res.Body.Close()
	raw, _ := io.ReadAll(res.Body)
	out := reply{status: res.StatusCode, header: res.Header, raw: raw}
	_ = json.Unmarshal(raw, &out.body)
	return out
}

func (a *mirrorAPI) mirror(data []byte, name string) string {
	a.t.Helper()
	got := a.send("agent", "POST", mirrors+"/"+name, bytes.NewReader(data), int64(len(data)))
	if got.status != 200 || got.body["bytes_sha256"] != hexSum(data) {
		a.t.Fatalf("mirror: %d %s", got.status, got.raw)
	}
	return hexSum(data)
}

func (a *mirrorAPI) read(name, sum string) reply {
	return a.send("agent", "GET", mirrors+"/"+name+"/"+sum, nil, 0)
}

// pinned is a commit-pinned row, as the backend's tests' _insert makes one.
func (a *mirrorAPI) pinned(name string) {
	a.sql(`INSERT INTO ai_plugin_packages (name, source_kind, source_url, source_ref, fetch_url,
		fetch_path, pin_kind, commit_sha, content_hash, files, review)
		VALUES ($1, 'git', 'https://git.test/' || $1 || '.git', 'HEAD', 'https://git.test/' || $1 || '.git',
		'', 'commit', $2, $3, '{}', '{}')`, name, c1, h1)
}

// bytesPinned is an archive row pinned to sum.
func (a *mirrorAPI) bytesPinned(name, sum string) {
	a.sql(`INSERT INTO ai_plugin_packages (name, source_kind, source_url, source_ref, fetch_url,
		fetch_path, pin_kind, bytes_sha256, content_hash, files, review)
		VALUES ($1, 'archive', 'https://files.test/' || $1 || '.zip', '', 'https://files.test/' || $1 || '.zip',
		'', 'bytes', $2, $3, '{}', '{}')`, name, sum, h1)
}

func wantRefusal(t *testing.T, got reply, status int, reason string) {
	t.Helper()
	if got.status != status || (reason != "" && got.body["reason"] != reason) {
		t.Fatalf("got %d %s, want %d %s", got.status, got.raw, status, reason)
	}
}

func TestAMirrorIsReadBackOnlyByAPackageThatNamesIt(t *testing.T) {
	a := newMirrorAPI(t)
	sum := a.mirror(zipBytes, "greeter")
	expectJSON(t, a.strings(`SELECT holder_id FROM blob_refs WHERE holder_kind = 'plugin_package_mirror'`),
		[]string{"greeter/" + sum})
	expectJSON(t, a.strings(`SELECT key FROM blob_refs WHERE holder_kind = 'plugin_package_mirror'`),
		[]string{"plugin-mirror-" + sum})
	if got := a.blobs.files["plugin-mirror-"+sum]; !bytes.Equal(got, zipBytes) {
		t.Fatalf("kept %q", got)
	}
	unnamed := a.read("greeter", sum)
	wantRefusal(t, unnamed, 404, mirrorNotFound)
	if unnamed.body["detail"] != "no mirror of the bytes "+sum+" is kept for greeter" {
		t.Fatalf("detail %v", unnamed.body["detail"])
	}
	a.bytesPinned("greeter", sum)
	got := a.read("greeter", sum)
	if got.status != 200 || !bytes.Equal(got.raw, zipBytes) ||
		got.header.Get("Content-Type") != "application/octet-stream" {
		t.Fatalf("got %d %q %s", got.status, got.raw, got.header.Get("Content-Type"))
	}
	wantRefusal(t, a.read("other", sum), 404, mirrorNotFound)
	otherSum := a.mirror(other, "greeter")
	wantRefusal(t, a.read("greeter", otherSum), 404, mirrorNotFound)
	// Named, but the store lost it.
	delete(a.blobs.files, "plugin-mirror-"+sum)
	wantRefusal(t, a.read("greeter", sum), 404, mirrorNotFound)
}

func TestTheMirrorRoutesRefuseBadPathsAndOtherCallers(t *testing.T) {
	a := newMirrorAPI(t)
	bad := a.send("agent", "POST", mirrors+"/-bad", bytes.NewReader(zipBytes), int64(len(zipBytes)))
	wantRefusal(t, bad, 422, "")
	expectJSON(t, bad.body["errors"].([]any)[0].(map[string]any)["loc"], []string{"path", "name"})
	short := a.read("greeter", "abc")
	wantRefusal(t, short, 422, "")
	expectJSON(t, short.body["errors"].([]any)[0].(map[string]any)["loc"], []string{"path", "sha256"})
	wantRefusal(t, a.read("greeter", strings.ToUpper(hexSum(zipBytes))), 422, "")
	if a.blobs.publishes != 0 {
		t.Fatal("a refused upload was kept")
	}
	sum := hexSum(zipBytes)
	wantRefusal(t, a.send("print", "GET", mirrors+"/greeter/"+sum, nil, 0), 403, "wrong_caller")
	wantRefusal(t, a.send("print", "POST", mirrors+"/greeter", bytes.NewReader(zipBytes),
		int64(len(zipBytes))), 403, "wrong_caller")
	wantRefusal(t, a.send("print", "GET", hostedP+"/greeter/versions/1.zip", nil, 0), 403, "wrong_caller")
}

// #7868: a bytes pin's file and a commit pin's recorded zip, each for the pin and its re-pin.
func TestEveryColumnThatNamesAMirrorServesIt(t *testing.T) {
	a := newMirrorAPI(t)
	unrelated := strings.Repeat("c", 64)
	repin := `UPDATE ai_plugin_packages SET pending_ref = 'main', pending_fetch_url = fetch_url,
		pending_fetch_path = '', pending_content_hash = $2, pending_files = '{}', pending_review = '{}',
		pending_pin_kind = $3, pending_bytes_sha256 = $4, pending_commit_sha = $5 WHERE name = $1`
	setups := map[string]func(name, sum string){
		"bytes_sha256": func(name, sum string) {
			a.bytesPinned(name, unrelated)
			wantRefusal(t, a.read(name, sum), 404, mirrorNotFound)
			a.sql(`UPDATE ai_plugin_packages SET bytes_sha256 = $1 WHERE name = $2`, sum, name)
		},
		"pending_bytes_sha256": func(name, sum string) {
			a.bytesPinned(name, unrelated)
			a.sql(repin, name, h2, "bytes", unrelated, nil)
			wantRefusal(t, a.read(name, sum), 404, mirrorNotFound)
			a.sql(`UPDATE ai_plugin_packages SET pending_bytes_sha256 = $1 WHERE name = $2`, sum, name)
		},
		"mirror_sha256": func(name, sum string) {
			a.pinned(name)
			wantRefusal(t, a.read(name, sum), 404, mirrorNotFound)
			a.sql(`UPDATE ai_plugin_packages SET mirror_sha256 = $1 WHERE name = $2`, sum, name)
		},
		"pending_mirror_sha256": func(name, sum string) {
			a.pinned(name)
			a.sql(repin, name, h2, "commit", nil, strings.Repeat("2", 40))
			wantRefusal(t, a.read(name, sum), 404, mirrorNotFound)
			a.sql(`UPDATE ai_plugin_packages SET pending_mirror_sha256 = $1 WHERE name = $2`, sum, name)
		},
	}
	for _, column := range []string{"bytes_sha256", "pending_bytes_sha256", "mirror_sha256", "pending_mirror_sha256"} {
		t.Run(column, func(t *testing.T) {
			name := strings.ReplaceAll(column, "_", "-")
			data := []byte("the bytes of " + column)
			sum := a.mirror(data, name)
			setups[column](name, sum)
			got := a.read(name, sum)
			if got.status != 200 || !bytes.Equal(got.raw, data) {
				t.Fatalf("got %d %s", got.status, got.raw)
			}
			wantRefusal(t, a.read("other-"+name, sum), 404, mirrorNotFound)
		})
	}
}

func TestAMirrorPastTheLimitIsRefusedAndNotKept(t *testing.T) {
	a := newMirrorAPI(t)
	detail := "a plugin package's archive is at most 24 MB " +
		"(fixed, agent_data/package_mirrors.py MAX_MIRROR_BYTES), and this one is larger"
	big := bytes.Repeat([]byte{'x'}, MaxMirrorBytes+1)
	declared := a.send("agent", "POST", mirrors+"/greeter", bytes.NewReader(big), int64(len(big)))
	wantRefusal(t, declared, 413, "")
	if declared.body["detail"] != detail || declared.body["title"] != "Content Too Large" {
		t.Fatalf("got %s", declared.raw)
	}
	// A declared length past the limit is refused on the headers, before any body is sent.
	conn, err := net.Dial("tcp", strings.TrimPrefix(a.url, "http://"))
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	_, _ = fmt.Fprintf(conn, "POST %s/greeter HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer %s\r\n"+
		"Content-Length: %d\r\n\r\n", mirrors, a.tokens["agent"], MaxMirrorBytes+1)
	_ = conn.SetReadDeadline(time.Now().Add(5 * time.Second))
	early, err := http.ReadResponse(bufio.NewReader(conn), nil)
	if err != nil || early.StatusCode != 413 {
		t.Fatalf("a declared length past the limit: %v %v", early, err)
	}
	_ = early.Body.Close()
	// No Content-Length: counted as it streams.
	streamed := a.send("agent", "POST", mirrors+"/greeter", io.MultiReader(bytes.NewReader(big)), -1)
	wantRefusal(t, streamed, 413, "")
	if streamed.body["detail"] != detail {
		t.Fatalf("got %s", streamed.raw)
	}
	if a.blobs.publishes != 0 || len(a.strings(`SELECT key FROM blob_refs`)) != 0 {
		t.Fatal("a refused upload was kept")
	}
	// The limit itself, streamed.
	most := big[:MaxMirrorBytes]
	got := a.send("agent", "POST", mirrors+"/greeter", io.MultiReader(bytes.NewReader(most)), -1)
	if got.status != 200 || got.body["bytes_sha256"] != hexSum(most) {
		t.Fatalf("got %d %s", got.status, got.raw)
	}
}

func TestKeptBytesAreNotPublishedAgain(t *testing.T) {
	a := newMirrorAPI(t)
	a.mirror(zipBytes, "greeter")
	a.mirror(zipBytes, "other")
	if a.blobs.publishes != 1 {
		t.Fatalf("published %d times", a.blobs.publishes)
	}
	// A publish that loses to another of the same bytes keeps that one.
	sum := hexSum(other)
	a.blobs.stale = other
	a.mirror(other, "greeter")
	if a.blobs.publishes != 2 || !bytes.Equal(a.blobs.files["plugin-mirror-"+sum], other) {
		t.Fatalf("published %d times", a.blobs.publishes)
	}
}

// A key that names a blob this call can neither claim nor replace (a row on another backend)
// is a 503 that says so, not a bare 500.
func TestAStalePublishWithNothingKeptIsA503(t *testing.T) {
	blobs := &staleForever{fakeBlobs: newFakeBlobs()}
	err := keep(context.Background(), blobs, bytes.NewReader(zipBytes), hexSum(zipBytes))
	var problem *httpx.Problem
	if !errors.As(err, &problem) || problem.Status != http.StatusServiceUnavailable {
		t.Fatalf("got %v", err)
	}
}

type staleForever struct{ *fakeBlobs }

func (s *staleForever) PublishFile(context.Context, string, string, io.ReadSeeker) error {
	return ErrStale
}

func TestAMirrorsHoldIsRefreshedAndUnnamedHoldsGoAfterTheirGrace(t *testing.T) {
	a := newMirrorAPI(t)
	zipSum := a.mirror(zipBytes, "greeter")
	a.mirror(other, "greeter")
	a.bytesPinned("greeter", zipSum)
	a.sql(`UPDATE blob_refs SET created_at = now() - interval '2 days' WHERE holder_kind = 'plugin_package_mirror'`)
	// The next upload releases the stale hold no row names, keeps the named one, and holds its own.
	third := a.mirror([]byte("a third archive"), "later")
	expectJSON(t, a.strings(`SELECT holder_id FROM blob_refs WHERE holder_kind = 'plugin_package_mirror'
		ORDER BY holder_id`), []string{"greeter/" + zipSum, "later/" + third})
	// Sent again, a hold's grace starts again.
	a.sql(`UPDATE blob_refs SET created_at = now() - interval '2 days' WHERE holder_id = $1`, "later/"+third)
	a.mirror([]byte("a third archive"), "later")
	fresh := a.strings(`SELECT holder_id FROM blob_refs WHERE holder_kind = 'plugin_package_mirror'
		AND created_at > now() - interval '1 minute' ORDER BY holder_id`)
	expectJSON(t, fresh, []string{"later/" + third})
}

func (a *mirrorAPI) hosted(name string, version int, zip []byte) string {
	a.t.Helper()
	sum := hexSum(zip)
	a.sql(`INSERT INTO ai_hosted_plugins (name, description, category, tags) VALUES ($1, '', NULL, '{}')
		ON CONFLICT (name) DO NOTHING`, name)
	a.sql(`INSERT INTO ai_hosted_plugin_versions (plugin_id, version, zip_sha256, content_hash, files, published_by)
		SELECT id, $2, $3, $4, '{}', 'system' FROM ai_hosted_plugins WHERE name = $1`, name, version, sum, h2)
	a.blobs.files["attachment-"+sum] = zip
	return sum
}

func TestAHostedVersionsZipIsServedAsPublished(t *testing.T) {
	a := newMirrorAPI(t)
	sum := a.hosted("greeter", 1, zipBytes)
	got := a.send("agent", "GET", hostedP+"/greeter/versions/1.zip", nil, 0)
	if got.status != 200 || !bytes.Equal(got.raw, zipBytes) ||
		got.header.Get("Content-Type") != "application/zip" ||
		got.header.Get("X-ScadBuddy-Zip-Sha256") != sum ||
		got.header.Get("X-ScadBuddy-Content-Hash") != h2 ||
		got.header.Get("X-ScadBuddy-Crud") != "1" {
		t.Fatalf("got %d %q %v", got.status, got.raw, got.header)
	}
	missing := a.send("agent", "GET", hostedP+"/greeter/versions/2.zip", nil, 0)
	wantRefusal(t, missing, 404, hostedVersionNotFound)
	if missing.body["detail"] != `the hosted plugin "greeter" has no version 2` {
		t.Fatalf("got %s", missing.raw)
	}
	wantRefusal(t, a.send("agent", "GET", hostedP+"/nobody/versions/1.zip", nil, 0), 404, hostedVersionNotFound)
	for _, bad := range []string{"0.zip", "abc.zip", "-1.zip", "1.5.zip"} {
		got := a.send("agent", "GET", hostedP+"/greeter/versions/"+bad, nil, 0)
		wantRefusal(t, got, 422, "")
		expectJSON(t, got.body["errors"].([]any)[0].(map[string]any)["loc"], []string{"path", "version"})
	}
	wantRefusal(t, a.send("agent", "GET", hostedP+"/-bad/versions/1.zip", nil, 0), 422, "")
	wantRefusal(t, a.send("agent", "GET", hostedP+"/greeter/versions/1", nil, 0), 404, "")
}

func TestAHostedZipTheStoreLostOrAlteredIsA500(t *testing.T) {
	a := newMirrorAPI(t)
	sum := a.hosted("greeter", 1, zipBytes)
	a.blobs.files["attachment-"+sum] = other
	altered := a.send("agent", "GET", hostedP+"/greeter/versions/1.zip", nil, 0)
	wantRefusal(t, altered, 500, blobMissing)
	if altered.body["detail"] != "a file's bytes in the store do not match their sha256" {
		t.Fatalf("got %s", altered.raw)
	}
	delete(a.blobs.files, "attachment-"+sum)
	lost := a.send("agent", "GET", hostedP+"/greeter/versions/1.zip", nil, 0)
	wantRefusal(t, lost, 500, blobMissing)
	if lost.body["detail"] != "the store has lost a file's bytes" {
		t.Fatalf("got %s", lost.raw)
	}
}

// A stored blob larger than the route reads is a 500 that names it, never a bare one.
func TestAStoredBlobPastTheReadBoundIsANamedProblem(t *testing.T) {
	a := newMirrorAPI(t)
	sum := a.mirror(zipBytes, "big")
	a.bytesPinned("big", sum)
	zip := a.hosted("greeter", 1, zipBytes)
	a.blobs.tooLarge = map[string]bool{"plugin-mirror-" + sum: true, "attachment-" + zip: true}
	for _, path := range []string{mirrors + "/big/" + sum, hostedP + "/greeter/versions/1.zip"} {
		got := a.send("agent", "GET", path, nil, 0)
		wantRefusal(t, got, 500, blobTooLarge)
		if got.body["detail"] != "a stored blob is larger than this route reads" {
			t.Fatalf("%s: %s", path, got.raw)
		}
	}
}

func expectJSON(t *testing.T, got, want any) {
	t.Helper()
	g, _ := json.Marshal(got)
	w, _ := json.Marshal(want)
	if string(g) != string(w) {
		t.Fatalf("got %s, want %s", g, w)
	}
}
