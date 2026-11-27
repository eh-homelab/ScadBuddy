package tagreads

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/auth"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/blobstore"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/devices"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/seal"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/testdb"
)

func TestMain(m *testing.M) { testdb.Main(m) }

// The owner's dump of the two tags of one spool, read 2026-11-26 (#7905): data blocks only.
// Blocks 1, 2, 4, 5, 6, 8, 9, 10 and 12 are the same on both tags.
const (
	uidA   = "9CC69CE3"
	uidB   = "DCAE1CE1"
	tray   = "6DB40AFAF762423DBD73428BDA79DCD0"
	shared = "1:4131372d503100004746413137000000 2:504c4100000000000000000000000000 4:504c41205472616e736c7563656e7400 " +
		"5:b8acd680e80300000000e03f00000000 6:3700080000000000f000c80000000000 8:00000000000000000000000000000000 " +
		"9:6db40afaf762423dbd73428bda79dcd0 10:020000009a0200000000000000000000 12:323032365f30365f32335f30355f3033"
	block0A = "0:9cc69ce325080400056cdd0f4628c890"
	block0B = "0:dcae1ce18f0804000526c8cd4dc0cd90"
)

type block struct {
	Index   int    `json:"index"`
	DataHex string `json:"data_hex"`
}

func blocksOf(dump string) []block {
	var out []block
	for _, f := range strings.Fields(dump) {
		i, h, _ := strings.Cut(f, ":")
		var n int
		for _, c := range i {
			n = n*10 + int(c-'0')
		}
		out = append(out, block{n, h})
	}
	return out
}

// fakeS3 keeps what is PUT to it; any presigned request is taken.
type fakeS3 struct {
	mu      sync.Mutex
	objects map[string][]byte
}

func (f *fakeS3) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	defer f.mu.Unlock()
	switch r.Method {
	case http.MethodPut:
		f.objects[r.URL.Path], _ = io.ReadAll(r.Body)
	case http.MethodGet:
		body, ok := f.objects[r.URL.Path]
		if !ok {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		_, _ = w.Write(body)
	}
}

// python stands in for the Python API: it keeps each body and answers a TagReadResult.
type python struct {
	mu     sync.Mutex
	bodies []map[string]any
	status int
	answer string
}

func (p *python) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	body, _ := io.ReadAll(r.Body)
	var got map[string]any
	_ = json.Unmarshal(body, &got)
	p.mu.Lock()
	p.bodies = append(p.bodies, got)
	status, answer := p.status, p.answer
	p.mu.Unlock()
	if status == 0 {
		status = http.StatusOK
	}
	if answer == "" {
		answer = `{"tag":{"id":"t"},"spool":null,"decoded":null,"draft":null,"resolved":null,"next":"bind_or_create"}`
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_, _ = io.WriteString(w, answer)
}

type harness struct {
	// who is the caller of each request; the zero Caller is none (auth off).
	who   auth.Caller
	t     *testing.T
	pool  *pgxpool.Pool
	s3    *fakeS3
	py    *python
	url   string
	reads *Reads
}

func newHarness(t *testing.T) *harness {
	t.Helper()
	pool := testdb.New(t)
	s3 := &fakeS3{objects: map[string][]byte{}}
	store := httptest.NewServer(s3)
	t.Cleanup(store.Close)
	env := map[string]string{
		"SCADBUDDY_STORE_BACKEND":              "s3",
		"SCADBUDDY_STORE_S3_ENDPOINT":          store.URL,
		"SCADBUDDY_STORE_S3_BUCKET":            "sb-test",
		"SCADBUDDY_STORE_S3_PREFIX":            "t",
		"SCADBUDDY_STORE_S3_ACCESS_KEY_ID":     "testing",
		"SCADBUDDY_STORE_S3_SECRET_ACCESS_KEY": "testing",
	}
	key := make([]byte, 32)
	blobs := &blobstore.Blobs{Pool: pool, Keys: &seal.Keys{Current: seal.NewKEK(key)},
		LookupEnv: func(n string) (string, bool) { v, ok := env[n]; return v, ok },
		HTTP:      store.Client(), Stream: store.Client(), Now: time.Now}
	py := &python{}
	reads := &Reads{Pool: pool, Blobs: blobs, Upstream: py, Devices: &devices.Store{Pool: pool}}
	h := &harness{t: t, pool: pool, s3: s3, py: py, reads: reads}
	mux := http.NewServeMux()
	for _, p := range reads.Public()[Area] {
		handler := p.Handler
		mux.Handle(p.Pattern, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if h.who.Kind != "" {
				r = r.WithContext(auth.WithCaller(r.Context(), h.who))
			}
			handler.ServeHTTP(w, r)
		}))
	}
	api := httptest.NewServer(mux)
	t.Cleanup(api.Close)
	h.url = api.URL + Path
	return h
}

func (h *harness) post(body any) (int, map[string]any) {
	h.t.Helper()
	raw, _ := json.Marshal(body)
	res, err := http.Post(h.url, "application/json", bytes.NewReader(raw))
	if err != nil {
		h.t.Fatal(err)
	}
	defer res.Body.Close()
	var out map[string]any
	_ = json.NewDecoder(res.Body).Decode(&out)
	return res.StatusCode, out
}

func (h *harness) exec(sql string, args ...any) {
	h.t.Helper()
	if _, err := h.pool.Exec(context.Background(), sql, args...); err != nil {
		h.t.Fatal(err)
	}
}

