package blobstore

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"reflect"
	"runtime"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/auth"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/seal"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/testdb"
)

func TestMain(m *testing.M) { testdb.Main(m) }

// The example of AWS's "Authenticating Requests: Using Query Parameters (AWS Signature
// Version 4)", https://docs.aws.amazon.com/AmazonS3/latest/API/sigv4-query-string-auth.html.
func TestThePresignIsAWSsExample(t *testing.T) {
	s3 := S3{
		Region:      "us-east-1",
		AccessKeyID: "AKIAIOSFODNN7EXAMPLE", SecretAccessKey: "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
	}
	// The example's URL is virtual-host style, which the store never asks for (objectURL):
	// it pins the signing alone.
	u := must(url.Parse("https://examplebucket.s3.amazonaws.com/test.txt"))
	got := s3.presignURL("GET", u, nil, 24*time.Hour, time.Date(2013, 5, 24, 0, 0, 0, 0, time.UTC))
	const want = "https://examplebucket.s3.amazonaws.com/test.txt?X-Amz-Algorithm=AWS4-HMAC-SHA256" +
		"&X-Amz-Credential=AKIAIOSFODNN7EXAMPLE%2F20130524%2Fus-east-1%2Fs3%2Faws4_request" +
		"&X-Amz-Date=20130524T000000Z&X-Amz-Expires=86400&X-Amz-SignedHeaders=host" +
		"&X-Amz-Signature=aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404"
	if got != want {
		t.Fatalf("\n got %s\nwant %s", got, want)
	}
}

// What botocore's generate_presigned_url (the client Python's store presigns with,
// store/s3.py) answered for the same PUT and GET on 2026-11-26, its clock fixed at
// 2026-11-26T12:00:00Z: the backend's venv, botocore with signature_version s3v4 and path
// addressing, as its own `presign_put` signs.
func TestThePresignIsBotocores(t *testing.T) {
	s3 := S3{Endpoint: "http://127.0.0.1:55480", Region: "us-east-1", Bucket: "sb-test",
		AccessKeyID: "testing", SecretAccessKey: "testing"}
	at := time.Date(2026, 11, 26, 12, 0, 0, 0, time.UTC)
	key := "t/tag_dump/" + strings.Repeat("ab", 32)
	put, err := s3.presign("PUT", key, map[string]string{
		"content-length":        "18",
		"Content-Type":          "application/octet-stream",
		"x-amz-acl":             "private",
		"x-amz-checksum-sha256": "q83vEjRWeJq83vEjRWeJq83vEjRWeJq83vEjRWeJq80=",
	}, 15*time.Minute, at)
	if err != nil {
		t.Fatal(err)
	}
	base := "http://127.0.0.1:55480/sb-test/" + key + "?X-Amz-Algorithm=AWS4-HMAC-SHA256" +
		"&X-Amz-Credential=testing%2F20261126%2Fus-east-1%2Fs3%2Faws4_request&X-Amz-Date=20261126T120000Z"
	if want := base + "&X-Amz-Expires=900" +
		"&X-Amz-SignedHeaders=content-length%3Bcontent-type%3Bhost%3Bx-amz-acl%3Bx-amz-checksum-sha256" +
		"&X-Amz-Signature=ff3bddb3ae6669f9889c8d8cae38c077d5dd48bf6a5c5f3036bba1f1efe39201"; put != want {
		t.Errorf("\n got %s\nwant %s", put, want)
	}
	get, err := s3.presign("GET", key, nil, time.Minute, at)
	if err != nil {
		t.Fatal(err)
	}
	if want := base + "&X-Amz-Expires=60&X-Amz-SignedHeaders=host" +
		"&X-Amz-Signature=b7cdf33f45ee3a565e59b5805d13c5885aa4196002cac5773eab4325e9bc0b89"; get != want {
		t.Errorf("\n got %s\nwant %s", get, want)
	}
}

func TestAPathStyleURLNamesTheBucketInThePath(t *testing.T) {
	s3 := S3{Endpoint: "http://127.0.0.1:9000/", Region: "us-east-1", Bucket: "b",
		AccessKeyID: "k", SecretAccessKey: "s"}
	got, err := s3.presign("PUT", "p/tag_dump/ab", map[string]string{"Content-Type": "application/octet-stream"},
		time.Minute, time.Now())
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(got, "http://127.0.0.1:9000/b/p/tag_dump/ab?") ||
		!strings.Contains(got, "X-Amz-SignedHeaders=content-type%3Bhost&") {
		t.Fatal(got)
	}
}

// What botocore 1.43.117 (the backend's lock) presigned on 2026-11-26 with
// addressing_style "auto", the style Python asks for when store_s3_path_style is false
// (store/s3.py): with an endpoint_url, path style for a bucket with dots and one without.
func TestAutoAddressingIsPathStyleAsBotocoresIs(t *testing.T) {
	for endpoint, want := range map[string]map[string]string{
		"https://atl1.digitaloceanspaces.com": {
			"bucket":    "https://atl1.digitaloceanspaces.com/bucket/k/x",
			"my.bucket": "https://atl1.digitaloceanspaces.com/my.bucket/k/x",
		},
		"http://127.0.0.1:9000": {
			"bucket":    "http://127.0.0.1:9000/bucket/k/x",
			"my.bucket": "http://127.0.0.1:9000/my.bucket/k/x",
		},
	} {
		for bucket, url := range want {
			s3 := S3{Endpoint: endpoint, Region: "us-east-1", Bucket: bucket, AccessKeyID: "a", SecretAccessKey: "b"}
			got, err := s3.presign("PUT", "k/x", nil, time.Minute, time.Now())
			if err != nil || !strings.HasPrefix(got, url+"?") {
				t.Errorf("%s %s: %s %v", endpoint, bucket, got, err)
			}
		}
	}
}

