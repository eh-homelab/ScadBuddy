package scan

import (
	"bytes"
	"context"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"hash/crc32"
	"image"
	"image/color"
	"image/jpeg"
	"image/png"
	"io"
	"math"
	"math/rand/v2"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/makiuchi-d/gozxing"
	"github.com/makiuchi-d/gozxing/datamatrix"
	"github.com/makiuchi-d/gozxing/oned"
	"github.com/makiuchi-d/gozxing/qrcode"
	"golang.org/x/image/draw"
	"golang.org/x/image/math/f64"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/auth"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/blobstore"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/seal"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/testdb"
)

func TestMain(m *testing.M) { testdb.Main(m) }

// The fixture photos, made here from the codes the labels print.

// qr is text's QR code, side pixels square, black on white with its quiet zone.
func qr(t *testing.T, text string, side int) image.Image {
	t.Helper()
	m, err := qrcode.NewQRCodeWriter().Encode(text, gozxing.BarcodeFormat_QR_CODE, side, side, nil)
	if err != nil {
		t.Fatal(err)
	}
	return m
}

func ean13(t *testing.T, digits string) image.Image {
	t.Helper()
	m, err := oned.NewEAN13Writer().Encode(digits, gozxing.BarcodeFormat_EAN_13, 400, 160, nil)
	if err != nil {
		t.Fatal(err)
	}
	return m
}

func dataMatrix(t *testing.T, text string) image.Image {
	t.Helper()
	m, err := datamatrix.NewDataMatrixWriter().Encode(text, gozxing.BarcodeFormat_DATA_MATRIX, 200, 200, nil)
	if err != nil {
		t.Fatal(err)
	}
	// The writer draws no quiet zone; a printed label has one.
	label := image.NewGray(image.Rect(0, 0, 260, 260))
	draw.Draw(label, label.Bounds(), image.White, image.Point{}, draw.Src)
	draw.Draw(label, image.Rect(30, 30, 230, 230), m, image.Point{}, draw.Src)
	return label
}

// snapshot puts code on a w x h canvas, a shelf's grey shading with a camera's grain, turned
// by degrees about its centre, as a phone's photo of a label would hold it. (A grain as
// strong as a bar's contrast reads as a barcode now and then, as it would on any reader.)
func snapshot(code image.Image, w, h int, degrees float64) *image.RGBA {
	canvas := image.NewRGBA(image.Rect(0, 0, w, h))
	rng := rand.New(rand.NewPCG(1, 2))
	for y := range h {
		for x := range w {
			v := uint8(100 + 60*x/w + rng.IntN(8))
			canvas.Set(x, y, color.RGBA{v, v, v + 10, 255})
		}
	}
	b := code.Bounds()
	rad := degrees * math.Pi / 180
	cos, sin := math.Cos(rad), math.Sin(rad)
	cx, cy := float64(w)/2, float64(h)/2
	sx, sy := float64(b.Dx())/2, float64(b.Dy())/2
	// dst = R·(src − s) + c
	m := f64.Aff3{cos, -sin, cx - cos*sx + sin*sy, sin, cos, cy - sin*sx - cos*sy}
	draw.BiLinear.Transform(canvas, m, code, b, draw.Over, nil)
	return canvas
}

func encodeJPEG(t *testing.T, img image.Image) []byte {
	t.Helper()
	var buf bytes.Buffer
	if err := jpeg.Encode(&buf, img, &jpeg.Options{Quality: 85}); err != nil {
		t.Fatal(err)
	}
	return buf.Bytes()
}

func encodePNG(t *testing.T, img image.Image) []byte {
	t.Helper()
	var buf bytes.Buffer
	if err := png.Encode(&buf, img); err != nil {
		t.Fatal(err)
	}
	return buf.Bytes()
}

// bombPNG is a PNG whose header claims w x h pixels and holds no pixels: a decompression
// bomb's shape, refused from its header alone.
func bombPNG(w, h uint32) []byte {
	var buf bytes.Buffer
	buf.WriteString("\x89PNG\r\n\x1a\n")
	chunk := func(kind string, data []byte) {
		_ = binary.Write(&buf, binary.BigEndian, uint32(len(data)))
		buf.WriteString(kind)
		buf.Write(data)
		_ = binary.Write(&buf, binary.BigEndian, crc32.ChecksumIEEE(append([]byte(kind), data...)))
	}
	header := make([]byte, 13)
	binary.BigEndian.PutUint32(header[0:], w)
	binary.BigEndian.PutUint32(header[4:], h)
	header[8], header[9] = 8, 0 // 8-bit greyscale
	chunk("IHDR", header)
	chunk("IEND", nil)
	return buf.Bytes()
}

