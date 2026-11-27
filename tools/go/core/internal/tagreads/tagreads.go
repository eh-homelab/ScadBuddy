// Package tagreads is area filament-tag-reads (#8148, #7905): a phone's read of a Bambu
// MIFARE Classic tag, decoded on the server.
//
// It answers POST /api/v1/filament/tags/reads, the one input path of every tag read (spec
// 2026-11-26-bambu-tag-scan-design.md G2), only for a body that carries mifare_blocks. Any
// other body goes to Python unchanged. For a MIFARE read it:
//
//   - checks the read and decodes it with internal/bambutag, every string and number bounded;
//   - stores the raw dump (the UID and the data blocks, never a key trailer) in the blob store
//     as kind tag_dump, complete or partial;
//   - in one transaction, finds the tag by the tray UUID of block 9, else by the UID through
//     filament_tag_uids, makes it when there is none (kind bambu_mifare), adds the UID to it,
//     folds a UID-only tag of that UID into it ("Same spool as…"), writes raw_blob from a
//     complete read only, appends a filament_tag_reads row and holds the dump in blob_refs;
//   - hands the read to Python as a UID-only read (no blocks, no records), which marks the tag
//     seen, binds it when bind_spool_id is set, and answers TagReadResult;
//   - adds bambu (the decoder's JSON), tag_read_id, dump_key and, when no spool holds the tag,
//     a draft from the decoded fields to Python's answer. The answer is a superset of
//     TagReadResult.
//
// It writes no spool: Python writes the binding, the ledger and the last-seen mark. A Bambu tag
// is never written.
package tagreads

import (
	"bytes"
	"context"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/auth"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/bambutag"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
)

// Area is the area's name in SCADBUDDY_CORE_AREAS.
const Area = "filament-tag-reads"

// Path is the route the area answers.
const Path = "/api/v1/filament/tags/reads"

const (
	// bodyMax bounds a request this route reads: 64 blocks as JSON are about 4 KiB, and an
	// NDEF body (NTAG216's 888 bytes as hex) far less. A larger body goes to Python whole.
	bodyMax = 256 << 10
	// tagTypeMax is TagReadIn.tag_type's bound.
	tagTypeMax = 32
	// mediaType is a dump's media type.
	mediaType = "application/json"
	// dumpKind is a dump's blob kind (blobstore.Kinds).
	dumpKind = "tag_dump"
	// holderKind is the blob_refs holder of a dump: one row per read.
	holderKind = "tag_read"
)

// Putter stores a blob and answers its content index key (blobstore.Blobs.Put).
type Putter interface {
	Put(ctx context.Context, kind, mediaType string, body []byte) (string, error)
}

// Reads serves the area.
type Reads struct {
	Pool *pgxpool.Pool
	// Blobs stores the dumps.
	Blobs Putter
	// Upstream is the Python API: every body without mifare_blocks, and each MIFARE read's
	// UID-only read, go there.
	Upstream http.Handler
}

// Public is the area's public route.
func (s *Reads) Public() map[string][]auth.Public {
	return map[string][]auth.Public{Area: {{
		Pattern: "POST " + Path,
		Route:   auth.Route{Class: "api", Template: Path},
		Handler: http.HandlerFunc(s.serve),
	}}}
}

// wireBlock is one block as the body carries it.
type wireBlock struct {
	Index   *int    `json:"index"`
	DataHex *string `json:"data_hex"`
}

// problem is a refusal with the code the tag routes give (api/filament.py `code=`).
func problem(status int, code, detail string) *httpx.Problem {
	return &httpx.Problem{Status: status, Detail: detail, Extensions: []httpx.Field{{Key: "code", Value: code}}}
}