func (h *harness) one(sql string, args ...any) any {
	h.t.Helper()
	var v any
	if err := h.pool.QueryRow(context.Background(), sql, args...).Scan(&v); err != nil {
		h.t.Fatal(err)
	}
	return v
}

func (h *harness) tagOf(uid string) string {
	h.t.Helper()
	var id string
	if err := h.pool.QueryRow(context.Background(),
		"SELECT tag_id::text FROM filament_tag_uids WHERE uid = $1", uid).Scan(&id); err != nil {
		h.t.Fatalf("uid %s: %v", uid, err)
	}
	return id
}

func mifareRead(uid, dump string) map[string]any {
	return map[string]any{"uid": uid, "tag_type": "mifare_classic", "records": []any{}, "mifare_blocks": blocksOf(dump)}
}

func TestBothSidesOfOneSpoolFoldIntoTheTagOfTheirTray(t *testing.T) {
	h := newHarness(t)
	// Prod's rows: a phone's Web NFC scans made one UID-only `other` tag per side.
	h.exec("INSERT INTO filament_tags (kind, tag_uid) VALUES ('other', $1), ('other', $2)",
		uidA+"00000100", uidB+"00000100")
	before := h.tagOf(uidB + "00000100")

	status, answer := h.post(mifareRead(uidA, block0A+" "+shared))
	if status != http.StatusOK {
		t.Fatalf("A: %d %v", status, answer)
	}
	bambu := answer["bambu"].(map[string]any)
	if bambu["tray_uid"] != tray || bambu["detailed_filament_type"] != "PLA Translucent" || bambu["color"] != "#B8ACD680" {
		t.Errorf("bambu = %v", bambu)
	}
	if answer["next"] != "bind_or_create" || answer["tag_read_id"] == nil || answer["dump_key"] == nil {
		t.Errorf("answer = %v", answer)
	}
	status, answer = h.post(mifareRead(uidB, block0B+" "+shared))
	if status != http.StatusOK {
		t.Fatalf("B: %d %v", status, answer)
	}

	tag := h.tagOf(uidA + "00000100")
	if got := h.tagOf(uidB + "00000100"); got != tag {
		t.Fatalf("the two sides are tags %s and %s", tag, got)
	}
	if got := h.one("SELECT count(*) FROM filament_tags WHERE id = $1::uuid", before); got != int64(0) && tag != before {
		t.Errorf("the folded row is still there")
	}
	if got := h.one("SELECT count(*) FROM filament_tags"); got != int64(1) {
		t.Errorf("%v tag rows, want 1", got)
	}
	if got := h.one("SELECT tray_uuid FROM filament_tags WHERE id = $1::uuid", tag); got != tray {
		t.Errorf("tray_uuid = %v", got)
	}
	if got := h.one("SELECT kind FROM filament_tags WHERE id = $1::uuid", tag); got != "bambu_mifare" {
		t.Errorf("kind = %v", got)
	}
	// A partial read writes no raw_blob.
	if got := h.one("SELECT raw_blob FROM filament_tags WHERE id = $1::uuid", tag); got != nil {
		t.Errorf("raw_blob = %v from a partial read", got)
	}
	if got := h.one("SELECT count(*) FROM filament_tag_reads WHERE tag_id = $1::uuid", tag); got != int64(2) {
		t.Errorf("%v reads, want 2", got)
	}
	if got := h.one("SELECT count(*) FROM blob_refs WHERE holder_kind = 'tag_read'"); got != int64(2) {
		t.Errorf("%v dump holds, want 2", got)
	}
	// The stored decode keeps its version and no raw block; the dump holds them.
	if got := h.one("SELECT decoded->'blocks' = '[]'::jsonb AND (decoded->>'decoder_version')::int = 1 FROM filament_tag_reads LIMIT 1"); got != true {
		t.Errorf("decoded keeps raw blocks or no version")
	}

	// Python got UID-only reads: no blocks, no records.
	for _, b := range h.py.bodies {
		if _, ok := b["mifare_blocks"]; ok || len(b["records"].([]any)) != 0 {
			t.Errorf("forwarded %v", b)
		}
	}
	if h.py.bodies[1]["uid"] != uidB {
		t.Errorf("forwarded uid %v", h.py.bodies[1]["uid"])
	}
	// Each dump is in the store, without key trailers.
	h.s3.mu.Lock()
	defer h.s3.mu.Unlock()
	if len(h.s3.objects) != 2 {
		t.Fatalf("%d objects, want 2", len(h.s3.objects))
	}
	for path, body := range h.s3.objects {
		if !strings.HasPrefix(path, "/sb-test/t/tag_dump/") {
			t.Errorf("object %s", path)
		}
		var d dump
		if err := json.Unmarshal(body, &d); err != nil || len(d.Blocks) != 10 || d.Format != dumpFormat {
			t.Errorf("dump %s: %v %+v", path, err, d)
		}
	}
}