// heic is the start of a HEIC file: its ftyp box.
var heic = append([]byte{0, 0, 0, 24}, []byte("ftypheic\x00\x00\x00\x00mif1heic")...)

// The decode alone, no database.

func TestTheDecoderReadsEachCodeTheLabelsAndBoxesCarry(t *testing.T) {
	ctx := context.Background()
	spool := "scadbuddy:spool:0b7f6a3e-2c1d-4e5f-8a9b-0c1d2e3f4a5b"
	cases := []struct {
		name string
		body []byte
		want []Code
	}{
		{"a spool label", encodePNG(t, qr(t, spool, 300)), []Code{{"qr_code", spool}}},
		{"a place label", encodePNG(t, qr(t, "https://scadbuddy.example/x/L-12", 300)),
			[]Code{{"qr_code", "https://scadbuddy.example/x/L-12"}}},
		{"a box's EAN-13", encodeJPEG(t, snapshot(ean13(t, "4006381333931"), 900, 600, 0)),
			[]Code{{"ean_13", "4006381333931"}}},
		{"an EAN-13 turned 90°", encodeJPEG(t, snapshot(ean13(t, "4006381333931"), 700, 900, 90)),
			[]Code{{"ean_13", "4006381333931"}}},
		{"a Data Matrix", encodePNG(t, snapshot(dataMatrix(t, "S-142"), 500, 500, 0)),
			[]Code{{"data_matrix", "S-142"}}},
		{"a photo-like label turned 30°", encodeJPEG(t, snapshot(qr(t, spool, 360), 1600, 1200, 30)),
			[]Code{{"qr_code", spool}}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			_, got, failure := decode(ctx, c.body)
			if failure != "" || len(got) != len(c.want) {
				t.Fatalf("got %v (%q), want %v", got, failure, c.want)
			}
			for i := range got {
				if got[i] != c.want[i] {
					t.Errorf("code %d: %v, want %v", i, got[i], c.want[i])
				}
			}
		})
	}
}

func TestTwoQRCodesInOnePhotoAreBothRead(t *testing.T) {
	canvas := snapshot(qr(t, "S-1", 1), 1400, 700, 0)
	for i, text := range []string{"S-7", "L-3"} {
		draw.Copy(canvas, image.Pt(100+i*700, 150), qr(t, text, 400), image.Rect(0, 0, 400, 400), draw.Src, nil)
	}
	_, got, failure := decode(context.Background(), encodePNG(t, canvas))
	if failure != "" || len(got) != 2 {
		t.Fatalf("got %v (%q)", got, failure)
	}
	texts := map[string]bool{got[0].Text: true, got[1].Text: true}
	if !texts["S-7"] || !texts["L-3"] {
		t.Errorf("got %v", got)
	}
}

// A strong grain passes an EAN's or a UPC's check digit on some row; a 1D result counts
// only when a row beside it reads the same.
func TestAPhotosGrainReadsAsNoBarcode(t *testing.T) {
	canvas := image.NewRGBA(image.Rect(0, 0, 900, 700))
	rng := rand.New(rand.NewPCG(1, 2))
	for y := range 700 {
		for x := range 900 {
			v := uint8(110 + rng.IntN(40))
			canvas.Set(x, y, color.RGBA{v, v, v + 10, 255})
		}
	}
	draw.Copy(canvas, image.Pt(100, 150), qr(t, "S-7", 400), image.Rect(0, 0, 400, 400), draw.Src, nil)
	_, got, _ := decode(context.Background(), encodePNG(t, canvas))
	if len(got) != 1 || got[0].Text != "S-7" {
		t.Errorf("got %v", got)
	}
}

func TestAPhotoWithNoCodeReadsNone(t *testing.T) {
	p, got, failure := decode(context.Background(), encodeJPEG(t, snapshot(image.NewGray(image.Rect(0, 0, 1, 1)), 800, 600, 0)))
	if failure != "" || len(got) != 0 || p.MediaType != "image/jpeg" || p.Width != 800 {
		t.Errorf("got %v %q %+v", got, failure, p)
	}
}

