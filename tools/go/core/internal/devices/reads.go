package devices

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/bus"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
)

const (
	// Holder is an upload's holder kind in blob_refs, with its call's id: the blob is kept
	// while the call's row lives, and the hold says which call's device uploaded it.
	Holder = "device_call"
	// The blob kinds a device uploads (blobstore.Kinds has their limits).
	KindDump  = "tag_dump"
	KindPhoto = "scan_image"
	dumpType  = "application/json"
	// blocksMax is a MIFARE Classic 4K's blocks; a 1K has 64.
	blocksMax = 256
)

var (
	uidPattern   = regexp.MustCompile(`^[0-9A-Fa-f]{8,20}$`)
	blockPattern = regexp.MustCompile(`^[0-9A-Fa-f]{32}$`)
)

// kindOf is the blob kind a method's read is stored as.
func kindOf(method string) string {
	if strings.HasPrefix(method, "nfc.") {
		return KindDump
	}
	return KindPhoto
}

// trailer says whether block i of a MIFARE Classic tag is a sector trailer, which holds the
// sector's keys: every 4th block of the first 32 sectors, then every 16th (a 4K's last 8).
func trailer(i int) bool {
	if i < 128 {
		return i%4 == 3
	}
	return i%16 == 15
}

// dump is a tag_dump blob: the tag's UID, its tech, every data block read, and any NDEF records.
type dump struct {
	UID     string            `json:"uid"`
	Tech    string            `json:"tech"`
	Blocks  []dumpBlock       `json:"blocks"`
	Records []json.RawMessage `json:"records"`
}

type dumpBlock struct {
	Index   int    `json:"index"`
	DataHex string `json:"data_hex"`
}

func invalid(reason, detail string) error {
	return httpx.WithReason(http.StatusUnprocessableEntity, detail, reason)
}

// checkDump refuses a dump that is not one: an unknown field, a bad UID, a block out of
// range or read twice, and any key trailer.
func checkDump(body []byte) error {
	d := json.NewDecoder(bytes.NewReader(body))
	d.DisallowUnknownFields()
	var v dump
	if err := d.Decode(&v); err != nil || d.More() {
		return invalid("dump_invalid", "A tag dump is JSON {uid, tech, blocks: [{index, data_hex}], records}.")
	}
	if !uidPattern.MatchString(v.UID) || len(v.UID)%2 != 0 || len(v.Tech) > 40 || len(v.Blocks) > blocksMax {
		return invalid("dump_invalid", "A tag dump needs a hex UID, a tech of at most 40 characters and at most 256 blocks.")
	}
	seen := map[int]bool{}
	for _, b := range v.Blocks {
		if b.Index < 0 || b.Index >= blocksMax || seen[b.Index] || !blockPattern.MatchString(b.DataHex) {
			return invalid("dump_invalid", "Each block has an index from 0 to 255, once, and 16 bytes as hex.")
		}
		if trailer(b.Index) {
			return invalid("key_trailer", "A tag dump never holds a sector trailer: it holds the sector's keys.")
		}
		seen[b.Index] = true
	}
	return nil
}

// sniff is a photo's media type from its first bytes: JPEG, PNG or WebP, else "".
func sniff(body []byte) string {
	switch {
	case bytes.HasPrefix(body, []byte{0xFF, 0xD8, 0xFF}):
		return "image/jpeg"
	case bytes.HasPrefix(body, []byte("\x89PNG\r\n\x1a\n")):
		return "image/png"
	case len(body) >= 12 && string(body[:4]) == "RIFF" && string(body[8:12]) == "WEBP":
		return "image/webp"
	}
	return ""
}

// upload stores what an open call read, as one blob: POST /api/v1/devices/uploads?call=&kind=,
// with the raw bytes. The kind must be its method's. It answers {key, media_type}.
func (s *Store) upload(r *http.Request, d Device) (any, error) {
	ctx := r.Context()
	callID, err := crud.QueryUUID(r, "call", false)
	if err != nil {
		return nil, err
	}
	kind, err := crud.QueryChoice(r, "kind", KindDump, KindPhoto)
	if err != nil {
		return nil, err
	}
	_, _ = s.expire(ctx, *callID)
	var call *Call
	if err := pgx.BeginFunc(ctx, s.Pool, func(tx pgx.Tx) error {
		call, err = s.openCall(ctx, tx, *callID, d)
		return err
	}); err != nil {
		return nil, err
	}
	if kind != kindOf(call.Method) {
		return nil, invalid("kind_mismatch", "A "+call.Method+" call uploads a "+kindOf(call.Method)+" blob.")
	}
	body, err := io.ReadAll(r.Body)
	if err != nil {
		return nil, err
	}
	mediaType := dumpType
	if kind == KindDump {
		if err := checkDump(body); err != nil {
			return nil, err
		}
	} else if mediaType = sniff(body); mediaType == "" {
		return nil, httpx.WithReason(http.StatusUnsupportedMediaType, "A photo is a JPEG, PNG or WebP.", "not_an_image")
	}
	stored, err := s.Blobs.Put(ctx, kind, mediaType, body)
	if err != nil {
		return nil, err
	}
	if _, err := s.Pool.Exec(ctx, `INSERT INTO blob_refs (key, holder_kind, holder_id) VALUES ($1, $2, $3)
		ON CONFLICT DO NOTHING`, stored.Key, Holder, call.ID); err != nil {
		return nil, err
	}
	return map[string]any{"key": stored.Key, "media_type": mediaType}, nil
}