func (s *Reads) serve(w http.ResponseWriter, r *http.Request) {
	raw, err := io.ReadAll(io.LimitReader(r.Body, bodyMax+1))
	if err != nil {
		httpx.WriteProblem(w, r, problem(http.StatusBadRequest, "body_unread", "the body could not be read"))
		return
	}
	var fields map[string]json.RawMessage
	if len(raw) > bodyMax || json.Unmarshal(raw, &fields) != nil || fields == nil {
		// Not a read this route can take: Python answers its own refusal.
		s.forward(w, r, io.MultiReader(bytes.NewReader(raw), r.Body), r.ContentLength)
		return
	}
	img, perr := s.imageOf(r.Context(), fields)
	if perr != nil {
		httpx.WriteProblem(w, r, perr)
		return
	}
	at, perr := locationOf(fields, time.Now())
	if perr != nil {
		httpx.WriteProblem(w, r, perr)
		return
	}
	scan := scanFacts{image: img, location: at}
	if !isNull(fields["mifare_blocks"]) {
		read, perr := parse(fields)
		if perr != nil {
			httpx.WriteProblem(w, r, perr)
			return
		}
		s.mifare(w, r, fields, read, scan)
		return
	}
	s.ndef(w, r, fields, raw, scan)
}

// scanFacts are what core records of a scan and Python does not: its photo and its position.
type scanFacts struct {
	image    *image
	location *location
}

// stripScan takes the scan's photo and position out of a body for Python, and says whether
// it held either.
func stripScan(fields map[string]json.RawMessage) bool {
	_, img := fields["image"]
	_, at := fields["location"]
	delete(fields, "image")
	delete(fields, "location")
	return img || at
}

// forward hands r to Python with body as its body.
func (s *Reads) forward(w http.ResponseWriter, r *http.Request, body io.Reader, length int64) {
	out := r.Clone(r.Context())
	out.Body = io.NopCloser(body)
	out.ContentLength = length
	if length >= 0 {
		out.Header.Set("Content-Length", strconv.FormatInt(length, 10))
	}
	s.Upstream.ServeHTTP(w, out)
}

// python is Python's answer to r with body.
func (s *Reads) python(r *http.Request, body []byte) *recorder {
	rec := &recorder{header: http.Header{}}
	s.forward(rec, r, bytes.NewReader(body), int64(len(body)))
	return rec
}

// answered is Python's 200 as an object, or nil after writing any other answer as it is.
func answered(w http.ResponseWriter, rec *recorder) map[string]json.RawMessage {
	var answer map[string]json.RawMessage
	if rec.status() == http.StatusOK && json.Unmarshal(rec.body.Bytes(), &answer) == nil && answer != nil {
		return answer
	}
	for k, v := range rec.header {
		w.Header()[k] = v
	}
	w.WriteHeader(rec.status())
	_, _ = w.Write(rec.body.Bytes())
	return nil
}

// ndef records a read with no MIFARE blocks after Python's own read of it: the history row
// of the tag Python found or made, with the read's records and its photo.
func (s *Reads) ndef(w http.ResponseWriter, r *http.Request, fields map[string]json.RawMessage, raw []byte, scan scanFacts) {
	ctx := r.Context()
	if stripScan(fields) {
		raw, _ = json.Marshal(fields)
	}
	answer := answered(w, s.python(r, raw))
	if answer == nil {
		return
	}
	var tag struct {
		ID *string `json:"id"`
	}
	// A bind_spool_id is the page binding a tag it read a moment ago (the New tag card): no
	// second tap, so no second history row.
	if err := json.Unmarshal(answer["tag"], &tag); err == nil && tag.ID != nil && isNull(fields["bind_spool_id"]) {
		var uid, tagType *string
		_ = json.Unmarshal(fields["uid"], &uid)
		_ = json.Unmarshal(fields["tag_type"], &tagType)
		caller := auth.CallerOf(ctx)
		row := historyRow{tagID: *tag.ID, source: sourceOf(caller, "phone"), actor: actorOf(caller),
			tagType: tagType, records: fields["records"], scan: scan}
		if uid != nil {
			row.uid = CleanUID(*uid)
		}
		if id, err := appendRead(ctx, s.Pool, row); err != nil {
			// Python's read is committed; the answer stands without its history row.
			slog.ErrorContext(ctx, "a tag read's history row could not be written", "error", err)
		} else {
			answer["tag_read_id"], _ = json.Marshal(id)
		}
	}
	_ = httpx.WriteJSON(w, answer)
}