func TestAReadMakesTheTagAndAKeyTrailerIsNeverStored(t *testing.T) {
	h := newHarness(t)
	status, answer := h.post(mifareRead(uidA+"00000100", block0A+" "+shared+" 3:ffffffffffffff078069ffffffffffff"))
	if status != http.StatusOK {
		t.Fatalf("%d %v", status, answer)
	}
	tag := h.tagOf(uidA + "00000100")
	if got := h.one("SELECT tag_uid || ' ' || tray_uuid || ' ' || kind FROM filament_tags WHERE id = $1::uuid", tag); got != uidA+"00000100 "+tray+" bambu_mifare" {
		t.Errorf("tag = %v", got)
	}
	h.s3.mu.Lock()
	defer h.s3.mu.Unlock()
	for _, body := range h.s3.objects {
		if bytes.Contains(bytes.ToUpper(body), []byte("FFFFFFFFFFFFFF078069")) {
			t.Error("the dump holds a key trailer")
		}
	}
}

// complete is A's dump with every other data block read as zeros.
func complete() string {
	given := map[int]bool{}
	for _, b := range blocksOf(block0A + " " + shared) {
		given[b.Index] = true
	}
	dump := block0A + " " + shared
	for i := range 64 {
		if i%4 != 3 && !given[i] {
			dump += " " + strconv.Itoa(i) + ":00000000000000000000000000000000"
		}
	}
	return dump
}

func TestOnlyACompleteReadWritesRawBlob(t *testing.T) {
	h := newHarness(t)
	status, answer := h.post(mifareRead(uidA, complete()))
	if status != http.StatusOK {
		t.Fatalf("%d %v", status, answer)
	}
	if answer["bambu"].(map[string]any)["complete"] != true {
		t.Fatalf("bambu = %v", answer["bambu"])
	}
	tag := h.tagOf(uidA + "00000100")
	if got := h.one("SELECT raw_blob FROM filament_tags WHERE id = $1::uuid", tag); got != answer["dump_key"] {
		t.Errorf("raw_blob = %v, want %v", got, answer["dump_key"])
	}
	// A later partial read keeps it.
	if status, answer := h.post(mifareRead(uidA, block0A+" "+shared)); status != http.StatusOK {
		t.Fatalf("%d %v", status, answer)
	}
	if got := h.one("SELECT raw_blob IS NOT NULL FROM filament_tags WHERE id = $1::uuid", tag); got != true {
		t.Error("a partial read cleared raw_blob")
	}
}

// Review of #8161: a complete read with tap false holds no dump, so it names none in raw_blob;
// the dump a tap named stays held.
func TestANoTapReadNamesNoUnheldDump(t *testing.T) {
	h := newHarness(t)
	body := mifareRead(uidA, complete())
	body["tap"] = false
	if status, answer := h.post(body); status != http.StatusOK {
		t.Fatalf("%d %v", status, answer)
	}
	tag := h.tagOf(uidA + "00000100")
	if got := h.one("SELECT raw_blob FROM filament_tags WHERE id = $1::uuid", tag); got != nil {
		t.Errorf("raw_blob = %v from a read that holds no dump", got)
	}
	status, answer := h.post(mifareRead(uidA, complete()))
	if status != http.StatusOK {
		t.Fatalf("%d %v", status, answer)
	}
	body["tap"] = false
	if status, answer := h.post(body); status != http.StatusOK {
		t.Fatalf("%d %v", status, answer)
	}
	if got := h.one(`SELECT count(*) FROM filament_tags t JOIN blob_refs r ON r.key = t.raw_blob
 AND r.holder_kind = 'tag_read' WHERE t.id = $1::uuid`, tag); got != int64(1) {
		t.Errorf("raw_blob's dump has %v holds, want 1", got)
	}
}

func TestAReadOfANewSideOfAKnownTrayAddsItsUID(t *testing.T) {
	h := newHarness(t)
	h.exec("INSERT INTO filament_tags (kind, tag_uid, tray_uuid) VALUES ('bambu_mifare', $1, $2)", uidA+"00000100", tray)
	if status, answer := h.post(mifareRead(uidB, block0B+" "+shared)); status != http.StatusOK {
		t.Fatalf("%d %v", status, answer)
	}
	if h.tagOf(uidA+"00000100") != h.tagOf(uidB+"00000100") {
		t.Error("side B is not on side A's tag")
	}
	if got := h.one("SELECT tag_uid FROM filament_tags"); got != uidA+"00000100" {
		t.Errorf("the first UID changed: %v", got)
	}
}

// Review of #8161: a UID a read adds is the read's, never the trigger's 'tag', whether the
// tag is new or a tray's tag with no UID yet.
func TestAUIDAReadAddsKeepsTheReadsSource(t *testing.T) {
	h := newHarness(t)
	h.exec("INSERT INTO filament_tags (kind, tray_uuid) VALUES ('bambu_mifare', $1)", tray)
	if status, answer := h.post(mifareRead(uidB, block0B+" "+shared)); status != http.StatusOK {
		t.Fatalf("%d %v", status, answer)
	}
	if got := h.one("SELECT string_agg(uid || ' ' || source, ',') FROM filament_tag_uids"); got != uidB+"00000100 app" {
		t.Errorf("uids = %v", got)
	}
	if got := h.one("SELECT tag_uid FROM filament_tags WHERE tray_uuid = $1", tray); got != uidB+"00000100" {
		t.Errorf("tag_uid = %v", got)
	}
}

func TestANewTagsUIDKeepsTheReadsSource(t *testing.T) {
	h := newHarness(t)
	if status, answer := h.post(mifareRead(uidA, block0A+" "+shared)); status != http.StatusOK {
		t.Fatalf("%d %v", status, answer)
	}
	if got := h.one("SELECT source FROM filament_tag_uids WHERE uid = $1", uidA+"00000100"); got != "app" {
		t.Errorf("source = %v", got)
	}
}