type vectorCase struct {
	Name      string `json:"name"`
	Kind      string `json:"kind"`
	SHA256    string `json:"sha256"`
	Size      int64  `json:"size"`
	ObjectKey string `json:"object_key"`
	IndexKey  string `json:"index_key"`
	Before    *struct {
		BackendID *string        `json:"backend_id"`
		Slug      *string        `json:"slug"`
		Meta      map[string]any `json:"meta"`
		Verified  bool           `json:"verified"`
	} `json:"before"`
	Row       map[string]any `json:"row"`
	Committed map[string]any `json:"committed"`
}

type vectorFile struct {
	Prefix       string       `json:"prefix"`
	StagingNonce string       `json:"staging_nonce"`
	StagingKey   string       `json:"staging_key"`
	Cases        []vectorCase `json:"cases"`
}

func vectors(t *testing.T) (string, []vectorCase) {
	v := vectorsFile(t)
	return v.Prefix, v.Cases
}

func vectorsFile(t *testing.T) vectorFile {
	t.Helper()
	_, file, _, _ := runtime.Caller(0)
	raw, err := os.ReadFile(filepath.Join(filepath.Dir(file), "../../../../../backend/tests/fixtures/blob-upload-vectors.json"))
	if err != nil {
		t.Fatal(err)
	}
	var v vectorFile
	if err := json.Unmarshal(raw, &v); err != nil {
		t.Fatal(err)
	}
	return v
}

func TestTheKeysArePythons(t *testing.T) {
	v := vectorsFile(t)
	if got := StagingKey(v.Prefix, v.StagingNonce); got != v.StagingKey || !noncePattern.MatchString(v.StagingNonce) {
		t.Errorf("staging key %s", got)
	}
	prefix, cases := v.Prefix, v.Cases
	for _, c := range cases {
		if got := ObjectKey(prefix, c.Kind, c.SHA256); got != c.ObjectKey {
			t.Errorf("%s: object key %s", c.Name, got)
		}
		if got := IndexKey(c.Kind, c.SHA256); got != c.IndexKey {
			t.Errorf("%s: index key %s", c.Name, got)
		}
	}
}

// row is a store_blobs row as the vectors write it, read as JSON so its numbers compare.
func row(t *testing.T, pool *pgxpool.Pool, key string) map[string]any {
	t.Helper()
	var raw []byte
	err := pool.QueryRow(context.Background(), `SELECT json_build_object('key', key, 'sha256', sha256,
 'kind', kind, 'backend', backend, 'backend_id', backend_id, 'size', size, 'slug', slug, 'meta', meta,
 'verified', verified_at IS NOT NULL) FROM store_blobs WHERE key = $1`, key).Scan(&raw)
	if err != nil {
		t.Fatal(err)
	}
	var out map[string]any
	if err := json.Unmarshal(raw, &out); err != nil {
		t.Fatal(err)
	}
	return out
}

func TestTheRowIsTheOnePythonWrites(t *testing.T) {
	pool := testdb.New(t)
	ctx := context.Background()
	_, cases := vectors(t)
	for _, verified := range []bool{false, true} {
		if _, err := pool.Exec(ctx, "DELETE FROM store_blobs"); err != nil {
			t.Fatal(err)
		}
		writeCases(t, pool, cases, verified)
	}
}

// writeCases writes each case's row as the commit does, verified or not, and checks it:
// put's row, and with verified the commit's changes over it.
func writeCases(t *testing.T, pool *pgxpool.Pool, cases []vectorCase, verified bool) {
	t.Helper()
	ctx := context.Background()
	for _, c := range cases {
		if b := c.Before; b != nil {
			backendID := c.ObjectKey
			if b.BackendID != nil {
				backendID = *b.BackendID
			}
			meta, _ := json.Marshal(b.Meta)
			_, err := pool.Exec(ctx, `INSERT INTO store_blobs
 (key, sha256, kind, backend, backend_id, size, slug, meta, verified_at)
 VALUES ($1, $2, $3, 's3', $4, $5, $6, $7, CASE WHEN $8 THEN now() END)`,
				c.IndexKey, c.SHA256, c.Kind, backendID, c.Size, b.Slug, meta, b.Verified)
			if err != nil {
				t.Fatal(err)
			}
		}
		ref := Ref{SHA256: c.SHA256, Kind: c.Kind, Backend: "s3", BackendID: c.ObjectKey, Size: c.Size}
		err := pgx.BeginFunc(ctx, pool, func(tx pgx.Tx) error {
			if verified {
				_, err := index(ctx, tx, c.IndexKey, ref)
				return err
			}
			_, err := putIn(ctx, tx, c.IndexKey, ref)
			return err
		})
		if err != nil {
			t.Fatal(err)
		}
		want := map[string]any{}
		for k, v := range c.Row {
			want[k] = v
		}
		if verified {
			for k, v := range c.Committed {
				want[k] = v
			}
		}
		if got := row(t, pool, c.IndexKey); !reflect.DeepEqual(got, want) {
			t.Errorf("%s (verified %v):\n got %v\nwant %v", c.Name, verified, got, want)
		}
	}
}