// read is a MIFARE read, checked.
type read struct {
	uid     []byte
	tagType *string
	blocks  []bambutag.Block
}

// parse checks a MIFARE read's body. Key trailers are dropped, never stored.
func parse(fields map[string]json.RawMessage) (read, *httpx.Problem) {
	var out read
	invalid := func(loc, msg string) (read, *httpx.Problem) {
		return read{}, httpx.Invalid(httpx.Issue{Loc: []string{"body", loc}, Msg: msg})
	}
	var uid *string
	if err := json.Unmarshal(fields["uid"], &uid); err != nil || uid == nil {
		return invalid("uid", "a MIFARE read needs the tag's 4-byte UID")
	}
	digits := hexDigits(*uid)
	if len(digits) == 16 && strings.HasSuffix(digits, mifareSuffix) {
		digits = digits[:8]
	}
	b, err := hex.DecodeString(digits)
	if err != nil || len(b) != 4 || len(*uid) > 64 {
		return invalid("uid", "a MIFARE Classic UID is 4 bytes")
	}
	out.uid = b
	if t, ok := fields["tag_type"]; ok {
		if err := json.Unmarshal(t, &out.tagType); err != nil || (out.tagType != nil && len(*out.tagType) > tagTypeMax) {
			return invalid("tag_type", "tag_type is text of at most 32 characters")
		}
	}
	if rec, ok := fields["records"]; ok {
		var records []json.RawMessage
		if err := json.Unmarshal(rec, &records); err != nil || len(records) > 0 {
			return invalid("records", "a MIFARE read carries no NDEF records")
		}
	}
	var blocks []wireBlock
	if err := json.Unmarshal(fields["mifare_blocks"], &blocks); err != nil || blocks == nil {
		return invalid("mifare_blocks", "mifare_blocks is a list of {index, data_hex}")
	}
	if len(blocks) > bambutag.BlockCount {
		return invalid("mifare_blocks", "a MIFARE Classic 1K tag has 64 blocks")
	}
	for _, w := range blocks {
		if w.Index == nil || w.DataHex == nil || *w.Index < 0 || *w.Index >= bambutag.BlockCount {
			return invalid("mifare_blocks", "each block has an index from 0 to 63 and its data_hex")
		}
		data, err := hex.DecodeString(*w.DataHex)
		if err != nil || len(data) != bambutag.BlockSize {
			return invalid("mifare_blocks", "each block's data_hex is 16 bytes as hex")
		}
		if bambutag.IsTrailer(*w.Index) {
			continue
		}
		block := bambutag.Block{Index: *w.Index}
		copy(block.Data[:], data)
		out.blocks = append(out.blocks, block)
	}
	return out, nil
}

// dump is a read's raw dump, the tag_dump blob: the UID and the data blocks read, and the
// data blocks not read. A later decoder version decodes it again.
type dump struct {
	Format        string      `json:"format"`
	UID           string      `json:"uid"`
	TagType       *string     `json:"tag_type"`
	Blocks        []dumpBlock `json:"blocks"`
	MissingBlocks []int       `json:"missing_blocks"`
}

type dumpBlock struct {
	Index   int    `json:"index"`
	DataHex string `json:"data_hex"`
}

// dumpFormat names the dump's shape.
const dumpFormat = "scadbuddy.mifare_classic_1k.v1"