// Review of #8161: the diameter is rounded before its bounds are checked, so the draft never
// holds one SpoolCreate (more than 0, less than 100) refuses.
func TestADraftsDiameterIsRoundedThenChecked(t *testing.T) {
	for in, want := range map[float32]string{1.749: "1.75", 99.994: "99.99", 99.996: "none", 0.004: "none", 0.006: "0.01", -1: "none"} {
		v := in
		got := "none"
		if d := diameter(&v); d != nil {
			got = strconv.FormatFloat(*d, 'f', -1, 64)
		}
		if got != want {
			t.Errorf("%v: %s, want %s", in, got, want)
		}
	}
}

func TestATokenOrServiceReadIsAnAgents(t *testing.T) {
	for kind, want := range map[auth.Kind]string{auth.KindPerson: "phone", auth.KindAnonymous: "phone", auth.KindToken: "agent", auth.KindService: "agent"} {
		if got := sourceOf(auth.Caller{Kind: kind}, "phone"); got != want {
			t.Errorf("%s: %s, want %s", kind, got, want)
		}
	}
}

func TestTheFoldMovesABindingAndRefusesTwo(t *testing.T) {
	h := newHarness(t)
	h.exec("INSERT INTO filament_tags (kind, tag_uid, tray_uuid) VALUES ('bambu_mifare', $1, $2)", uidA+"00000100", tray)
	h.exec("INSERT INTO filament_tags (kind, tag_uid) VALUES ('other', $1)", uidB+"00000100")
	material := h.one("INSERT INTO filament_materials (name) VALUES ('Fold test') RETURNING id::text")
	spool := h.one("INSERT INTO filament_spools (material_id) VALUES ($1::uuid) RETURNING id::text", material)
	h.exec("INSERT INTO filament_tag_bindings (tag_id, spool_id) VALUES ($1::uuid, $2::uuid)", h.tagOf(uidB+"00000100"), spool)
	if status, answer := h.post(mifareRead(uidB, block0B+" "+shared)); status != http.StatusOK {
		t.Fatalf("%d %v", status, answer)
	}
	tag := h.tagOf(uidA + "00000100")
	if got := h.one("SELECT tag_id::text FROM filament_tag_bindings WHERE spool_id = $1::uuid", spool); got != tag {
		t.Errorf("the binding is on %v, want %v", got, tag)
	}
	// Review of #8161: the bound tag gained a tray UUID, so Bambuddy's link is pushed again.
	if got := h.one("SELECT count(*) FROM filament_bb_tag_pending WHERE tag_id = $1::uuid", tag); got != int64(1) {
		t.Errorf("%v pending rows for the folded tag, want 1", got)
	}

	// Two bound tags are never folded.
	h.exec("INSERT INTO filament_tags (kind, tag_uid) VALUES ('other', 'AABBCCDD00000100')")
	other := h.one("INSERT INTO filament_spools (material_id) VALUES ($1::uuid) RETURNING id::text", material)
	h.exec("INSERT INTO filament_tag_bindings (tag_id, spool_id) VALUES ($1::uuid, $2::uuid)", h.tagOf("AABBCCDD00000100"), other)
	status, answer := h.post(mifareRead("AABBCCDD", "0:aabbccdd000000000000000000000000 "+shared))
	if status != http.StatusConflict || answer["code"] != "tags_both_bound" {
		t.Errorf("%d %v", status, answer)
	}
	if got := h.one("SELECT count(*) FROM filament_tag_reads"); got != int64(1) {
		t.Errorf("a refused read left %v rows", got)
	}
}

// Review of #8161: a bound UID-only tag that a read gives a tray UUID is queued for the push;
// an unbound one is not.
func TestABoundTagThatGainsATrayIsQueued(t *testing.T) {
	for _, bound := range []bool{true, false} {
		h := newHarness(t)
		tag := h.one("INSERT INTO filament_tags (kind, tag_uid) VALUES ('other', $1) RETURNING id::text", uidA+"00000100")
		if bound {
			material := h.one("INSERT INTO filament_materials (name) VALUES ('Queue test') RETURNING id::text")
			spool := h.one("INSERT INTO filament_spools (material_id) VALUES ($1::uuid) RETURNING id::text", material)
			h.exec("INSERT INTO filament_tag_bindings (tag_id, spool_id) VALUES ($1::uuid, $2::uuid)", tag, spool)
			h.exec("DELETE FROM filament_bb_tag_pending")
		}
		if status, answer := h.post(mifareRead(uidA, block0A+" "+shared)); status != http.StatusOK {
			t.Fatalf("%d %v", status, answer)
		}
		if got := h.one("SELECT tray_uuid FROM filament_tags WHERE id = $1::uuid", tag); got != tray {
			t.Errorf("tray_uuid = %v", got)
		}
		want := int64(0)
		if bound {
			want = 1
		}
		if got := h.one("SELECT count(*) FROM filament_bb_tag_pending WHERE tag_id = $1::uuid", tag); got != want {
			t.Errorf("bound %v: %v pending rows, want %v", bound, got, want)
		}
	}
}

