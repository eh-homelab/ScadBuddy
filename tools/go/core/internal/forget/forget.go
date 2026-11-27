// Package forget is area forget of backend/scadbuddy/api/crud_agent_forget.py, with
// agent_data/forget.py and agent_data/flow_blobs.py, statement for statement: a subject
// forgotten (subject.go: a session's forks first take their ranges of its transcript, opened
// and sealed under the KEK), a browser call's key dropped, the keys no forget deletes swept,
// and a flow run's images kept and read.
package forget

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"mime"
	"mime/multipart"
	"net/http"
	"regexp"
	"strings"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/seal"
)

const (
	bridgeKeyMaxAgeS  = 3600
	orphanKeyMinAgeS  = 24 * 3600
	orphanSweepLimit  = 100
	ageMin            = 60
	ageMax            = 30 * 86400
	putImagesMax      = 16
	imageBytesMax     = 32 * 1024 * 1024
	putBytesMax       = 64 * 1024 * 1024
	flowImagesMax     = 256
	multipartBodyMax  = 32 * 1024 * 1024 // api/limits.py MAX_MULTIPART_BODY_BYTES
	fieldBytesMax     = 1024 * 1024      // Starlette's max_part_size for a field
	mediaTypeHeader   = "X-ScadBuddy-Media-Type"
	jsonBodyBytesMax  = 8 * 1024 * 1024 // api/limits.py MAX_TEXT_BODY_BYTES
	subjectForgotten  = "subject-forgotten"
	noSuchImage       = "no-such-image"
	lock              = `SELECT pg_advisory_xact_lock(hashtext($1))`
	deleteKey         = `DELETE FROM ai_payload_keys WHERE subject = $1`
	payloadKeyLockPre = "scadbuddy:ai_payload_keys:"
)