func TestAnOversizedPhotoIsRefusedFromItsHeader(t *testing.T) {
	for _, size := range [][2]uint32{{6000, 4001}, {12_001, 10}, {50_000, 50_000}} {
		p, got, failure := decode(context.Background(), bombPNG(size[0], size[1]))
		if failure != errTooLarge || got != nil || p.MediaType != "image/png" {
			t.Errorf("%v: %v %q %+v", size, got, failure, p)
		}
	}
}

func TestHEICAndOtherBytesAreNotReadAsPhotos(t *testing.T) {
	if _, err := sniff(heic); err != errHEIC {
		t.Errorf("HEIC: %v", err)
	}
	if _, err := sniff([]byte("GIF89a not a photo")); err != errNotImage {
		t.Errorf("other: %v", err)
	}
	if _, _, failure := decode(context.Background(), heic); failure != errUnreadable {
		t.Errorf("a stored HEIC decodes to %q", failure)
	}
}

func TestATextIsKeptBoundedAndValid(t *testing.T) {
	if got := clean("a\x00b\xffc"); got != "ab�c" {
		t.Errorf("%q", got)
	}
	long := strings.Repeat("é", maxText) // 2 bytes each
	if got := clean(long); len(got) > maxText || !strings.HasPrefix(long, got) || len(got) != maxText {
		t.Errorf("%d bytes", len(got))
	}
}

func TestATypedCodeParsesAsPythonsDoes(t *testing.T) {
	for text, want := range map[string]string{
		"L-12": "L12", " s142 ": "S142", "l-0": "", "L-2147483648": "", "L-12345678901": "", "LL-1": "", "": "",
	} {
		prefix, number, ok := parseCode(text)
		got := ""
		if ok {
			got = prefix + jsonInt(number)
		}
		if got != want {
			t.Errorf("%q: %q, want %q", text, got, want)
		}
	}
}

func jsonInt(n int) string { return strconv.Itoa(n) }

// The routes, on a real database and a fake store.

// store is an S3 that keeps what is PUT and answers it to a GET; every request must be
// presigned.
type store struct {
	mu      sync.Mutex
	objects map[string][]byte
}

func (s *store) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if r.URL.Query().Get("X-Amz-Signature") == "" {
		w.WriteHeader(http.StatusForbidden)
		return
	}
	switch r.Method {
	case http.MethodPut:
		body, _ := io.ReadAll(r.Body)
		s.objects[r.URL.Path] = body
	case http.MethodGet:
		body, ok := s.objects[r.URL.Path]
		if !ok {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		_, _ = w.Write(body)
	}
}

type harness struct {
	who   auth.Caller
	t     *testing.T
	pool  *pgxpool.Pool
	scans *Scans
	api   string
	store *store
}

func newHarness(t *testing.T) *harness {
	t.Helper()
	pool := testdb.New(t)
	s3 := &store{objects: map[string][]byte{}}
	server := httptest.NewServer(s3)
	t.Cleanup(server.Close)
	env := map[string]string{
		"SCADBUDDY_STORE_BACKEND":              "s3",
		"SCADBUDDY_STORE_S3_ENDPOINT":          server.URL,
		"SCADBUDDY_STORE_S3_BUCKET":            "sb-test",
		"SCADBUDDY_STORE_S3_PREFIX":            "t",
		"SCADBUDDY_STORE_S3_ACCESS_KEY_ID":     "testing",
		"SCADBUDDY_STORE_S3_SECRET_ACCESS_KEY": "testing",
	}
	key := make([]byte, 32)
	blobs := &blobstore.Blobs{Pool: pool, Keys: &seal.Keys{Current: seal.NewKEK(key)},
		LookupEnv: func(name string) (string, bool) { v, ok := env[name]; return v, ok },
		HTTP:      server.Client(), Stream: server.Client(), Now: time.Now}
	scans := New(pool, blobs)
	mux := http.NewServeMux()
	h := &harness{t: t, pool: pool, scans: scans, store: s3,
		who: auth.Caller{Kind: auth.KindPerson, Principal: "user:test#ada", SessionKind: "login"}}
	for _, p := range scans.Public() {
		handler := p.Handler
		mux.Handle(p.Pattern, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			handler.ServeHTTP(w, r.WithContext(auth.WithCaller(r.Context(), h.who)))
		}))
	}
	api := httptest.NewServer(mux)
	t.Cleanup(api.Close)
	h.api = api.URL
	return h
}