// Review of #8161: a MIFARE read that binds a spool is the page binding a tag it just read, as
// for an NDEF read: no second history row.
func TestAMifareReadThatBindsWritesNoRow(t *testing.T) {
	h := newHarness(t)
	body := mifareRead(uidA, block0A+" "+shared)
	body["bind_spool_id"] = "00000000-0000-0000-0000-000000000001"
	if status, answer := h.post(body); status != http.StatusOK || answer["tag_read_id"] != nil {
		t.Fatalf("%d %v", status, answer)
	}
	if got := h.one("SELECT count(*) FROM filament_tag_reads"); got != int64(0) {
		t.Errorf("rows = %v", got)
	}
	if h.py.bodies[0]["bind_spool_id"] == nil {
		t.Errorf("python did not get the bind: %v", h.py.bodies[0])
	}
}

func TestAUIDOfAnotherTrayIsAClash(t *testing.T) {
	h := newHarness(t)
	h.exec("INSERT INTO filament_tags (kind, tag_uid, tray_uuid) VALUES ('bambu_mifare', $1, 'D3B5D4BCA6044374A9B52E9C2DC9AC6A')", uidA+"00000100")
	status, answer := h.post(mifareRead(uidA, block0A+" "+shared))
	if status != http.StatusConflict || answer["code"] != "tag_keys_clash" {
		t.Errorf("%d %v", status, answer)
	}
}

func TestABadReadIsRefused(t *testing.T) {
	h := newHarness(t)
	for name, body := range map[string]map[string]any{
		"no uid":       {"mifare_blocks": blocksOf(shared)},
		"7-byte uid":   {"uid": "04112233445566", "mifare_blocks": blocksOf(shared)},
		"short block":  {"uid": uidA, "mifare_blocks": []block{{1, "00"}}},
		"index 64":     {"uid": uidA, "mifare_blocks": []block{{64, "00000000000000000000000000000000"}}},
		"with records": {"uid": uidA, "records": []any{map[string]any{"record_type": "text", "data_hex": "00"}}, "mifare_blocks": blocksOf(shared)},
		"long type":    {"uid": uidA, "tag_type": strings.Repeat("x", 33), "mifare_blocks": blocksOf(shared)},
		"not a list":   {"uid": uidA, "mifare_blocks": "1:00"},
	} {
		// Core's own check, before it records: the page may post the read again by its UID.
		if status, answer := h.post(body); status != http.StatusUnprocessableEntity || answer["code"] != "mifare_invalid" {
			t.Errorf("%s: %d %v", name, status, answer)
		}
	}
	// Block 0 names another UID.
	if status, answer := h.post(mifareRead(uidB, block0A+" "+shared)); status != http.StatusUnprocessableEntity || answer["code"] != "mifare_unreadable" {
		t.Errorf("block 0 of another tag: %d %v", status, answer)
	}
	if len(h.py.bodies) != 0 {
		t.Errorf("a refused read reached Python: %v", h.py.bodies)
	}
}

func TestAnNDEFReadGoesToPythonUnchanged(t *testing.T) {
	h := newHarness(t)
	h.py.answer = `{"next":"none"}`
	body := map[string]any{"uid": "53F64B94420001", "tag_type": nil, "records": []any{}, "mifare_blocks": nil}
	status, answer := h.post(body)
	if status != http.StatusOK || answer["next"] != "none" || answer["bambu"] != nil {
		t.Errorf("%d %v", status, answer)
	}
	if len(h.py.bodies) != 1 || h.py.bodies[0]["uid"] != "53F64B94420001" {
		t.Errorf("python got %v", h.py.bodies)
	}
}

func TestAnNDEFReadIsInTheHistoryOfPythonsTag(t *testing.T) {
	h := newHarness(t)
	tag := h.one("INSERT INTO filament_tags (kind, tag_uid) VALUES ('ntag_openspool', '53F64B94420001') RETURNING id::text")
	h.py.answer = `{"tag":{"id":"` + tag.(string) + `"},"spool":null,"decoded":null,"draft":null,"next":"bind_or_create"}`
	records := []any{map[string]any{"record_type": "mime", "media_type": "application/json", "data_hex": "7b7d"}}
	status, answer := h.post(map[string]any{"uid": "53:f6:4b:94:42:00:01", "tag_type": nil, "records": records})
	if status != http.StatusOK || answer["tag_read_id"] == nil {
		t.Fatalf("%d %v", status, answer)
	}
	got := h.one(`SELECT source || ' ' || uid || ' ' || (records->0->>'data_hex') || ' ' || (dump_key IS NULL)
 FROM filament_tag_reads WHERE tag_id = $1::uuid`, tag)
	if got != "phone 53F64B94420001 7b7d true" {
		t.Errorf("row = %v", got)
	}
}

// Review of #8161: a read with no Bambu field (no tray UUID, no filament type) is no Bambu tag.
func TestAReadWithNoBambuFieldsMakesNoBambuTag(t *testing.T) {
	h := newHarness(t)
	if status, answer := h.post(mifareRead(uidA, block0A+" 8:00000000000000000000000000000000")); status != http.StatusOK {
		t.Fatalf("%d %v", status, answer)
	}
	if got := h.one("SELECT kind FROM filament_tags"); got != "other" {
		t.Errorf("kind = %v", got)
	}
}