var (
	bridgeSubject = regexp.MustCompile(`^bridge-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`)
	flowSubject   = regexp.MustCompile(`^flow-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`)
	blobName      = regexp.MustCompile(`^[0-9a-f]{64}\.(png|jpg|gif|webp)$`)
	extensions    = map[string]string{"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}
)

// Forget is the area's routes. Keys may be nil: a forget whose forks take transcript entries
// is then refused.
type Forget struct {
	Pool *pgxpool.Pool
	Keys *seal.Keys
}

// Reads are the area's GET route.
func (f *Forget) Reads() []crud.Route {
	return []crud.Route{{Method: "GET", Path: "/flow-blobs/{subject}/{name}", Handle: f.getBlob}}
}

// Writes are the area's writes.
func (f *Forget) Writes() []crud.Route {
	return []crud.Route{
		{Method: "POST", Path: "/forgotten-subjects/{subject}", Handle: f.forget},
		{Method: "POST", Path: "/payload-keys/{subject}/drop", Handle: f.drop},
		{Method: "POST", Path: "/payload-key-sweeps", Handle: f.sweep, MaxBody: jsonBodyBytesMax},
		{Method: "POST", Path: "/flow-blobs/{subject}", Handle: f.put, MaxBody: multipartBodyMax},
	}
}

// drop is a browser call's key deleted once its caller has the answer, under the subject's
// lock; no tombstone, as nothing takes the id again.
func (f *Forget) drop(r *http.Request, p crud.Params) (any, error) {
	subject, err := p.Match("subject", bridgeSubject)
	if err != nil {
		return nil, err
	}
	dropped := false
	ctx := r.Context()
	err = pgx.BeginFunc(ctx, f.Pool, func(tx pgx.Tx) error {
		if _, err := tx.Exec(ctx, lock, payloadKeyLockPre+subject); err != nil {
			return err
		}
		tag, err := tx.Exec(ctx, deleteKey, subject)
		dropped = tag.RowsAffected() > 0
		return err
	})
	if err != nil {
		return nil, err
	}
	return struct {
		Dropped bool `json:"dropped"`
	}{dropped}, nil
}

// sweep deletes a session's key whose row is gone, older than orphan_min_age_s and checked
// again under its lock, one transaction each; and a browser call's older than
// bridge_max_age_s. Not a forget: no tombstone.
func (f *Forget) sweep(r *http.Request, _ crud.Params) (any, error) {
	body, err := crud.ReadBody(r, "orphan_min_age_s", "bridge_max_age_s")
	if err != nil {
		return nil, err
	}
	orphanAge, bridgeAge := int64(orphanKeyMinAgeS), int64(bridgeKeyMaxAgeS)
	if v := body.Get("orphan_min_age_s"); v.Given() {
		orphanAge = v.Int(ageMin, ageMax, false)
	}
	if v := body.Get("bridge_max_age_s"); v.Given() {
		bridgeAge = v.Int(ageMin, ageMax, false)
	}
	if err := body.Err(); err != nil {
		return nil, err
	}
	ctx := r.Context()
	conn, err := f.Pool.Acquire(ctx)
	if err != nil {
		return nil, err
	}
	defer conn.Release()
	var subjects []string
	err = pgx.BeginFunc(ctx, conn, func(tx pgx.Tx) error {
		rows, err := tx.Query(ctx, `SELECT k.subject FROM ai_payload_keys k
			WHERE k.subject LIKE 'session-%'
			AND k.created_at < now() - make_interval(secs => $1::int)
			AND NOT EXISTS (SELECT 1 FROM ai_sessions s WHERE s.id::text = substr(k.subject, 9))
			LIMIT $2`, orphanAge, orphanSweepLimit)
		if err != nil {
			return err
		}
		subjects, err = pgx.CollectRows(rows, pgx.RowTo[string])
		return err
	})
	if err != nil {
		return nil, err
	}
	var orphaned, bridge int64
	for _, subject := range subjects {
		session := strings.TrimPrefix(subject, "session-")
		err := pgx.BeginFunc(ctx, conn, func(tx pgx.Tx) error {
			if _, err := tx.Exec(ctx, lock, payloadKeyLockPre+subject); err != nil {
				return err
			}
			tag, err := tx.Exec(ctx, `DELETE FROM ai_payload_keys WHERE subject = $1
				AND NOT EXISTS (SELECT 1 FROM ai_sessions WHERE id::text = $2)
				AND NOT EXISTS (SELECT 1 FROM ai_session_entries WHERE session_id::text = $2)`,
				subject, session)
			orphaned += tag.RowsAffected()
			return err
		})
		if err != nil {
			return nil, err
		}
	}
	err = pgx.BeginFunc(ctx, conn, func(tx pgx.Tx) error {
		tag, err := tx.Exec(ctx, `DELETE FROM ai_payload_keys WHERE subject LIKE 'bridge-%'
			AND created_at < now() - make_interval(secs => $1::int)`, bridgeAge)
		bridge = tag.RowsAffected()
		return err
	})
	if err != nil {
		return nil, err
	}
	return struct {
		Orphaned int64 `json:"orphaned"`
		Bridge   int64 `json:"bridge"`
	}{orphaned, bridge}, nil
}

type image struct {
	name, mediaType string
	size            int64
	data            []byte
}

type part struct {
	name   string
	isFile bool
	data   []byte
}

func refused(detail string) error {
	return &httpx.Problem{Status: http.StatusUnprocessableEntity, Detail: detail}
}

// form reads a multipart body as Starlette does with max_files and max_fields: a part with a
// file name is a file, any other a field of at most 1 MiB.
func form(r *http.Request) ([]part, error) {
	_, params, err := mime.ParseMediaType(r.Header.Get("Content-Type"))
	if err != nil || params["boundary"] == "" {
		return nil, refused("the images' body was refused: Missing boundary in multipart.")
	}
	reader := multipart.NewReader(r.Body, params["boundary"])
	var parts []part
	files, fields := 0, 0
	for {
		p, err := reader.NextRawPart()
		if errors.Is(err, io.EOF) {
			return parts, nil
		}
		var tooLarge *http.MaxBytesError
		if errors.As(err, &tooLarge) {
			return nil, err
		}
		if err != nil {
			return nil, refused("the images' body was refused: There was an error parsing the body")
		}
		_, disposition, _ := mime.ParseMediaType(p.Header.Get("Content-Disposition"))
		_, isFile := disposition["filename"]
		if isFile {
			if files++; files > putImagesMax {
				return nil, refused(fmt.Sprintf(
					"the images' body was refused: Too many files. Maximum number of files is %d.", putImagesMax))
			}
		} else if fields++; fields > 1 {
			return nil, refused("the images' body was refused: Too many fields. Maximum number of fields is 1.")
		}
		data, err := io.ReadAll(p)
		if errors.As(err, &tooLarge) {
			return nil, err
		}
		if err != nil {
			return nil, refused("the images' body was refused: There was an error parsing the body")
		}
		if !isFile && len(data) > fieldBytesMax {
			return nil, refused("the images' body was refused: Part exceeded maximum size of 1024KB.")
		}
		parts = append(parts, part{disposition["name"], isFile, data})
	}
}

// metas is the images part: a JSON list of {name, media_type, size}, each checked.
func metas(raw []byte) ([]image, bool) {
	var items []map[string]json.RawMessage
	if json.Unmarshal(raw, &items) != nil || items == nil {
		return nil, false
	}
	out := make([]image, len(items))
	for i, item := range items {
		if item == nil || len(item) != 3 {
			return nil, false
		}
		var name, media *string
		var size *float64
		if json.Unmarshal(item["name"], &name) != nil || name == nil || !blobName.MatchString(*name) ||
			json.Unmarshal(item["media_type"], &media) != nil || media == nil ||
			json.Unmarshal(item["size"], &size) != nil || size == nil ||
			*size != math.Trunc(*size) || *size <= 0 || *size > imageBytesMax {
			return nil, false
		}
		out[i] = image{name: *name, mediaType: *media, size: int64(*size)}
	}
	return out, true
}

func sealedImages(r *http.Request) ([]image, error) {
	kind, _, _ := strings.Cut(r.Header.Get("Content-Type"), ";")
	if strings.TrimSpace(kind) != "multipart/form-data" {
		return nil, refused("the images come as multipart/form-data")
	}
	parts, err := form(r)
	if err != nil {
		return nil, err
	}
	var images *part
	var blobs []part
	for i := range parts {
		switch parts[i].name {
		case "images":
			if images == nil {
				images = &parts[i]
			}
		case "blob":
			blobs = append(blobs, parts[i])
		}
	}
	if images == nil || images.isFile {
		return nil, refused("the part images is the JSON list of the images")
	}
	out, ok := metas(images.data)
	if !ok {
		return nil, refused("the part images is not a list of {name, media_type, size}")
	}
	if len(out) < 1 || len(out) > putImagesMax || len(blobs) != len(out) {
		return nil, refused(fmt.Sprintf("1 to %d images, each with one blob part", putImagesMax))
	}
	total := 0
	for i := range out {
		_, ext, _ := strings.Cut(out[i].name, ".")
		if want, ok := extensions[out[i].mediaType]; !ok || want != ext {
			return nil, refused(fmt.Sprintf("%s is not named for %s", out[i].name, out[i].mediaType))
		}
		if !blobs[i].isFile {
			return nil, refused("each blob part is a file")
		}
		data := blobs[i].data
		total += len(data)
		if len(data) == 0 || len(data) > imageBytesMax || total > putBytesMax {
			return nil, refused(fmt.Sprintf("an image is 1 to %d bytes, a put %d", imageBytesMax, putBytesMax))
		}
		out[i].data = data
	}
	return out, nil
}

// put keeps a run's images; one already there is left as it is. Under the lock forgetting
// takes, so no row lands behind a forget that just committed, and two calls of one run never
// both take the last room.
func (f *Forget) put(r *http.Request, p crud.Params) (any, error) {
	subject, err := p.Match("subject", flowSubject)
	if err != nil {
		return nil, err
	}
	images, err := sealedImages(r)
	if err != nil {
		return nil, err
	}
	ctx := r.Context()
	var names []string
	gone := false
	err = pgx.BeginFunc(ctx, f.Pool, func(tx pgx.Tx) error {
		if _, err := tx.Exec(ctx, lock, payloadKeyLockPre+subject); err != nil {
			return err
		}
		var one int
		err := tx.QueryRow(ctx, `SELECT 1 FROM ai_forgotten_subjects WHERE subject = $1`, subject).Scan(&one)
		if err == nil {
			gone = true
			return nil
		}
		if !errors.Is(err, pgx.ErrNoRows) {
			return err
		}
		rows, err := tx.Query(ctx, `SELECT name FROM ai_flow_blobs WHERE subject = $1`, subject)
		if err != nil {
			return err
		}
		heldNames, err := pgx.CollectRows(rows, pgx.RowTo[string])
		if err != nil {
			return err
		}
		held := map[string]bool{}
		for _, n := range heldNames {
			held[n] = true
		}
		var fresh []image
		seen := map[string]bool{}
		for _, img := range images {
			if !held[img.name] && !seen[img.name] {
				fresh = append(fresh, img)
				seen[img.name] = true
			}
		}
		room := max(0, flowImagesMax-len(held))
		for _, img := range fresh[:min(room, len(fresh))] {
			if _, err := tx.Exec(ctx, `INSERT INTO ai_flow_blobs (subject, name, media_type, size, data)
				VALUES ($1, $2, $3, $4, $5) ON CONFLICT (subject, name) DO NOTHING`,
				subject, img.name, img.mediaType, img.size, img.data); err != nil {
				return err
			}
			held[img.name] = true
		}
		names = []string{}
		listed := map[string]bool{}
		for _, img := range images {
			if !listed[img.name] && held[img.name] {
				names = append(names, img.name)
			}
			listed[img.name] = true
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	if gone {
		return nil, httpx.WithReason(http.StatusGone,
			fmt.Sprintf("%s was forgotten; its images are not kept.", subject), subjectForgotten)
	}
	return struct {
		Names []string `json:"names"`
	}{names}, nil
}

func (f *Forget) getBlob(r *http.Request, p crud.Params) (any, error) {
	subject, err := p.Match("subject", flowSubject)
	if err != nil {
		return nil, err
	}
	name, err := p.Match("name", blobName)
	if err != nil {
		return nil, err
	}
	var media string
	var data []byte
	err = f.Pool.QueryRow(r.Context(),
		`SELECT media_type, data FROM ai_flow_blobs WHERE subject = $1 AND name = $2`,
		subject, name).Scan(&media, &data)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, httpx.WithReason(http.StatusNotFound,
			fmt.Sprintf("%s has no image %s.", subject, name), noSuchImage)
	}
	if err != nil {
		return nil, err
	}
	return &crud.Reply{
		Status:    http.StatusOK,
		MediaType: "application/octet-stream",
		Headers:   map[string]string{mediaTypeHeader: media},
		Body:      bytes.Clone(data),
	}, nil
}