func (h *harness) send(method, path, mediaType string, body []byte) (int, []byte, http.Header) {
	h.t.Helper()
	req, _ := http.NewRequest(method, h.api+path, bytes.NewReader(body))
	req.Header.Set("Content-Type", mediaType)
	res, err := http.DefaultClient.Do(req)
	if err != nil {
		h.t.Fatal(err)
	}
	defer res.Body.Close()
	out, _ := io.ReadAll(res.Body)
	return res.StatusCode, out, res.Header
}

func (h *harness) json(method, path string, body any) (int, map[string]any) {
	h.t.Helper()
	raw, _ := json.Marshal(body)
	if body == nil {
		raw = nil
	}
	status, out, _ := h.send(method, path, "application/json", raw)
	var v map[string]any
	_ = json.Unmarshal(out, &v)
	return status, v
}

func (h *harness) upload(body []byte) map[string]any {
	h.t.Helper()
	status, out, _ := h.send("POST", "/api/v1/scan/images", "image/jpeg", body)
	var v map[string]any
	_ = json.Unmarshal(out, &v)
	if status != 200 {
		h.t.Fatalf("upload: %d %s", status, out)
	}
	return v
}

func (h *harness) spool() (id string, number int) {
	h.t.Helper()
	err := h.pool.QueryRow(context.Background(), `INSERT INTO filament_spools (material_id)
		SELECT id FROM filament_materials WHERE name = 'PLA' RETURNING id::text, number`).Scan(&id, &number)
	if err != nil {
		h.t.Fatal(err)
	}
	return id, number
}

func (h *harness) place(name string) (id string, number int) {
	h.t.Helper()
	err := h.pool.QueryRow(context.Background(), `INSERT INTO filament_locations (name, kind_id)
		SELECT $1, id FROM filament_location_kinds ORDER BY name LIMIT 1 RETURNING id::text, number`, name).
		Scan(&id, &number)
	if err != nil {
		h.t.Fatal(err)
	}
	return id, number
}

func TestAScanStoresThePhotoDecodesItAndResolvesEachCode(t *testing.T) {
	h := newHarness(t)
	spoolID, spoolNumber := h.spool()
	placeID, placeNumber := h.place("Dry box")
	canvas := snapshot(qr(t, "x", 1), 1500, 600, 0)
	texts := []string{"scadbuddy:spool:" + spoolID, "https://sb.example/x/L-" + jsonInt(placeNumber), "hello"}
	for i, text := range texts {
		draw.Copy(canvas, image.Pt(40+i*490, 100), qr(t, text, 420), image.Rect(0, 0, 420, 420), draw.Src, nil)
	}
	body := encodePNG(t, canvas)
	stored := h.upload(body)
	key := stored["key"].(string)
	if !strings.HasPrefix(key, "scan_image-") || stored["media_type"] != "image/png" || stored["size"] != float64(len(body)) {
		t.Fatalf("stored %v", stored)
	}
	status, sc := h.json("POST", "/api/v1/scan/codes", map[string]any{
		"key": key, "source": "shutter",
		"location": map[string]any{"lat": 43.65, "lon": -79.38, "accuracy_m": 12.5, "at": "2026-11-27T04:00:00Z"},
	})
	if status != 200 {
		t.Fatalf("%d %v", status, sc)
	}
	if sc["actor"] != "user:test#ada" || sc["source"] != "shutter" || sc["error"] != nil {
		t.Errorf("scan %v", sc)
	}
	if img := sc["image"].(map[string]any); img["key"] != key || img["media_type"] != "image/png" {
		t.Errorf("image %v", img)
	}
	if loc := sc["location"].(map[string]any); loc["lat"] != 43.65 || loc["accuracy_m"] != 12.5 || loc["at"] == nil {
		t.Errorf("location %v", loc)
	}
	byText := map[string]map[string]any{}
	for _, c := range sc["codes"].([]any) {
		code := c.(map[string]any)
		byText[code["text"].(string)] = code
	}
	if len(byText) != 3 {
		t.Fatalf("codes %v", sc["codes"])
	}
	if r := byText[texts[0]]["resolved"].(map[string]any); r["kind"] != "spool" || r["id"] != spoolID ||
		r["code"] != "S-"+jsonInt(spoolNumber) || r["archived"] != false {
		t.Errorf("spool %v", r)
	}
	if r := byText[texts[1]]["resolved"].(map[string]any); r["kind"] != "location" || r["id"] != placeID || r["name"] != "Dry box" {
		t.Errorf("place %v", r)
	}
	if byText["hello"]["resolved"] != nil || byText["hello"]["format"] != "qr_code" {
		t.Errorf("text %v", byText["hello"])
	}
	// The row holds its photo until it is deleted.
	id := sc["id"].(string)
	var held int
	_ = h.pool.QueryRow(context.Background(), "SELECT count(*) FROM blob_refs WHERE key = $1 AND holder_kind = 'code_scan' AND holder_id = $2", key, id).Scan(&held)
	if held != 1 {
		t.Errorf("held %d", held)
	}
	// The photo comes back with its type.
	status, image, headers := h.send("GET", "/api/v1/scan/scans/"+id+"/image", "", nil)
	if status != 200 || !bytes.Equal(image, body) || headers.Get("Content-Type") != "image/png" || headers.Get("X-Content-Type-Options") != "nosniff" {
		t.Errorf("image %d %s", status, headers)
	}
	// It lists, newest first, and reads alone.
	status, list := h.json("GET", "/api/v1/scan/scans?limit=10", nil)
	if status != 200 || len(list["scans"].([]any)) != 1 {
		t.Errorf("list %d %v", status, list)
	}
	if status, one := h.json("GET", "/api/v1/scan/scans/"+id, nil); status != 200 || one["id"] != id {
		t.Errorf("get %d %v", status, one)
	}
	// A delete takes the row and its hold; the photo goes with the sweep.
	if status, _, _ := h.send("DELETE", "/api/v1/scan/scans/"+id, "", nil); status != 204 {
		t.Errorf("delete %d", status)
	}
	_ = h.pool.QueryRow(context.Background(), "SELECT count(*) FROM blob_refs WHERE holder_kind = 'code_scan' AND holder_id = $1", id).Scan(&held)
	if held != 0 {
		t.Errorf("held after delete %d", held)
	}
	if status, _ := h.json("GET", "/api/v1/scan/scans/"+id, nil); status != 404 {
		t.Errorf("get after delete %d", status)
	}
}