// Review of #8161: a page's lookup of a tag it read before (tap false) is no tap.
func TestALookupThatIsNoTapIsNotInTheHistory(t *testing.T) {
	h := newHarness(t)
	tag := h.one("INSERT INTO filament_tags (kind, tag_uid) VALUES ('ntag_openspool', '53F64B94420001') RETURNING id::text")
	h.py.answer = `{"tag":{"id":"` + tag.(string) + `"},"spool":null,"decoded":null,"draft":null,"next":"bind_or_create"}`
	status, answer := h.post(map[string]any{"uid": "53:f6:4b:94:42:00:01", "tag_type": nil, "records": []any{}, "tap": false})
	if status != http.StatusOK || answer["tag_read_id"] != nil {
		t.Fatalf("%d %v", status, answer)
	}
	if got := h.one("SELECT count(*) FROM filament_tag_reads"); got != int64(0) {
		t.Errorf("rows = %v", got)
	}
}

// Review of #8161: tap false on a MIFARE read writes the tag but no history row and no hold.
func TestAMifareLookupThatIsNoTapIsNotInTheHistory(t *testing.T) {
	h := newHarness(t)
	body := mifareRead(uidA, block0A+" "+shared)
	body["tap"] = false
	status, answer := h.post(body)
	if status != http.StatusOK || answer["tag_read_id"] != nil || answer["bambu"] == nil {
		t.Fatalf("%d %v", status, answer)
	}
	if got := h.one("SELECT tray_uuid FROM filament_tags WHERE id = $1::uuid", h.tagOf(uidA+"00000100")); got != tray {
		t.Errorf("tray_uuid = %v", got)
	}
	if got := h.one("SELECT count(*) FROM filament_tag_reads"); got != int64(0) {
		t.Errorf("rows = %v", got)
	}
	if got := h.one("SELECT count(*) FROM blob_refs WHERE holder_kind = 'tag_read'"); got != int64(0) {
		t.Errorf("holds = %v", got)
	}
}

// Review of #8161: the fold keeps the tray tag's note over the folded tag's, and takes the
// folded tag's note only when the tray's tag has none.
func TestTheFoldKeepsTheTrayTagsNote(t *testing.T) {
	for _, c := range []struct {
		name string
		kept any
		want string
	}{{"both", "tray note", "tray note"}, {"folded only", nil, "side note"}} {
		t.Run(c.name, func(t *testing.T) {
			h := newHarness(t)
			h.exec("INSERT INTO filament_tags (kind, tag_uid, tray_uuid, note) VALUES ('bambu_mifare', $1, $2, $3)", uidA+"00000100", tray, c.kept)
			h.exec("INSERT INTO filament_tags (kind, tag_uid, note) VALUES ('other', $1, 'side note')", uidB+"00000100")
			if status, answer := h.post(mifareRead(uidB, block0B+" "+shared)); status != http.StatusOK {
				t.Fatalf("%d %v", status, answer)
			}
			if got := h.one("SELECT count(*) FROM filament_tags"); got != int64(1) {
				t.Fatalf("%v tag rows", got)
			}
			if got := h.one("SELECT note || ' ' || kind FROM filament_tags"); got != c.want+" bambu_mifare" {
				t.Errorf("note and kind = %v", got)
			}
		})
	}
}

// Review of #8161: a body over bodyMax goes to Python whole, which answers its own refusal.
func TestAnOversizeBodyGoesToPythonWhole(t *testing.T) {
	h := newHarness(t)
	h.py.status, h.py.answer = http.StatusRequestEntityTooLarge, `{"code":"too_large"}`
	body := mifareRead(uidA, block0A+" "+shared)
	body["pad"] = strings.Repeat("x", bodyMax)
	status, answer := h.post(body)
	if status != http.StatusRequestEntityTooLarge || answer["code"] != "too_large" {
		t.Fatalf("%d %v", status, answer)
	}
	if len(h.py.bodies) != 1 || h.py.bodies[0]["pad"] != body["pad"] || h.py.bodies[0]["mifare_blocks"] == nil {
		t.Errorf("python did not get the whole body")
	}
	if got := h.one("SELECT count(*) FROM filament_tags"); got != int64(0) {
		t.Errorf("an oversize read wrote %v tags", got)
	}
}

func TestAScanKeepsItsPhoto(t *testing.T) {
	h := newHarness(t)
	photo := "scan_image-" + strings.Repeat("ab", 32)
	h.exec(`INSERT INTO store_blobs (sha256, kind, backend, backend_id, size, meta, key)
 VALUES ($1, 'scan_image', 's3', 't/scan_image/x', 10, '{}'::jsonb, $2)`, strings.Repeat("ab", 32), photo)
	body := mifareRead(uidA, block0A+" "+shared)
	body["image"] = map[string]any{"key": photo, "media_type": "image/jpeg"}
	status, answer := h.post(body)
	if status != http.StatusOK {
		t.Fatalf("%d %v", status, answer)
	}
	if got := h.one("SELECT image_key || ' ' || image_media_type FROM filament_tag_reads"); got != photo+" image/jpeg" {
		t.Errorf("row = %v", got)
	}
	if got := h.one("SELECT count(*) FROM blob_refs WHERE key = $1 AND holder_kind = 'tag_read'", photo); got != int64(1) {
		t.Errorf("%v photo holds", got)
	}
	if _, ok := h.py.bodies[0]["image"]; ok {
		t.Error("Python got the photo")
	}
	for name, img := range map[string]map[string]any{
		"unknown key": {"key": "scan_image-" + strings.Repeat("cd", 32), "media_type": "image/jpeg"},
		"other kind":  {"key": "tag_dump-" + strings.Repeat("ab", 32), "media_type": "image/jpeg"},
		"not image":   {"key": photo, "media_type": "text/html"},
	} {
		body["image"] = img
		if status, answer := h.post(body); status != http.StatusUnprocessableEntity {
			t.Errorf("%s: %d %v", name, status, answer)
		}
	}
}

