package tagreads

import (
	"context"
	"encoding/json"
	"net/http"
	"time"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/bambutag"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
)

// Recorded is what RecordMifare wrote.
type Recorded struct {
	// TagID is the tag the read names now; ReadID its filament_tag_reads row, "" for a read
	// with tap false.
	TagID, ReadID string
	// DumpKey is the raw dump's content index key (kind tag_dump).
	DumpKey string
	// Bambu is the decoded read, as the answer's bambu gives it.
	Bambu bambutag.Tag
}

// sources are filament_tag_reads.source's values.
var sources = map[string]bool{"phone": true, "app": true, "agent": true, "ams": true}

// RecordMifare records a MIFARE read from another Go route (#8157: a scan job's result),
// without Python's part (no last-seen mark, no bind, no answer). body is the shape
// POST /api/v1/filament/tags/reads takes: {uid, tag_type, records, mifare_blocks}, with an
// optional image and location. actor is who the history row names, source one of phone,
// app, agent or ams. A refusal is an *httpx.Problem.
func (s *Reads) RecordMifare(ctx context.Context, body []byte, actor, source string) (Recorded, error) {
	if !sources[source] {
		return Recorded{}, problem(http.StatusInternalServerError, "bad_source", "no read source "+source)
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(body, &fields); err != nil || fields == nil {
		return Recorded{}, httpx.Invalid(httpx.Issue{Loc: []string{"body"}, Msg: "a tag read is a JSON object"})
	}
	img, p := s.imageOf(ctx, fields)
	if p != nil {
		return Recorded{}, p
	}
	at, p := locationOf(fields, time.Now())
	if p != nil {
		return Recorded{}, p
	}
	rd, p := parse(fields)
	if p != nil {
		return Recorded{}, p
	}
	return s.recordMifare(ctx, rd, scanFacts{image: img, location: at}, actor, source, false)
}

// recordMifare decodes rd, stores its dump and writes the tag and the history row.
func (s *Reads) recordMifare(ctx context.Context, rd read, scan scanFacts, actor, source string, noTap bool) (Recorded, error) {
	tag, err := bambutag.Decode(rd.uid, rd.blocks)
	if err != nil {
		return Recorded{}, problem(http.StatusUnprocessableEntity, "mifare_unreadable", err.Error())
	}
	d := dump{Format: dumpFormat, UID: tag.UID, TagType: rd.tagType, Blocks: []dumpBlock{}, MissingBlocks: tag.MissingBlocks}
	for _, b := range tag.Blocks {
		d.Blocks = append(d.Blocks, dumpBlock{Index: b.Block, DataHex: b.Hex})
	}
	body, _ := httpx.Marshal(d)
	put, err := s.Blobs.Put(ctx, dumpKind, mediaType, body)
	if err != nil {
		return Recorded{}, err
	}
	key := put.Key
	stored := tag
	stored.Blocks, stored.Signature.Blocks = []bambutag.RawBlock{}, []bambutag.RawBlock{}
	decoded, _ := json.Marshal(stored)
	tray := ""
	if tag.TrayUID != nil {
		tray = cleanTray(*tag.TrayUID)
	}
	saved, err := record(ctx, s.Pool, recordIn{
		uid: CleanUID(tag.UID), tray: tray, tagType: rd.tagType, actor: actor, source: source,
		dumpKey: key, complete: tag.Complete, decoded: decoded, scan: scan,
		bambu: tray != "" || tag.FilamentType != nil, noTap: noTap,
	})
	if err != nil {
		return Recorded{}, err
	}
	return Recorded{TagID: saved.tagID, ReadID: saved.readID, DumpKey: key, Bambu: tag}, nil
}