func TestAScanThatReadsNothingIsKeptWithItsPhoto(t *testing.T) {
	h := newHarness(t)
	for _, c := range []struct {
		body    []byte
		failure any
	}{
		{encodeJPEG(t, snapshot(image.NewGray(image.Rect(0, 0, 1, 1)), 640, 480, 0)), nil},
		{bombPNG(20_000, 20_000), errTooLarge},
	} {
		key := h.upload(c.body)["key"].(string)
		status, sc := h.json("POST", "/api/v1/scan/codes", map[string]any{"key": key, "source": "nfc"})
		if status != 200 || len(sc["codes"].([]any)) != 0 || sc["error"] != c.failure || sc["location"] != nil {
			t.Errorf("%d %v", status, sc)
		}
	}
	var rows int
	_ = h.pool.QueryRow(context.Background(), "SELECT count(*) FROM code_scans").Scan(&rows)
	if rows != 2 {
		t.Errorf("%d rows", rows)
	}
}

func TestAScanIsDecodedAgain(t *testing.T) {
	h := newHarness(t)
	key := h.upload(encodePNG(t, qr(t, "S-9", 300)))["key"].(string)
	_, sc := h.json("POST", "/api/v1/scan/codes", map[string]any{"key": key})
	id := sc["id"].(string)
	// As if an older decoder had found nothing.
	if _, err := h.pool.Exec(context.Background(), "UPDATE code_scans SET codes = '[]' WHERE id = $1", id); err != nil {
		t.Fatal(err)
	}
	status, again := h.json("POST", "/api/v1/scan/scans/"+id+"/decode", nil)
	if status != 200 || len(again["codes"].([]any)) != 1 || again["source"] != "shutter" {
		t.Errorf("%d %v", status, again)
	}
}

func TestRecordIsAScanFromBytesForAGoCaller(t *testing.T) {
	h := newHarness(t)
	actor := "service:app"
	sc, err := h.scans.Record(context.Background(), encodePNG(t, qr(t, "L-404", 300)),
		Input{Source: "app", Actor: &actor})
	if err != nil {
		t.Fatal(err)
	}
	if sc.Source != "app" || len(sc.Codes) != 1 || sc.Codes[0].Text != "L-404" || sc.Codes[0].Resolved != nil ||
		*sc.Actor != actor || sc.Image.MediaType != "image/png" {
		t.Errorf("%+v", sc)
	}
}