func TestAScanKeepsWhereItWasMade(t *testing.T) {
	h := newHarness(t)
	now := time.Now().UTC().Truncate(time.Second)
	body := mifareRead(uidA, block0A+" "+shared)
	body["location"] = map[string]any{"lat": 51.5, "lon": -0.12, "accuracy_m": 12.5, "at": now.Format(time.RFC3339)}
	if status, answer := h.post(body); status != http.StatusOK {
		t.Fatalf("%d %v", status, answer)
	}
	var lat, lon, accuracy float64
	var at time.Time
	if err := h.pool.QueryRow(context.Background(),
		"SELECT location_lat, location_lon, location_accuracy_m, location_at FROM filament_tag_reads").
		Scan(&lat, &lon, &accuracy, &at); err != nil {
		t.Fatal(err)
	}
	if lat != 51.5 || lon != -0.12 || accuracy != 12.5 || !at.Equal(now) {
		t.Errorf("row = %v %v %v %v", lat, lon, accuracy, at)
	}
	if _, ok := h.py.bodies[0]["location"]; ok {
		t.Error("Python got the position")
	}
	for name, l := range map[string]map[string]any{
		"lat 91":     {"lat": 91, "lon": 0, "at": now.Format(time.RFC3339)},
		"lon -181":   {"lat": 0, "lon": -181, "at": now.Format(time.RFC3339)},
		"accuracy":   {"lat": 0, "lon": 0, "accuracy_m": 100001, "at": now.Format(time.RFC3339)},
		"two days":   {"lat": 0, "lon": 0, "at": now.Add(-48 * time.Hour).Format(time.RFC3339)},
		"no time":    {"lat": 0, "lon": 0},
		"not a time": {"lat": 0, "lon": 0, "at": "yesterday"},
	} {
		body["location"] = l
		if status, answer := h.post(body); status != http.StatusUnprocessableEntity {
			t.Errorf("%s: %d %v", name, status, answer)
		}
	}
	// A refused position is null: the scan is kept without one.
	body["location"] = nil
	if status, answer := h.post(body); status != http.StatusOK {
		t.Errorf("null: %d %v", status, answer)
	}
}

// Another Go route (#8157, a scan job's result) records a read with no Python part.
func TestRecordMifareIsTheRoutesRecordWithoutPython(t *testing.T) {
	h := newHarness(t)
	body, _ := json.Marshal(mifareRead(uidA, block0A+" "+shared))
	got, err := h.reads.RecordMifare(context.Background(), body, "service:agent", "agent")
	if err != nil {
		t.Fatal(err)
	}
	if got.Bambu.TrayUID == nil || *got.Bambu.TrayUID != tray || got.DumpKey == "" || got.ReadID == "" {
		t.Errorf("recorded %+v", got)
	}
	if v := h.one("SELECT source || ' ' || actor FROM filament_tag_reads WHERE id = $1::uuid", got.ReadID); v != "agent service:agent" {
		t.Errorf("row = %v", v)
	}
	if len(h.py.bodies) != 0 {
		t.Error("Python got a read")
	}
	if _, err := h.reads.RecordMifare(context.Background(), body, "x", "fax"); err == nil {
		t.Error("an unknown source was taken")
	}
}

func TestPythonsRefusalIsTheAnswer(t *testing.T) {
	h := newHarness(t)
	h.py.status, h.py.answer = http.StatusNotFound, `{"detail":"no spool"}`
	status, answer := h.post(mifareRead(uidA, block0A+" "+shared))
	if status != http.StatusNotFound || answer["detail"] != "no spool" {
		t.Errorf("%d %v", status, answer)
	}
	if got := h.one("SELECT count(*) FROM filament_tag_reads"); got != int64(1) {
		t.Errorf("the scan was not kept: %v rows", got)
	}
}

func TestADraftComesFromTheCatalog(t *testing.T) {
	h := newHarness(t)
	h.exec(`INSERT INTO filament_materials (name) SELECT 'PLA'
 WHERE NOT EXISTS (SELECT 1 FROM filament_material_names WHERE key = 'PLA')`)
	status, answer := h.post(mifareRead(uidA, block0A+" "+shared))
	if status != http.StatusOK {
		t.Fatalf("%d %v", status, answer)
	}
	d, _ := answer["draft"].(map[string]any)
	if d == nil || d["material_id"] == nil || d["rgba"] != "#B8ACD680" || d["label_weight_g"] != float64(1000) ||
		d["diameter_mm"] != 1.75 || d["nozzle_temp_min"] != float64(200) || d["nozzle_temp_max"] != float64(240) ||
		d["drying_temp_c"] != float64(55) || d["drying_hours"] != float64(8) {
		t.Errorf("draft = %v", d)
	}
}