// readIn is the read a done result names.
type readIn struct {
	key, uid, tech string
	complete       bool
	missing        []int
}

func readResult(b *crud.Body, method string) *readIn {
	names := []string{"blob_key", "media_type"}
	nfc := strings.HasPrefix(method, "nfc.")
	if nfc {
		names = []string{"blob_key", "uid", "tech", "complete", "missing_blocks"}
	}
	result := b.Need("result", "read")
	if result == nil {
		return nil
	}
	defer result.Done()
	n := result.Need("read", names...)
	if n == nil {
		return nil
	}
	defer n.Done()
	in := &readIn{key: n.Text("blob_key", 1, 200), complete: true}
	if nfc {
		in.uid = strings.ToUpper(n.Match("uid", 8, 20, uidPattern))
		in.tech = n.TextOr("tech", "", 0, 40)
		in.complete = n.Bool("complete", true)
		for i, raw := range n.List("missing_blocks", true, blocksMax) {
			var v int
			if json.Unmarshal(raw, &v) != nil || v < 0 || v >= blocksMax {
				n.Fail([]string{"missing_blocks", strconv.Itoa(i)}, "a block index from 0 to 255")
			}
			in.missing = append(in.missing, v)
		}
	} else if n.Present("media_type") {
		// The type is the stored bytes', not the device's word.
		n.OptText("media_type", 1, 100, nil)
	}
	return in
}

// result ends an open call from its device. outcome "error" ends it with the device's
// error; "done" records the read its result names. A read whose blob this call's device did
// not upload, or whose kind is not the method's, is refused (422 foreign_blob): no
// device_reads row, and the call ends with outcome error.
func (s *Store) result(r *http.Request, d Device) (any, error) {
	ctx := r.Context()
	id := r.PathValue("id")
	_, _ = s.expire(ctx, id)
	b, err := crud.ReadBody(r, "outcome", "result", "error")
	if err != nil {
		return nil, err
	}
	outcome := b.Choice("outcome", "done", "error")
	if err := b.Err(); err != nil {
		return nil, err
	}
	now := s.now()
	var (
		call     *Call
		in       *readIn
		readID   string
		refusal  error
		failText string
	)
	err = pgx.BeginFunc(ctx, s.Pool, func(tx pgx.Tx) error {
		call, err = s.openCall(ctx, tx, id, d)
		if err != nil {
			return err
		}
		if outcome == "error" {
			failText = b.TextOr("error", "the device failed", 1, 200)
			if err := b.Err(); err != nil {
				return err
			}
			return end(ctx, tx, id, "error", map[string]any{"error": failText}, now)
		}
		in = readResult(b, call.Method)
		if err := b.Err(); err != nil {
			return err
		}
		kind := kindOf(call.Method)
		var held bool
		if err := tx.QueryRow(ctx, `SELECT EXISTS (SELECT 1 FROM blob_refs WHERE key = $1 AND holder_kind = $2
			AND holder_id = $3)`, in.key, Holder, call.ID).Scan(&held); err != nil {
			return err
		}
		if !held || !strings.HasPrefix(in.key, kind+"-") {
			failText = "foreign_blob"
			refusal = invalid("foreign_blob", "The result names a blob this call's device did not upload as a "+kind+".")
			return end(ctx, tx, id, "error", map[string]any{"error": failText}, now)
		}
		mediaType := dumpType
		readKind := "nfc"
		if kind == KindPhoto {
			readKind = "photo"
			if mediaType, err = s.mediaType(ctx, in.key); err != nil {
				return err
			}
		}
		var uid, tech *string
		if in.uid != "" {
			uid = &in.uid
		}
		if in.tech != "" {
			tech = &in.tech
		}
		missing := in.missing
		if missing == nil {
			missing = []int{}
		}
		if err := tx.QueryRow(ctx, `INSERT INTO device_reads (device_id, call_id, at, kind, uid, tech, blob_key,
			media_type, complete, missing_blocks) VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6, $7, $8, $9, $10)
			RETURNING id::text`, d.ID, call.ID, now, readKind, uid, tech, in.key, mediaType, in.complete, missing).
			Scan(&readID); err != nil {
			return err
		}
		return end(ctx, tx, id, "done", doneResult(readID, kind, in), now)
	})
	if err != nil {
		return nil, err
	}
	if outcome == "error" || refusal != nil {
		s.publishFailed(ctx, id, d.ID, "error", failText)
		if refusal != nil {
			return nil, refusal
		}
		return &crud.Reply{Status: http.StatusNoContent}, nil
	}
	fields := []httpx.Field{{Key: "call", Value: id}, {Key: "device", Value: d.ID}, {Key: "read_id", Value: readID}}
	if kindOf(call.Method) == KindDump {
		fields = append(fields, httpx.Field{Key: "uid", Value: in.uid}, httpx.Field{Key: "complete", Value: in.complete})
	}
	fields = append(fields, httpx.Field{Key: "topics", Value: []string{"device:" + d.ID, "device:call:" + id}},
		httpx.Field{Key: "retention_seconds", Value: Retention})
	bus.Publish(ctx, s.Pool, "device.call_done", fields...)
	return doneResult(readID, kindOf(call.Method), in), nil
}