func TestRefusedUploadsAndScans(t *testing.T) {
	h := newHarness(t)
	for name, body := range map[string][]byte{"HEIC": heic, "text": []byte("not a photo"), "empty": nil} {
		status, out, _ := h.send("POST", "/api/v1/scan/images", "image/heic", body)
		if status == 200 || !bytes.Contains(out, []byte("image_not_supported")) {
			t.Errorf("%s: %d %s", name, status, out)
		}
	}
	big := make([]byte, blobstore.Kinds[Kind]+1)
	if status, _, _ := h.send("POST", "/api/v1/scan/images", "image/jpeg", big); status != http.StatusRequestEntityTooLarge {
		t.Errorf("too large: %d", status)
	}
	// A key that is no scan photo, or none at all.
	if _, err := h.pool.Exec(context.Background(), `INSERT INTO store_blobs (key, sha256, kind, backend, backend_id, size)
		VALUES ('tag_dump-x', 'x', 'tag_dump', 's3', 't/tag_dump/x', 1)`); err != nil {
		t.Fatal(err)
	}
	for _, key := range []string{"tag_dump-x", "scan_image-" + strings.Repeat("0", 64)} {
		if status, out := h.json("POST", "/api/v1/scan/codes", map[string]any{"key": key}); status != 404 || out["reason"] != "image_not_found" {
			t.Errorf("%s: %d %v", key, status, out)
		}
	}
	// A location out of range, and a source no one sends.
	key := h.upload(encodePNG(t, qr(t, "S-1", 200)))["key"].(string)
	for _, body := range []map[string]any{
		{"key": key, "location": map[string]any{"lat": 91, "lon": 0}},
		{"key": key, "location": map[string]any{"lon": 0}},
		{"key": key, "source": "camera"},
	} {
		if status, out := h.json("POST", "/api/v1/scan/codes", body); status != 422 {
			t.Errorf("%v: %d %v", body, status, out)
		}
	}
	if status, _ := h.json("GET", "/api/v1/scan/scans/not-an-id", nil); status != 404 {
		t.Errorf("bad id: %d", status)
	}
	// A missing lon is named as lon.
	if _, out := h.json("POST", "/api/v1/scan/codes", map[string]any{"key": key, "location": map[string]any{"lat": 1}}); !strings.Contains(fmt.Sprint(out), "lon") {
		t.Errorf("missing lon: %v", out)
	}
}

func TestAPersonSeesOnlyTheirOwnScans(t *testing.T) {
	h := newHarness(t)
	key := h.upload(encodePNG(t, qr(t, "S-1", 200)))["key"].(string)
	_, made := h.json("POST", "/api/v1/scan/codes", map[string]any{"key": key, "location": map[string]any{"lat": 1, "lon": 2}})
	id := made["id"].(string)
	h.who = auth.Caller{Kind: auth.KindPerson, Principal: "user:test#bob", SessionKind: "login"}
	if _, out := h.json("GET", "/api/v1/scan/scans", nil); len(out["scans"].([]any)) != 0 {
		t.Errorf("bob lists ada's scan: %v", out)
	}
	for _, path := range []string{"/api/v1/scan/scans/" + id, "/api/v1/scan/scans/" + id + "/image"} {
		if status, _ := h.json("GET", path, nil); status != 404 {
			t.Errorf("bob reads %s: %d", path, status)
		}
	}
	for _, method := range []string{"POST", "DELETE"} {
		path := "/api/v1/scan/scans/" + id
		if method == "POST" {
			path += "/decode"
		}
		if status, _ := h.json(method, path, nil); status != 404 {
			t.Errorf("bob %s %s: %d", method, path, status)
		}
	}
	var holds int
	if err := h.pool.QueryRow(context.Background(), "SELECT count(*) FROM blob_refs WHERE holder_kind = $1 AND holder_id = $2", Holder, id).Scan(&holds); err != nil || holds != 1 {
		t.Errorf("bob's delete dropped ada's photo hold: %d %v", holds, err)
	}
	h.who = auth.Caller{Kind: auth.KindService, Service: &auth.Service{Caller: "agent"}}
	if _, out := h.json("GET", "/api/v1/scan/scans", nil); len(out["scans"].([]any)) != 1 {
		t.Errorf("a service sees every scan: %v", out)
	}
}