func (s *Reads) mifare(w http.ResponseWriter, r *http.Request, fields map[string]json.RawMessage, rd read, scan scanFacts) {
	ctx := r.Context()
	caller := auth.CallerOf(ctx)
	saved, err := s.recordMifare(ctx, rd, scan, actorOf(caller), sourceOf(caller, "app"))
	if err != nil {
		writeError(w, r, err)
		return
	}
	tag, key := saved.Bambu, saved.DumpKey

	// Python's UID-only read: the last-seen mark, the bind and the answer. A refusal (an
	// unknown bind_spool_id, say) is the answer as it is; the scan stays recorded, as the
	// tap happened.
	delete(fields, "mifare_blocks")
	stripScan(fields)
	fields["uid"], _ = json.Marshal(tag.UID)
	fields["records"] = json.RawMessage("[]")
	forwarded, _ := json.Marshal(fields)
	answer := answered(w, s.python(r, forwarded))
	if answer == nil {
		return
	}
	answer["bambu"], _ = json.Marshal(tag)
	answer["tag_read_id"], _ = json.Marshal(saved.ReadID)
	answer["dump_key"], _ = json.Marshal(key)
	if isNull(answer["spool"]) && isNull(answer["draft"]) {
		if draft, err := draftOf(ctx, s.Pool, tag); err != nil {
			slog.WarnContext(ctx, "a Bambu tag's draft could not be read; the answer has none", "error", err)
		} else if draft != nil {
			answer["draft"], _ = json.Marshal(draft)
		}
	}
	_ = httpx.WriteJSON(w, answer)
}

func isNull(v json.RawMessage) bool { return len(v) == 0 || string(v) == "null" }

// writeError answers a problem as it is, and any other error as a 500.
func writeError(w http.ResponseWriter, r *http.Request, err error) {
	var p *httpx.Problem
	if errors.As(err, &p) {
		httpx.WriteProblem(w, r, p)
		return
	}
	if r.Context().Err() == nil {
		slog.ErrorContext(r.Context(), "a MIFARE tag read failed", "error", err)
	}
	httpx.WriteProblem(w, r, &httpx.Problem{Status: http.StatusInternalServerError, Detail: "the tag read failed"})
}

// actorOf is api/filament.py `actor`: a person by their principal, a service by its name.
func actorOf(c auth.Caller) string {
	switch c.Kind {
	case auth.KindPerson, auth.KindToken:
		return c.Principal
	case auth.KindService:
		return "service:" + c.Service.Caller
	}
	return "anonymous"
}

// sourceOf is a read's source in filament_tag_reads: a person's page is the phone (Web NFC)
// or, for a read that carries MIFARE blocks, the ScadBuddy app's native bridge (page); an
// access token or a service credential is an agent's or a script's read.
func sourceOf(c auth.Caller, page string) string {
	if c.Kind == auth.KindToken || c.Kind == auth.KindService {
		return "agent"
	}
	return page
}

// mifareSuffix is spools/models.py MIFARE_UID_SUFFIX: the AMS's form of a 4-byte UID.
const mifareSuffix = "00000100"

func hexDigits(s string) string {
	var b strings.Builder
	for _, c := range s {
		if strings.ContainsRune("0123456789abcdefABCDEF", c) {
			b.WriteRune(c)
		}
	}
	return strings.ToUpper(b.String())
}

// key is spools/models.py `_key`: "" for none or all zeros.
func key(s string) string {
	if strings.Trim(s, "0") == "" {
		return ""
	}
	return s
}

// CleanUID is spools/models.py clean_tag_uid: the last 16 hex digits, upper case, a 4-byte
// UID in the AMS's form, "" for none or zeros (test-fixtures/filament/tag-uid-vectors.json).
func CleanUID(s string) string {
	digits := hexDigits(s)
	if len(digits) > 16 {
		digits = digits[len(digits)-16:]
	}
	if key(digits) == "" {
		return ""
	}
	if len(digits) == 8 {
		return digits + mifareSuffix
	}
	return digits
}

// cleanTray is spools/models.py clean_tray_uuid: the first 32 hex digits, "" for zeros.
func cleanTray(s string) string {
	digits := hexDigits(s)
	if len(digits) > 32 {
		digits = digits[:32]
	}
	return key(digits)
}

// recorder keeps Python's answer to a MIFARE read's UID-only read.
type recorder struct {
	header http.Header
	code   int
	body   bytes.Buffer
}

func (r *recorder) Header() http.Header { return r.header }

func (r *recorder) WriteHeader(code int) {
	if r.code == 0 {
		r.code = code
	}
}

func (r *recorder) Write(b []byte) (int, error) {
	r.WriteHeader(http.StatusOK)
	return r.body.Write(b)
}

func (r *recorder) status() int {
	if r.code == 0 {
		return http.StatusOK
	}
	return r.code
}