func doneResult(readID, kind string, in *readIn) map[string]any {
	out := map[string]any{"read_id": readID}
	if kind == KindDump {
		out["uid"], out["complete"] = in.uid, in.complete
	}
	return out
}

// mediaType is a stored photo's type, read from its first bytes.
func (s *Store) mediaType(ctx context.Context, key string) (string, error) {
	body, _, err := s.Blobs.OpenKey(ctx, key)
	if err != nil {
		return "", err
	}
	defer body.Close()
	head := make([]byte, 16)
	n, _ := io.ReadFull(body, head)
	if t := sniff(head[:n]); t != "" {
		return t, nil
	}
	return "application/octet-stream", nil
}

// Read is a device read, as GET /api/v1/devices/reads/{id} answers it.
type Read struct {
	ID            string    `json:"id"`
	DeviceID      string    `json:"device_id"`
	CallID        string    `json:"call_id"`
	At            crud.Time `json:"at"`
	Kind          string    `json:"kind"`
	UID           *string   `json:"uid"`
	Tech          *string   `json:"tech"`
	BlobKey       string    `json:"blob_key"`
	MediaType     string    `json:"media_type"`
	Complete      bool      `json:"complete"`
	MissingBlocks []int     `json:"missing_blocks"`

	owner string
}

// ReadOf is a device read for a Go consumer (the tag and code decoders): no owner check.
func (s *Store) ReadOf(ctx context.Context, id string) (Read, error) {
	var out Read
	var at time.Time
	if !crud.IsUUID(id) {
		return out, notFound("read")
	}
	err := s.Pool.QueryRow(ctx, `SELECT r.id::text, r.device_id::text, r.call_id::text, r.at, r.kind, r.uid, r.tech,
		r.blob_key, r.media_type, r.complete, r.missing_blocks, d.owner FROM device_reads r
		JOIN devices d ON d.id = r.device_id WHERE r.id = $1::uuid`, id).Scan(&out.ID, &out.DeviceID, &out.CallID,
		&at, &out.Kind, &out.UID, &out.Tech, &out.BlobKey, &out.MediaType, &out.Complete, &out.MissingBlocks, &out.owner)
	if errors.Is(err, pgx.ErrNoRows) {
		return out, notFound("read")
	}
	out.At = crud.Time(at)
	return out, err
}

func (s *Store) ownRead(r *http.Request) (Read, error) {
	read, err := s.ReadOf(r.Context(), r.PathValue("id"))
	if err != nil {
		return read, err
	}
	if read.owner != principal(r.Context()) {
		return Read{}, notFound("read")
	}
	return read, nil
}

func (s *Store) getRead(r *http.Request) (any, error) {
	return s.ownRead(r)
}

// readBlob answers a read's bytes with their media type, never sniffed by the browser.
func (s *Store) readBlob(r *http.Request) (any, error) {
	read, err := s.ownRead(r)
	if err != nil {
		return nil, err
	}
	body, ref, err := s.Blobs.OpenKey(r.Context(), read.BlobKey)
	if err != nil {
		return nil, err
	}
	defer body.Close()
	bytes, err := io.ReadAll(io.LimitReader(body, ref.Size+1))
	if err != nil {
		return nil, err
	}
	return &crud.Reply{Status: http.StatusOK, MediaType: read.MediaType, Body: bytes,
		Headers: map[string]string{"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"}}, nil
}