func TestCleanUIDIsPythons(t *testing.T) {
	_, file, _, _ := runtime.Caller(0)
	raw, err := os.ReadFile(filepath.Join(filepath.Dir(file), "../../../../../test-fixtures/filament/tag-uid-vectors.json"))
	if err != nil {
		t.Fatal(err)
	}
	var vectors struct {
		Cases []struct {
			In  *string `json:"in"`
			Out *string `json:"out"`
		} `json:"cases"`
	}
	if err := json.Unmarshal(raw, &vectors); err != nil {
		t.Fatal(err)
	}
	for _, c := range vectors.Cases {
		in, want := "", ""
		if c.In != nil {
			in = *c.In
		}
		if c.Out != nil {
			want = *c.Out
		}
		if got := CleanUID(in); got != want {
			t.Errorf("CleanUID(%q) = %q, want %q", in, got, want)
		}
	}
}

// deviceRead is a paired device's tag read for owner, as the devices area's result writes it
// (#8244): the dump stored as a tag_dump blob, and its device and call.
func (h *harness) deviceRead(owner, uid, blocks string) string {
	h.t.Helper()
	ctx := context.Background()
	body, _ := json.Marshal(map[string]any{"uid": uid, "tech": "MifareClassic", "blocks": blocksOf(blocks), "records": []any{}})
	put, err := h.reads.Blobs.Put(ctx, dumpKind, mediaType, body)
	if err != nil {
		h.t.Fatal(err)
	}
	var id string
	if err := h.pool.QueryRow(ctx, `WITH d AS (
		INSERT INTO devices (owner, name, model, app_version, capabilities)
		VALUES ($1, 'Phone', 'Pixel', '1.0', ARRAY['nfc.read']) RETURNING id),
	c AS (INSERT INTO device_calls (device_id, caller, caller_kind, method, timeout_s, deadline, outcome, ended_at)
		SELECT id, $1, 'person', 'nfc.read', 60, now(), 'done', now() FROM d RETURNING id, device_id)
	INSERT INTO device_reads (device_id, call_id, kind, uid, tech, blob_key, media_type, complete)
		SELECT device_id, id, 'nfc', $2, 'MifareClassic', $3, $4, false FROM c RETURNING id::text`,
		owner, uid, put.Key, mediaType).Scan(&id); err != nil {
		h.t.Fatal(err)
	}
	return id
}

var ada = auth.Caller{Kind: auth.KindPerson, Principal: "user:test#ada", SessionKind: "login"}

func TestTagReadFromDeviceRead(t *testing.T) {
	h := newHarness(t)
	h.who = ada
	readA := h.deviceRead(ada.Principal, uidA, block0A+" "+shared)
	readB := h.deviceRead(ada.Principal, uidB, block0B+" "+shared)
	for _, read := range []string{readA, readB} {
		status, answer := h.post(map[string]any{"read_id": read})
		if status != http.StatusOK {
			t.Fatalf("%s: %d %v", read, status, answer)
		}
		if bambu := answer["bambu"].(map[string]any); bambu["tray_uid"] != tray || answer["tag_read_id"] == nil {
			t.Errorf("answer = %v", answer)
		}
	}
	tag := h.tagOf(uidA + "00000100")
	if got := h.tagOf(uidB + "00000100"); got != tag {
		t.Fatalf("the two sides are tags %s and %s", tag, got)
	}
	if got := h.one("SELECT tray_uuid FROM filament_tags WHERE id = $1::uuid", tag); got != tray {
		t.Errorf("tray_uuid = %v", got)
	}
	for _, read := range []string{readA, readB} {
		if got := h.one(`SELECT source || ' ' || tag_type FROM filament_tag_reads
			WHERE device_read_id = $1::uuid AND tag_id = $2::uuid`, read, tag); got != "app MifareClassic" {
			t.Errorf("read %s: %v", read, got)
		}
	}
	// Python got UID-only reads, without the read_id.
	for _, b := range h.py.bodies {
		if _, ok := b["read_id"]; ok || b["mifare_blocks"] != nil {
			t.Errorf("forwarded %v", b)
		}
	}
	// The same read posted again answers its first row and writes no second one.
	first := h.one("SELECT id::text FROM filament_tag_reads WHERE device_read_id = $1::uuid", readA)
	if status, answer := h.post(map[string]any{"read_id": readA}); status != http.StatusOK || answer["tag_read_id"] != first {
		t.Errorf("again: %d %v, want row %v", status, answer, first)
	}
	if got := h.one("SELECT count(*) FROM filament_tag_reads"); got != int64(2) {
		t.Errorf("%v reads, want 2", got)
	}
	// A read_id comes in place of the inline read.
	if status, answer := h.post(map[string]any{"read_id": readA, "uid": uidA}); status != http.StatusUnprocessableEntity {
		t.Errorf("read_id and uid: %d %v", status, answer)
	}
}

func TestTagReadFromAnotherPersonsDeviceReadIsNotFound(t *testing.T) {
	h := newHarness(t)
	h.who = ada
	bobs := h.deviceRead("user:test#bob", uidA, block0A+" "+shared)
	for _, id := range []string{bobs, "not-an-id", "00000000-0000-0000-0000-000000000000"} {
		if status, answer := h.post(map[string]any{"read_id": id}); status != http.StatusNotFound {
			t.Errorf("read %s: %d %v", id, status, answer)
		}
	}
	if got := h.one("SELECT count(*) FROM filament_tags"); got != int64(0) {
		t.Errorf("%v tags from another person's read", got)
	}
	if len(h.py.bodies) != 0 {
		t.Errorf("Python got %v", h.py.bodies)
	}
}