func testKeys(t *testing.T) *seal.Keys {
	t.Helper()
	key := make([]byte, 32)
	for i := range key {
		key[i] = byte(i)
	}
	return &seal.Keys{Current: seal.NewKEK(key)}
}

func TestATicketOpensOnlyAsSigned(t *testing.T) {
	keys := testKeys(t)
	tk := ticket{Caller: "core", Kind: "tag_dump", SHA256: strings.Repeat("a", 64), Size: 9,
		MediaType: "application/octet-stream", Expires: time.Unix(1_800_000_000, 0).UTC(),
		Nonce: strings.Repeat("0f", 16)}
	id := tk.sign(keys.Current)
	got, err := openTicket(keys, id)
	if err != nil || got != tk {
		t.Fatalf("%+v %v", got, err)
	}
	body, mac, _ := strings.Cut(id, ".")
	forged := base64.URLEncoding.EncodeToString(bytes.Replace(must(base64.URLEncoding.DecodeString(body)),
		[]byte("\n9\n"), []byte("\n8\n"), 1)) + "." + mac
	other := &seal.Keys{Current: seal.NewKEK(make([]byte, 32))}
	for name, bad := range map[string]string{"forged": forged, "no mac": body, "empty": ""} {
		if _, err := openTicket(keys, bad); err == nil {
			t.Errorf("%s opened", name)
		}
	}
	if _, err := openTicket(other, id); err == nil {
		t.Error("another KEK's ticket opened")
	}
	// During a rotation the previous KEK's tickets still open.
	if _, err := openTicket(&seal.Keys{Current: other.Current, Previous: keys.Current}, id); err != nil {
		t.Error(err)
	}
}

func must[T any](v T, err error) T {
	if err != nil {
		panic(err)
	}
	return v
}

func TestTheSettingsResolveAsPythonsDo(t *testing.T) {
	pool := testdb.New(t)
	ctx := context.Background()
	keys := testKeys(t)
	sealed := seal.SealSecret(keys.Current, "the secret", "settings:store_s3_secret_access_key")
	secret, _ := json.Marshal(map[string]any{"sealed": map[string]string{
		"secret": base64.StdEncoding.EncodeToString(sealed.SecretSealed),
		"dek":    base64.StdEncoding.EncodeToString(sealed.DEKSealed),
		"kek_id": sealed.KEKID,
	}})
	for name, value := range map[string]string{
		"store_backend":              `"s3"`,
		"store_s3_bucket":            `"saved-bucket"`,
		"store_s3_prefix":            `"/p/"`,
		"store_s3_access_key_id":     `null`,
		"store_s3_secret_access_key": string(secret),
		"store_max_count":            `7`,
	} {
		if _, err := pool.Exec(ctx, "INSERT INTO settings (name, value) VALUES ($1, $2::jsonb)", name, value); err != nil {
			t.Fatal(err)
		}
	}
	env := map[string]string{
		"SCADBUDDY_STORE_S3_ENDPOINT":      "http://s3.test",
		"SCADBUDDY_STORE_S3_BUCKET":        "env-bucket",
		"SCADBUDDY_STORE_S3_ACCESS_KEY_ID": "env-key",
	}
	store, err := loadStore(ctx, pool, keys, lookup(env))
	if err != nil {
		t.Fatal(err)
	}
	// A saved clear beats the environment: no access key, so no S3 store.
	if store.Backend != "s3" || store.S3 != nil || store.MaxCount != 7 || store.MaxTotalBytes != 50<<30 {
		t.Fatalf("%+v", store)
	}
	if _, err := pool.Exec(ctx, "DELETE FROM settings WHERE name = 'store_s3_access_key_id'"); err != nil {
		t.Fatal(err)
	}
	store, err = loadStore(ctx, pool, keys, lookup(env))
	if err != nil {
		t.Fatal(err)
	}
	want := S3{Endpoint: "http://s3.test", Region: "us-east-1", Bucket: "saved-bucket", Prefix: "p",
		AccessKeyID: "env-key", SecretAccessKey: "the secret"}
	if store.S3 == nil || *store.S3 != want {
		t.Fatalf("%+v", store.S3)
	}
	// A secret sealed under a KEK this process lacks follows the environment.
	env["SCADBUDDY_STORE_S3_SECRET_ACCESS_KEY"] = "env-secret"
	store, err = loadStore(ctx, pool, &seal.Keys{Current: seal.NewKEK(make([]byte, 32))}, lookup(env))
	if err != nil || store.S3 == nil || store.S3.SecretAccessKey != "env-secret" {
		t.Fatalf("%+v %v", store.S3, err)
	}
	// A variable set to "" is set, as pydantic-settings reads it: an empty region is "",
	// and an empty prefix (none saved) is a blank one, so no S3 store, never the default.
	if _, err := pool.Exec(ctx, "DELETE FROM settings WHERE name = 'store_s3_prefix'"); err != nil {
		t.Fatal(err)
	}
	env["SCADBUDDY_STORE_S3_REGION"] = ""
	store, err = loadStore(ctx, pool, keys, lookup(env))
	if err != nil || store.S3 == nil || store.S3.Region != "" || store.S3.Prefix != "scadbuddy" {
		t.Fatalf("%+v %v", store.S3, err)
	}
	env["SCADBUDDY_STORE_S3_PREFIX"] = ""
	store, err = loadStore(ctx, pool, keys, lookup(env))
	if err != nil || store.S3 != nil {
		t.Fatalf("%+v %v", store.S3, err)
	}
}

