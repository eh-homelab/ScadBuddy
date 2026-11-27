package tagreads

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"unicode/utf8"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/bambutag"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/blobstore"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
)

// dumpMax bounds a device's tag_dump blob as this route reads it: a MIFARE Classic 4K's 256
// blocks as JSON are about 13 KiB.
const dumpMax = 64 << 10

// inlineFields are the fields of a read that a read_id comes in place of.
var inlineFields = []string{"uid", "tag_type", "records", "mifare_blocks"}

// deviceDump is a device read's tag_dump blob (devices.checkDump has its shape).
type deviceDump struct {
	UID     string            `json:"uid"`
	Tech    string            `json:"tech"`
	Blocks  []wireBlock       `json:"blocks"`
	Records []json.RawMessage `json:"records"`
}

// fromDevice puts a paired device's read in fields in place of read_id (#8244): the dump's
// UID, its tech as tag_type, and its blocks as mifare_blocks, or its records when it holds
// no block. The read is then decoded as an inline read is. It answers the read's id. A read
// of a device the caller does not own is a 404, as a read that is not there.
func (s *Reads) fromDevice(ctx context.Context, fields map[string]json.RawMessage) (string, *httpx.Problem) {
	refuse := func(loc, msg string) (string, *httpx.Problem) {
		p := httpx.Invalid(httpx.Issue{Loc: []string{"body", loc}, Msg: msg})
		p.Extensions = append(p.Extensions, httpx.Field{Key: "code", Value: "device_read_invalid"})
		return "", p
	}
	for _, k := range inlineFields {
		// An empty records is the Python model's default, which a generated client may send.
		if v, ok := fields[k]; ok && !(k == "records" && (isNull(v) || string(bytes.TrimSpace(v)) == "[]")) {
			return refuse(k, "read_id comes in place of the read's own fields")
		}
	}
	var id string
	if err := json.Unmarshal(fields["read_id"], &id); err != nil || id == "" {
		return refuse("read_id", "read_id is a device read's id")
	}
	rd, err := s.Devices.OwnRead(ctx, id)
	var p *httpx.Problem
	if errors.As(err, &p) && p.Status == http.StatusNotFound {
		// The tag routes' refusals carry a code (problem).
		return "", problem(http.StatusNotFound, "read_not_found", "That read is not there.")
	}
	if err != nil {
		return "", asProblem(ctx, err)
	}
	if rd.Kind != "nfc" {
		return refuse("read_id", "this device read is a photo, not a tag")
	}
	body, err := s.readDump(ctx, rd.BlobKey)
	if err != nil {
		return "", asProblem(ctx, err)
	}
	var d deviceDump
	if err := json.Unmarshal(body, &d); err != nil {
		return "", problem(http.StatusUnprocessableEntity, invalidCode, "the device read's dump is not a tag dump")
	}
	delete(fields, "read_id")
	fields["uid"], _ = json.Marshal(d.UID)
	if tech := d.Tech; tech != "" {
		// A device's tech may be up to 40 bytes; tag_type holds 32. The cut keeps whole runes.
		for len(tech) > tagTypeMax {
			_, size := utf8.DecodeLastRuneInString(tech)
			tech = tech[:len(tech)-size]
		}
		fields["tag_type"], _ = json.Marshal(tech)
	}
	if classic1K(d) {
		fields["mifare_blocks"], _ = json.Marshal(d.Blocks)
	} else {
		// No MIFARE Classic 1K read (no block, a UID that is not 4 bytes, or a 4K's blocks):
		// a UID-only read with the dump's records, as the page's fallback posts it.
		records := d.Records
		if records == nil {
			records = []json.RawMessage{}
		}
		fields["records"], _ = json.Marshal(records)
	}
	return rd.ID, nil
}

// classic1K says whether d is a MIFARE Classic 1K read that parse takes: a 4-byte UID and
// blocks 0 to 63 only.
func classic1K(d deviceDump) bool {
	if len(d.Blocks) == 0 || len(hexDigits(d.UID)) != 8 {
		return false
	}
	for _, b := range d.Blocks {
		if b.Index == nil || *b.Index >= bambutag.BlockCount {
			return false
		}
	}
	return true
}

// readDump is the bytes of the tag_dump blob key.
func (s *Reads) readDump(ctx context.Context, key string) ([]byte, error) {
	stream, ref, err := s.Blobs.OpenKey(ctx, key)
	switch {
	case errors.Is(err, blobstore.ErrBlobNotFound):
		return nil, problem(http.StatusNotFound, "dump_not_found", "the device read's dump is not in the store")
	case errors.Is(err, blobstore.ErrNotConfigured):
		return nil, problem(http.StatusServiceUnavailable, "not_configured", "device reads need store_backend s3")
	case err != nil:
		return nil, err
	}
	defer stream.Close()
	if ref.Kind != dumpKind || ref.Size > dumpMax {
		return nil, problem(http.StatusUnprocessableEntity, invalidCode, "the device read's blob is not a tag dump")
	}
	return io.ReadAll(io.LimitReader(stream, dumpMax+1))
}

// asProblem is err as a problem: a refusal as it is, any other error logged and a 500.
func asProblem(ctx context.Context, err error) *httpx.Problem {
	var p *httpx.Problem
	if errors.As(err, &p) {
		return p
	}
	slog.ErrorContext(ctx, "a device read could not be loaded", "error", err)
	return &httpx.Problem{Status: http.StatusInternalServerError, Detail: "the device read could not be loaded"}
}