// lookup is env as os.LookupEnv reads it.
func lookup(env map[string]string) func(string) (string, bool) {
	return func(name string) (string, bool) {
		v, ok := env[name]
		return v, ok
	}
}

// fakeS3 keeps the objects PUT to it, as S3 does a presigned PUT: the signed headers must be
// sent, and a body that is not its checksum is refused (BadDigest) unless noChecksum.
type fakeS3 struct {
	mu      sync.Mutex
	objects map[string][]byte
	puts    int
	// status, when set, is every PUT's answer.
	status int
	// noChecksum takes a body whatever its checksum, as a store that checks none would.
	noChecksum bool
	// afterGet, when set, runs after each GET is answered, under mu, with its path.
	afterGet func(path string)
	// beforeGet, when set, runs before each GET is looked up, under mu, with its path.
	beforeGet func(path string)
	// requests are each request's method and path.
	requests []string
	// lastPut is the last PUT's headers.
	lastPut http.Header
}

func (f *fakeS3) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	defer f.mu.Unlock()
	q := r.URL.Query()
	if q.Get("X-Amz-Signature") == "" || q.Get("X-Amz-Algorithm") != "AWS4-HMAC-SHA256" {
		w.WriteHeader(http.StatusForbidden)
		return
	}
	f.requests = append(f.requests, r.Method+" "+r.URL.Path)
	switch r.Method {
	case http.MethodPut:
		f.puts++
		f.lastPut = r.Header.Clone()
		if f.status != 0 {
			w.WriteHeader(f.status)
			return
		}
		for _, name := range strings.Split(q.Get("X-Amz-SignedHeaders"), ";") {
			if name != "host" && name != "content-length" && r.Header.Get(name) == "" {
				w.WriteHeader(http.StatusForbidden)
				return
			}
		}
		body, _ := io.ReadAll(r.Body)
		sum := sha256.Sum256(body)
		if !f.noChecksum && r.Header.Get("x-amz-checksum-sha256") != base64.StdEncoding.EncodeToString(sum[:]) {
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		f.objects[r.URL.Path] = body
	case http.MethodGet:
		if f.beforeGet != nil {
			f.beforeGet(r.URL.Path)
		}
		body, ok := f.objects[r.URL.Path]
		if !ok {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		_, _ = w.Write(body)
		if f.afterGet != nil {
			f.afterGet(r.URL.Path)
		}
	case http.MethodHead:
		if _, ok := f.objects[r.URL.Path]; !ok {
			w.WriteHeader(http.StatusNotFound)
		}
	case http.MethodDelete:
		delete(f.objects, r.URL.Path)
		w.WriteHeader(http.StatusNoContent)
	}
}

// object is the fake store's object at key, under the harness's bucket.
func (h *harness) object(key string) ([]byte, bool) {
	h.s3.mu.Lock()
	defer h.s3.mu.Unlock()
	body, ok := h.s3.objects["/"+h.bucket+"/"+key]
	return body, ok
}

// stagingKeys are the staging objects the fake store holds.
func (h *harness) stagingKeys() []string {
	h.s3.mu.Lock()
	defer h.s3.mu.Unlock()
	var keys []string
	for path := range h.s3.objects {
		if strings.HasPrefix(path, "/"+h.bucket+"/t/staging/") {
			keys = append(keys, path)
		}
	}
	return keys
}

// resolver takes "Bearer <caller>:<scope>,…" as that caller's credential.
type resolver struct{}

func (resolver) Resolve(_ context.Context, authorization string) *auth.Service {
	caller, scopes, ok := strings.Cut(strings.TrimPrefix(authorization, "Bearer "), ":")
	if !ok {
		return nil
	}
	return &auth.Service{Caller: caller, Scopes: strings.Split(scopes, ",")}
}

type harness struct {
	t      *testing.T
	pool   *pgxpool.Pool
	s3     *fakeS3
	api    string
	blobs  *Blobs
	now    time.Time
	bucket string
	// env is the store's settings the harness started with.
	env map[string]string
}

func newHarness(t *testing.T) *harness {
	t.Helper()
	pool := testdb.New(t)
	s3 := &fakeS3{objects: map[string][]byte{}}
	store := httptest.NewServer(s3)
	t.Cleanup(store.Close)
	h := &harness{t: t, pool: pool, s3: s3, now: time.Now(), bucket: "sb-test"}
	env := map[string]string{
		"SCADBUDDY_STORE_BACKEND":              "s3",
		"SCADBUDDY_STORE_S3_ENDPOINT":          store.URL,
		"SCADBUDDY_STORE_S3_BUCKET":            h.bucket,
		"SCADBUDDY_STORE_S3_PREFIX":            "t",
		"SCADBUDDY_STORE_S3_ACCESS_KEY_ID":     "testing",
		"SCADBUDDY_STORE_S3_SECRET_ACCESS_KEY": "testing",
	}
	h.env = env
	h.blobs = &Blobs{Pool: pool, Keys: testKeys(t), LookupEnv: lookup(env),
		HTTP: store.Client(), Stream: store.Client(), Now: func() time.Time { return h.now }}
	router := &crud.Router{Resolver: resolver{}, Next: http.NotFoundHandler()}
	router.AddShared(Area, h.blobs.Routes()...)
	api := httptest.NewServer(router)
	t.Cleanup(api.Close)
	h.api = api.URL + crud.Prefix + Area
	return h
}

func (h *harness) post(path, credential string, body any) (int, map[string]any) {
	h.t.Helper()
	raw, _ := json.Marshal(body)
	req, _ := http.NewRequest(http.MethodPost, h.api+path, bytes.NewReader(raw))
	if body == nil {
		req.Body = http.NoBody
	}
	req.Header.Set("Authorization", "Bearer "+credential)
	req.Header.Set("Content-Type", "application/json")
	res, err := http.DefaultClient.Do(req)
	if err != nil {
		h.t.Fatal(err)
	}
	defer res.Body.Close()
	var out map[string]any
	_ = json.NewDecoder(res.Body).Decode(&out)
	return res.StatusCode, out
}

const core = "core:crud.blobs"

func ask(body []byte) map[string]any {
	sum := sha256.Sum256(body)
	return map[string]any{"kind": "tag_dump", "size": len(body), "sha256": hex.EncodeToString(sum[:]),
		"media_type": "application/octet-stream"}
}

// put PUTs body to an upload's URL with its headers, as a client does.
func put(t *testing.T, upload map[string]any, body []byte) int {
	t.Helper()
	req, _ := http.NewRequest(http.MethodPut, upload["url"].(string), bytes.NewReader(body))
	for name, value := range upload["headers"].(map[string]any) {
		req.Header.Set(name, value.(string))
	}
	res, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	res.Body.Close()
	return res.StatusCode
}

func TestAnUploadIsPutAndCommittedAsPythonIndexesIt(t *testing.T) {
	h := newHarness(t)
	body := []byte("a Bambu tag's dump")
	status, upload := h.post("/uploads", core, ask(body))
	if status != http.StatusOK {
		t.Fatal(status, upload)
	}
	if got := put(t, upload, body); got != http.StatusOK {
		t.Fatal(got)
	}
	sum := sha256.Sum256(body)
	sha := hex.EncodeToString(sum[:])
	// The PUT wrote the upload's staging object, never the content key.
	if _, ok := h.object("t/tag_dump/" + sha); ok {
		t.Fatal("the PUT wrote the content key")
	}
	if staged := h.stagingKeys(); len(staged) != 1 {
		t.Fatalf("%v", staged)
	}
	id := upload["upload_id"].(string)
	for range 2 {
		// A commit sent again answers the same.
		status, got := h.post("/uploads/"+id+"/commit", core, nil)
		want := map[string]any{"key": "tag_dump-" + sha, "sha256": sha, "size": float64(len(body))}
		if status != http.StatusOK || !reflect.DeepEqual(got, want) {
			t.Fatal(status, got)
		}
	}
	if got, ok := h.object("t/tag_dump/" + sha); !ok || !bytes.Equal(got, body) {
		t.Fatalf("content object %q", got)
	}
	if staged := h.stagingKeys(); len(staged) != 0 {
		t.Fatalf("the staging object stayed: %v", staged)
	}
	r := row(t, h.pool, "tag_dump-"+sha)
	if r["backend_id"] != "t/tag_dump/"+sha || r["backend"] != "s3" || r["kind"] != "tag_dump" || r["verified"] != true {
		t.Fatal(r)
	}
}

// The content key is never presigned: the upload's URL names a staging key of
// Python's staging form, under the store's prefix.
func TestTheContentKeyIsNeverPresigned(t *testing.T) {
	h := newHarness(t)
	body := []byte("a dump")
	status, upload := h.post("/uploads", core, ask(body))
	if status != http.StatusOK {
		t.Fatal(status, upload)
	}
	u := must(url.Parse(upload["url"].(string)))
	staging, ok := strings.CutPrefix(u.Path, "/"+h.bucket+"/t/staging/")
	if !ok || !noncePattern.MatchString(staging) || strings.Contains(u.Path, "tag_dump") {
		t.Fatal(u.Path)
	}
	// Two uploads of the same bytes get two staging keys.
	_, again := h.post("/uploads", core, ask(body))
	if must(url.Parse(again["url"].(string))).Path == u.Path {
		t.Fatal("two uploads share a staging key")
	}
}

// A PUT to the staging key after the commit's check (an endpoint that takes any body)
// changes nothing indexed: the commit PUTs the bytes it verified.
func TestASecondPutToTheStagingKeyChangesNothingIndexed(t *testing.T) {
	h := newHarness(t)
	h.s3.noChecksum = true
	body := []byte("the verified dump")
	sum := sha256.Sum256(body)
	sha := hex.EncodeToString(sum[:])
	_, upload := h.post("/uploads", core, ask(body))
	if got := put(t, upload, body); got != http.StatusOK {
		t.Fatal(got)
	}
	// Right after the commit's read of the staging object, a second PUT replaces it.
	h.s3.afterGet = func(path string) {
		if strings.Contains(path, "/staging/") {
			h.s3.objects[path] = []byte("other bytes")
		}
	}
	status, got := h.post("/uploads/"+upload["upload_id"].(string)+"/commit", core, nil)
	if status != http.StatusOK {
		t.Fatal(status, got)
	}
	if got, _ := h.object("t/tag_dump/" + sha); !bytes.Equal(got, body) {
		t.Fatalf("content object %q", got)
	}
	// A PUT after the commit lands at the staging key again, never the content key.
	h.s3.afterGet = nil
	if got := put(t, upload, []byte("later bytes")); got != http.StatusOK {
		t.Fatal(got)
	}
	if got, _ := h.object("t/tag_dump/" + sha); !bytes.Equal(got, body) {
		t.Fatalf("content object %q", got)
	}
	if r := row(t, h.pool, "tag_dump-"+sha); r["verified"] != true || r["size"] != float64(len(body)) {
		t.Fatal(r)
	}
}

// A content object that is there already is never written, as store/s3.py `upload`
// skips it; the commit reads it and marks the row verified.
func TestAContentObjectThereAlreadyIsNotWritten(t *testing.T) {
	h := newHarness(t)
	body := []byte("a dump stored before")
	sum := sha256.Sum256(body)
	sha := hex.EncodeToString(sum[:])
	h.s3.objects["/"+h.bucket+"/t/tag_dump/"+sha] = body
	_, upload := h.post("/uploads", core, ask(body))
	if got := put(t, upload, body); got != http.StatusOK {
		t.Fatal(got)
	}
	h.s3.requests = nil
	status, got := h.post("/uploads/"+upload["upload_id"].(string)+"/commit", core, nil)
	if status != http.StatusOK {
		t.Fatal(status, got)
	}
	for _, request := range h.s3.requests {
		if strings.HasPrefix(request, "PUT ") {
			t.Fatalf("the commit wrote %s", request)
		}
	}
	if r := row(t, h.pool, "tag_dump-"+sha); r["verified"] != true {
		t.Fatal(r)
	}
	if staged := h.stagingKeys(); len(staged) != 0 {
		t.Fatalf("%v", staged)
	}
}

// verifiedAt is key's verified_at.
func verifiedAt(t *testing.T, h *harness, key string) time.Time {
	t.Helper()
	var at time.Time
	if err := h.pool.QueryRow(context.Background(),
		"SELECT verified_at FROM store_blobs WHERE key = $1", key).Scan(&at); err != nil {
		t.Fatal(err)
	}
	return at
}

// A content object that is not its key's bytes is a 409 store_object_mismatch: it is not
// overwritten, nothing is indexed, a row that named it verified before stays as it was,
// and the staging object stays, so the commit goes through once the object is repaired.
func TestAWrongContentObjectIsRefused(t *testing.T) {
	h := newHarness(t)
	body := []byte("a dump stored and verified")
	sum := sha256.Sum256(body)
	sha := hex.EncodeToString(sum[:])
	key := "tag_dump-" + sha
	content := "/" + h.bucket + "/t/tag_dump/" + sha
	_, upload := h.post("/uploads", core, ask(body))
	if got := put(t, upload, body); got != http.StatusOK {
		t.Fatal(got)
	}
	if status, got := h.post("/uploads/"+upload["upload_id"].(string)+"/commit", core, nil); status != http.StatusOK {
		t.Fatal(status, got)
	}
	stamped, before := verifiedAt(t, h, key), row(t, h.pool, key)
	// The store's object goes bad; another upload of the same bytes is committed.
	h.s3.objects[content] = []byte("corrupt")
	_, upload = h.post("/uploads", core, ask(body))
	if got := put(t, upload, body); got != http.StatusOK {
		t.Fatal(got)
	}
	id := upload["upload_id"].(string)
	status, got := h.post("/uploads/"+id+"/commit", core, nil)
	if status != http.StatusConflict || got["reason"] != "store_object_mismatch" {
		t.Fatal(status, got)
	}
	if got, _ := h.object("t/tag_dump/" + sha); string(got) != "corrupt" {
		t.Fatalf("%q", got)
	}
	if r, at := row(t, h.pool, key), verifiedAt(t, h, key); !reflect.DeepEqual(r, before) || !at.Equal(stamped) {
		t.Fatal(r, at, stamped)
	}
	if staged := h.stagingKeys(); len(staged) != 1 {
		t.Fatalf("%v", staged)
	}
	// With no row before, nothing is indexed either.
	other := []byte("another dump")
	osum := sha256.Sum256(other)
	osha := hex.EncodeToString(osum[:])
	h.s3.objects["/"+h.bucket+"/t/tag_dump/"+osha] = []byte("corrupt")
	_, oupload := h.post("/uploads", core, ask(other))
	if got := put(t, oupload, other); got != http.StatusOK {
		t.Fatal(got)
	}
	status, got = h.post("/uploads/"+oupload["upload_id"].(string)+"/commit", core, nil)
	if status != http.StatusConflict || got["reason"] != "store_object_mismatch" {
		t.Fatal(status, got)
	}
	var rows int
	if err := h.pool.QueryRow(context.Background(), "SELECT count(*) FROM store_blobs WHERE key = $1",
		"tag_dump-"+osha).Scan(&rows); err != nil || rows != 0 {
		t.Fatal(rows, err)
	}
	// Once the object is repaired, the first refused commit goes through.
	h.s3.mu.Lock()
	h.s3.objects[content] = body
	h.s3.mu.Unlock()
	if status, got := h.post("/uploads/"+id+"/commit", core, nil); status != http.StatusOK {
		t.Fatal(status, got)
	}
	if staged := h.stagingKeys(); len(staged) != 1 {
		t.Fatalf("only the other upload's staging object stays: %v", staged)
	}
}

// Two commits of one upload at once: when the other commit writes the content object and
// deletes the staging object between this commit's two reads, this one reads the content
// object again and answers as a repeated commit, not 409 upload_not_received.
func TestACommitRacingAnotherOfTheSameUploadSucceeds(t *testing.T) {
	h := newHarness(t)
	body := []byte("a dump committed twice")
	sum := sha256.Sum256(body)
	sha := hex.EncodeToString(sum[:])
	content := "/" + h.bucket + "/t/tag_dump/" + sha
	_, upload := h.post("/uploads", core, ask(body))
	if got := put(t, upload, body); got != http.StatusOK {
		t.Fatal(got)
	}
	// Just before this commit reads the staging object, the other commit has stored the
	// content and deleted the staging object.
	h.s3.beforeGet = func(path string) {
		if strings.Contains(path, "/staging/") {
			h.s3.objects[content] = h.s3.objects[path]
			delete(h.s3.objects, path)
		}
	}
	status, got := h.post("/uploads/"+upload["upload_id"].(string)+"/commit", core, nil)
	if status != http.StatusOK || got["sha256"] != sha {
		t.Fatal(status, got)
	}
	if r := row(t, h.pool, "tag_dump-"+sha); r["verified"] != true {
		t.Fatal(r)
	}
}

// A commit that finds every one of the stage lock's slots busy for SlotWait is a 503
// store_busy; once a slot is free, it goes through.
func TestACommitWaitsForASlotThenGivesUp(t *testing.T) {
	h := newHarness(t)
	h.blobs.SlotWait = 50 * time.Millisecond
	body := []byte("a dump behind busy slots")
	_, upload := h.post("/uploads", core, ask(body))
	if got := put(t, upload, body); got != http.StatusOK {
		t.Fatal(got)
	}
	h.blobs.slotsOnce.Do(func() { h.blobs.slots = make(chan struct{}, stageSlots) })
	for range stageSlots {
		h.blobs.slots <- struct{}{}
	}
	id := upload["upload_id"].(string)
	status, got := h.post("/uploads/"+id+"/commit", core, nil)
	if status != http.StatusServiceUnavailable || got["reason"] != "store_busy" {
		t.Fatal(status, got)
	}
	<-h.blobs.slots
	if status, got := h.post("/uploads/"+id+"/commit", core, nil); status != http.StatusOK {
		t.Fatal(status, got)
	}
}

// A removal that holds the object's lock through every try is a 503 store_busy; once it
// lets go, the commit goes through.
func TestACommitWaitsForARemovalThenGivesUp(t *testing.T) {
	h := newHarness(t)
	h.blobs.LockTimeout, h.blobs.LockTries = 50*time.Millisecond, 2
	body := []byte("a held dump")
	sum := sha256.Sum256(body)
	sha := hex.EncodeToString(sum[:])
	_, upload := h.post("/uploads", core, ask(body))
	if got := put(t, upload, body); got != http.StatusOK {
		t.Fatal(got)
	}
	ctx := context.Background()
	removal, err := h.pool.Acquire(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer removal.Release()
	if _, err := removal.Exec(ctx, "SELECT pg_advisory_lock(hashtextextended($1, 0))", objectLock(backendS3, sha)); err != nil {
		t.Fatal(err)
	}
	id := upload["upload_id"].(string)
	status, got := h.post("/uploads/"+id+"/commit", core, nil)
	if status != http.StatusServiceUnavailable || got["reason"] != "store_busy" {
		t.Fatal(status, got)
	}
	if _, err := removal.Exec(ctx, "SELECT pg_advisory_unlock_all()"); err != nil {
		t.Fatal(err)
	}
	if status, got := h.post("/uploads/"+id+"/commit", core, nil); status != http.StatusOK {
		t.Fatal(status, got)
	}
	// The commit's lock went with its connection: a removal takes it at once.
	var free bool
	if err := removal.QueryRow(ctx, "SELECT pg_try_advisory_lock(hashtextextended($1, 0))",
		objectLock(backendS3, sha)).Scan(&free); err != nil || !free {
		t.Fatal(free, err)
	}
	if _, err := removal.Exec(ctx, "SELECT pg_advisory_unlock_all()"); err != nil {
		t.Fatal(err)
	}
}

func TestARefusedUpload(t *testing.T) {
	h := newHarness(t)
	body := []byte("dump")
	for name, c := range map[string]struct {
		credential string
		body       map[string]any
		status     int
		reason     string
	}{
		"no credential":                  {"", ask(body), 401, "no_credential"},
		"another scope":                  {"fleet:crud.fleet", ask(body), 403, "out_of_scope"},
		"a kind not listed":              {core, map[string]any{"kind": "piece", "size": 4, "sha256": ask(body)["sha256"], "media_type": "a/b"}, 422, "kind_not_allowed"},
		"a photo, which only Put stores": {core, map[string]any{"kind": "scan_image", "size": 4, "sha256": ask(body)["sha256"], "media_type": "image/jpeg"}, 422, "kind_not_allowed"},
		"too large":                      {core, map[string]any{"kind": "tag_dump", "size": 64<<10 + 1, "sha256": ask(body)["sha256"], "media_type": "a/b"}, 413, "upload_too_large"},
	} {
		status, got := h.post("/uploads", c.credential, c.body)
		if status != c.status || got["reason"] != c.reason {
			t.Errorf("%s: %d %v", name, status, got)
		}
	}
	status, got := h.post("/uploads", core, map[string]any{"kind": "tag_dump", "size": -1, "sha256": "x", "media_type": "a/b"})
	if status != 422 || got["errors"] == nil {
		t.Errorf("a malformed body: %d %v", status, got)
	}
}

func TestTheStoresCapsRefuseAnUpload(t *testing.T) {
	h := newHarness(t)
	if _, err := h.pool.Exec(context.Background(),
		`INSERT INTO settings (name, value) VALUES ('store_max_count', '1'::jsonb)`); err != nil {
		t.Fatal(err)
	}
	if _, err := h.pool.Exec(context.Background(), `INSERT INTO store_blobs
 (key, sha256, kind, backend, backend_id, size) VALUES ('k', 's', 'piece', 's3', 't/piece/s', 1)`); err != nil {
		t.Fatal(err)
	}
	status, got := h.post("/uploads", core, ask([]byte("x")))
	if status != http.StatusInsufficientStorage || got["reason"] != "store_full" {
		t.Fatal(status, got)
	}
}

// #8195: with no cap set, room runs no usage aggregate, as store/content.py
// `_require_room` does since #8110. The Blobs has no pool, so a query would panic.
func TestRoomWithNoCapRunsNoUsageQuery(t *testing.T) {
	if err := (&Blobs{}).room(context.Background(), &Store{}, 1<<30); err != nil {
		t.Fatal(err)
	}
}

func TestACommitRefusesWhatWasNotUploaded(t *testing.T) {
	h := newHarness(t)
	body := []byte("the dump asked for")
	_, upload := h.post("/uploads", core, ask(body))
	id := upload["upload_id"].(string)
	status, got := h.post("/uploads/"+id+"/commit", core, nil)
	if status != http.StatusConflict || got["reason"] != "upload_not_received" {
		t.Fatal(status, got)
	}
	// Other bytes at the staging object, as a store that took no checksum would keep them.
	h.s3.noChecksum = true
	if got := put(t, upload, []byte("other bytes")); got != http.StatusOK {
		t.Fatal(got)
	}
	status, got = h.post("/uploads/"+id+"/commit", core, nil)
	if status != http.StatusUnprocessableEntity || got["reason"] != "upload_mismatch" {
		t.Fatal(status, got)
	}
	var n int
	if err := h.pool.QueryRow(context.Background(), "SELECT count(*) FROM store_blobs").Scan(&n); err != nil || n != 0 {
		t.Fatal(n, err)
	}
	sum := sha256.Sum256(body)
	if _, ok := h.object("t/tag_dump/" + hex.EncodeToString(sum[:])); ok {
		t.Fatal("a mismatch wrote the content key")
	}
	// Another caller's upload is no upload of this one's.
	status, got = h.post("/uploads/"+id+"/commit", "fleet:crud.blobs", nil)
	if status != http.StatusNotFound || got["reason"] != "upload_not_found" {
		t.Fatal(status, got)
	}
	status, got = h.post("/uploads/not.a-ticket/commit", core, nil)
	if status != http.StatusNotFound || got["reason"] != "upload_not_found" {
		t.Fatal(status, got)
	}
}

func TestAnExpiredUploadIsRefused(t *testing.T) {
	h := newHarness(t)
	body := []byte("late")
	_, upload := h.post("/uploads", core, ask(body))
	if got := put(t, upload, body); got != http.StatusOK {
		t.Fatal(got)
	}
	h.now = h.now.Add(urlTTL + commitGrace + time.Second)
	status, got := h.post("/uploads/"+upload["upload_id"].(string)+"/commit", core, nil)
	if status != http.StatusGone || got["reason"] != "upload_expired" {
		t.Fatal(status, got)
	}
}

func TestOnlyTheS3StoreTakesUploads(t *testing.T) {
	h := newHarness(t)
	if _, err := h.pool.Exec(context.Background(),
		`INSERT INTO settings (name, value) VALUES ('store_backend', '"bambuddy"'::jsonb)`); err != nil {
		t.Fatal(err)
	}
	status, got := h.post("/uploads", core, ask([]byte("x")))
	if status != http.StatusServiceUnavailable || got["reason"] != "not_configured" {
		t.Fatal(status, got)
	}
}
