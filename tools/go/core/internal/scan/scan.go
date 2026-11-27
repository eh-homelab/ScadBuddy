// Package scan is area scan (#8155): a code scanned from a photo, decoded in core. The page
// sends the photo's bytes (POST /api/v1/scan/images), core keeps them as a scan_image blob,
// and POST /api/v1/scan/codes decodes every QR code, Data Matrix, EAN, UPC and Code 128 in
// it and resolves each text: a spool label's payload names its spool, a place label's
// address or a typed code what GET /api/v1/codes/{code} names. Each scan is a code_scans
// row that holds its photo in blob_refs (holder code_scan) until the scan is deleted, so the
// photo is shown again and a failed decode can be decoded again.
//
// The page cannot PUT the photo to the bucket itself: the bucket's CORS rule and the app's
// CSP admit only the storefront. So core takes the bytes and stores them with
// blobstore.Put, the commit's own steps without a staging object.
//
// Record is the same scan for a Go caller that holds the bytes already (#8157's result
// route): no person-auth upload step.
package scan

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/auth"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/blobstore"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/devices"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/seal"
)

const (
	// Area is the area's name in SCADBUDDY_CORE_AREAS.
	Area = "scan"
	// Kind is the photo's blob kind (blobstore.Kinds has its limit).
	Kind = "scan_image"
	// Holder is the photo's holder kind in blob_refs, with the scan's id.
	Holder = "code_scan"
	// decodeSlots is how many photos are decoded at once: each may take 96 MiB of RGBA.
	decodeSlots = 2
	// slotWait is how long a scan waits for a slot; then 503 scan_busy.
	slotWait = 10 * time.Second
	// listMax is the most scans a list answers.
	listMax = 100
	bodyMax = 4 << 10
)

// Sources are where a scan's photo came from: the page's shutter, a frame taken at an NFC
// read, a file the person chose, the phone app (#8157).
var Sources = []string{"shutter", "nfc", "file", "app"}

// Scans serves the area.
type Scans struct {
	Pool  *pgxpool.Pool
	Blobs *blobstore.Blobs
	// Devices loads a body's read_id, a paired device's photo (#8244).
	Devices *devices.Store
	slots   chan struct{}
}

// New is the area over pool and the blob store.
func New(pool *pgxpool.Pool, blobs *blobstore.Blobs) *Scans {
	return &Scans{Pool: pool, Blobs: blobs, Devices: &devices.Store{Pool: pool}, slots: make(chan struct{}, decodeSlots)}
}

// PublicAreas are the area's public routes (main.go publicPackages). The blob store opens
// its secret key with the KEK; without one it is a 503 not_configured at each call.
func PublicAreas(pool *pgxpool.Pool, keys *seal.Keys) map[string][]auth.Public {
	return map[string][]auth.Public{Area: New(pool, blobstore.New(pool, keys, lookupEnv, nil)).Public()}
}

// lookupEnv is the process's environment, which the blob store reads its settings from.
var lookupEnv = os.LookupEnv

// Public are the area's routes.
func (s *Scans) Public() []auth.Public {
	route := func(method, path string, handle func(*http.Request) (any, error), maxBody int64, scriptless bool) auth.Public {
		return auth.Public{
			Pattern: method + " " + path,
			Route:   auth.Route{Class: "api", Template: path, Scriptless: scriptless},
			Handler: crud.Serve(handle, maxBody),
		}
	}
	return []auth.Public{
		// The bytes come through core until the page can PUT a presigned upload (#8162).
		route("POST", "/api/v1/scan/images", s.upload, blobstore.Kinds[Kind], false),
		route("POST", "/api/v1/scan/codes", s.codes, bodyMax, false),
		route("GET", "/api/v1/scan/scans", s.list, 0, false),
		route("GET", "/api/v1/scan/scans/{id}", s.get, 0, false),
		route("GET", "/api/v1/scan/scans/{id}/image", s.image, 0, true),
		route("POST", "/api/v1/scan/scans/{id}/decode", s.decodeAgain, 0, false),
		route("DELETE", "/api/v1/scan/scans/{id}", s.remove, 0, false),
	}
}

// Image is a photo's blob, as a pointer: never its bytes in JSON (#2479).
type Image struct {
	Key       string `json:"key"`
	MediaType string `json:"media_type"`
}

// Stored is the answer to an upload.
type Stored struct {
	Image
	SHA256 string `json:"sha256"`
	Size   int64  `json:"size"`
}

// Location is where the page was when it took the photo, from its GPS fix.
type Location struct {
	Lat       float64    `json:"lat"`
	Lon       float64    `json:"lon"`
	AccuracyM *float64   `json:"accuracy_m"`
	At        *crud.Time `json:"at"`
}

// Found is one code a scan read, and the thing it names (nil when none).
type Found struct {
	Code
	Resolved *Resolved `json:"resolved"`
}

// Scan is one scan, as the routes answer it.
type Scan struct {
	ID        string    `json:"id"`
	At        crud.Time `json:"at"`
	Source    string    `json:"source"`
	Actor     *string   `json:"actor"`
	Image     Image     `json:"image"`
	Width     *int      `json:"width"`
	Height    *int      `json:"height"`
	Codes     []Found   `json:"codes"`
	Error     *string   `json:"error"`
	Location  *Location `json:"location"`
	DecodedAt crud.Time `json:"decoded_at"`
}

// Input is one scan to record: its source, who made it, and where.
type Input struct {
	Source   string
	Actor    *string
	Location *Location
}

func refused(status int, reason, detail string) error {
	return httpx.WithReason(status, detail, reason)
}

func notFound() error { return refused(http.StatusNotFound, "scan_not_found", "no such scan") }

// actorOf is who a request is from, as code_scans.actor keeps it; nil for no one.
func actorOf(ctx context.Context) *string {
	c := auth.CallerOf(ctx)
	var actor string
	switch c.Kind {
	case auth.KindPerson, auth.KindToken:
		actor = c.Principal
	case auth.KindService:
		actor = "service:" + c.Service.Caller
	}
	if actor == "" {
		return nil
	}
	return &actor
}

// Store keeps body as a scan_image blob. A body that is no JPEG, PNG or WebP is a 415
// (reason image_not_supported), a HEIC one with its own words. The media type is the one
// the bytes are, never the one a caller names.
func (s *Scans) Store(ctx context.Context, body []byte) (Stored, error) {
	p, err := sniff(body)
	if err != nil {
		return Stored{}, refused(http.StatusUnsupportedMediaType, "image_not_supported", err.Error())
	}
	done, err := s.Blobs.Put(ctx, Kind, p.MediaType, body)
	if err != nil {
		return Stored{}, err
	}
	return Stored{Image: Image{Key: done.Key, MediaType: p.MediaType}, SHA256: done.SHA256, Size: done.Size}, nil
}

// Record is a scan of body: stored, decoded and recorded, for a Go caller that holds the
// photo's bytes (#8157).
func (s *Scans) Record(ctx context.Context, body []byte, in Input) (Scan, error) {
	stored, err := s.Store(ctx, body)
	if err != nil {
		return Scan{}, err
	}
	return s.scan(ctx, stored.Key, body, in)
}

// Decode is a scan of the stored photo key: decoded and recorded. A key that is no
// scan_image blob is a 404 (reason image_not_found).
func (s *Scans) Decode(ctx context.Context, key string, in Input) (Scan, error) {
	body, err := s.read(ctx, key)
	if err != nil {
		return Scan{}, err
	}
	return s.scan(ctx, key, body, in)
}

// read is the photo key's bytes, read to the end (the blob store checks them there).
func (s *Scans) read(ctx context.Context, key string) ([]byte, error) {
	if !strings.HasPrefix(key, Kind+"-") {
		return nil, refused(http.StatusNotFound, "image_not_found", "no such scan photo")
	}
	stream, ref, err := s.Blobs.OpenKey(ctx, key)
	switch {
	case errors.Is(err, blobstore.ErrBlobNotFound):
		return nil, refused(http.StatusNotFound, "image_not_found", "no such scan photo")
	case errors.Is(err, blobstore.ErrNotConfigured):
		return nil, refused(http.StatusServiceUnavailable, "not_configured", "scans need store_backend s3")
	case err != nil:
		return nil, err
	}
	defer stream.Close()
	if ref.Kind != Kind {
		return nil, refused(http.StatusNotFound, "image_not_found", "no such scan photo")
	}
	body, err := io.ReadAll(stream)
	if errors.Is(err, blobstore.ErrBlobMismatch) {
		return nil, refused(http.StatusConflict, "store_object_mismatch", "the store's object is not the photo's bytes")
	}
	if err != nil {
		return nil, refused(http.StatusServiceUnavailable, "store_unavailable", "the photo could not be read")
	}
	return body, nil
}

// slot waits for a decode slot; release gives it back.
func (s *Scans) slot(ctx context.Context) (release func(), err error) {
	timer := time.NewTimer(slotWait)
	defer timer.Stop()
	select {
	case s.slots <- struct{}{}:
		return func() { <-s.slots }, nil
	case <-timer.C:
		return nil, refused(http.StatusServiceUnavailable, "scan_busy", "every decode is busy; scan again")
	case <-ctx.Done():
		return nil, ctx.Err()
	}
}

// decoded is body's codes, decoded in a slot.
func (s *Scans) decoded(ctx context.Context, body []byte) (photo, []Code, string, error) {
	release, err := s.slot(ctx)
	if err != nil {
		return photo{}, nil, "", err
	}
	defer release()
	p, codes, failure := decode(ctx, body)
	return p, codes, failure, nil
}

// scan decodes body, the photo key, and writes its row with the photo's hold.
func (s *Scans) scan(ctx context.Context, key string, body []byte, in Input) (Scan, error) {
	p, codes, failure, err := s.decoded(ctx, body)
	if err != nil {
		return Scan{}, err
	}
	raw, _ := json.Marshal(nonNil(codes))
	var id string
	err = pgx.BeginFunc(ctx, s.Pool, func(tx pgx.Tx) error {
		var lat, lon, accuracy *float64
		var located *time.Time
		if in.Location != nil {
			lat, lon, accuracy = &in.Location.Lat, &in.Location.Lon, in.Location.AccuracyM
			if in.Location.At != nil {
				at := time.Time(*in.Location.At)
				located = &at
			}
		}
		if err := tx.QueryRow(ctx, `INSERT INTO code_scans
			(actor, source, image_key, media_type, width, height, codes, error, lat, lon, accuracy_m, located_at)
			VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12) RETURNING id::text`,
			in.Actor, in.Source, key, p.MediaType, positive(p.Width), positive(p.Height), raw, nullable(failure),
			lat, lon, accuracy, located).Scan(&id); err != nil {
			return err
		}
		_, err := tx.Exec(ctx, `INSERT INTO blob_refs (key, holder_kind, holder_id) VALUES ($1, $2, $3)
			ON CONFLICT DO NOTHING`, key, Holder, id)
		return err
	})
	if err != nil {
		return Scan{}, err
	}
	return s.find(ctx, id, true, nil)
}

func nonNil(codes []Code) []Code {
	if codes == nil {
		return []Code{}
	}
	return codes
}

func positive(n int) *int {
	if n <= 0 {
		return nil
	}
	return &n
}

func nullable(s string) *string {
	if s == "" {
		return nil
	}
	return &s
}

const columns = `id::text, at, source, actor, image_key, media_type, width, height, codes, error,
	lat, lon, accuracy_m, located_at, decoded_at`

// scanRow reads one row of columns.
func scanRow(row pgx.Row) (Scan, []Code, error) {
	var sc Scan
	var at, decodedAt time.Time
	var raw []byte
	var lat, lon, accuracy *float64
	var located *time.Time
	if err := row.Scan(&sc.ID, &at, &sc.Source, &sc.Actor, &sc.Image.Key, &sc.Image.MediaType, &sc.Width,
		&sc.Height, &raw, &sc.Error, &lat, &lon, &accuracy, &located, &decodedAt); err != nil {
		return Scan{}, nil, err
	}
	sc.At, sc.DecodedAt = crud.Time(at), crud.Time(decodedAt)
	if lat != nil && lon != nil {
		sc.Location = &Location{Lat: *lat, Lon: *lon, AccuracyM: accuracy}
		if located != nil {
			t := crud.Time(*located)
			sc.Location.At = &t
		}
	}
	var codes []Code
	if err := json.Unmarshal(raw, &codes); err != nil {
		return Scan{}, nil, fmt.Errorf("scan: a row's codes: %w", err)
	}
	return sc, codes, nil
}

// withResolved fills sc's codes, each resolved now: an archive or a new spool since the scan
// shows as it is.
func (s *Scans) withResolved(ctx context.Context, sc Scan, codes []Code) (Scan, error) {
	sc.Codes = make([]Found, 0, len(codes))
	for _, c := range codes {
		r, err := resolve(ctx, s.Pool, c.Text)
		if err != nil {
			return Scan{}, err
		}
		sc.Codes = append(sc.Codes, Found{Code: c, Resolved: r})
	}
	return sc, nil
}

// whose is the scans a caller may see. A service sees all. A person or a token sees only its
// own: a scan carries its photo and its GPS fix. Any other caller (anonymous, as every caller
// is while auth is off) sees only scans that no one made: actor NULL, what such a caller's own
// scans record. all is true for every scan; me is the actor to match otherwise, nil for NULL.
func whose(ctx context.Context) (all bool, me *string) {
	c := auth.CallerOf(ctx)
	switch c.Kind {
	case auth.KindService:
		return true, nil
	case auth.KindPerson, auth.KindToken:
		if c.Principal != "" {
			return false, &c.Principal
		}
	}
	return false, nil
}

// one is the scan id as the caller on ctx may see it (whose), or the 404.
func (s *Scans) one(ctx context.Context, id string) (Scan, error) {
	everyone, me := whose(ctx)
	return s.find(ctx, id, everyone, me)
}

// find is the scan id, for everyone or for actor me only, or the 404. A scan just recorded
// is read back with everyone: its own writer reads it, a Go caller in core too, whose ctx
// names no caller (Record).
func (s *Scans) find(ctx context.Context, id string, everyone bool, me *string) (Scan, error) {
	if !crud.IsUUID(id) {
		return Scan{}, notFound()
	}
	sc, codes, err := scanRow(s.Pool.QueryRow(ctx, "SELECT "+columns+
		" FROM code_scans WHERE id = $1 AND ($2 OR actor IS NOT DISTINCT FROM $3)", id, everyone, me))
	if errors.Is(err, pgx.ErrNoRows) {
		return Scan{}, notFound()
	}
	if err != nil {
		return Scan{}, err
	}
	return s.withResolved(ctx, sc, codes)
}

func (s *Scans) upload(r *http.Request) (any, error) {
	body, err := io.ReadAll(r.Body)
	if err != nil {
		return nil, err
	}
	if len(body) == 0 {
		return nil, refused(http.StatusUnprocessableEntity, "image_not_supported", "the body is empty: send the photo's bytes")
	}
	return s.Store(r.Context(), body)
}

// input reads a body's source, fallback when it names none, and its location.
func input(r *http.Request, body *crud.Body, fallback string) Input {
	in := Input{Actor: actorOf(r.Context()), Source: body.ChoiceOr("source", fallback, Sources...)}
	if place := body.Object("location", "lat", "lon", "accuracy_m", "at"); place != nil {
		lat := place.OptFloat("lat", -90, 90)
		lon := place.OptFloat("lon", -180, 180)
		accuracy := place.OptFloat("accuracy_m", 0, 1_000_000)
		at := place.Datetime("at", true)
		if lat == nil {
			place.Need("lat")
		}
		if lon == nil {
			place.Need("lon")
		}
		place.Done()
		if lat != nil && lon != nil {
			in.Location = &Location{Lat: *lat, Lon: *lon, AccuracyM: accuracy}
			if at != nil {
				if t, err := parseTime(*at); err == nil {
					ct := crud.Time(t)
					in.Location.At = &ct
				}
			}
		}
	}
	return in
}

// parseTime reads a Datetime the body checked: RFC 3339, or with a space or no zone (UTC).
func parseTime(s string) (time.Time, error) {
	s = strings.Replace(s, " ", "T", 1)
	for _, layout := range []string{time.RFC3339Nano, "2006-01-02T15:04:05.999999999", "2006-01-02T15:04",
		"2006-01-02T15:04Z07:00", "2006-01-02T15:04:05.999999999Z0700"} {
		if t, err := time.Parse(layout, s); err == nil {
			return t.UTC(), nil
		}
	}
	return time.Time{}, errors.New("scan: not a time")
}

// codes decodes a stored photo: {key}, or {read_id}, a paired device's photo read (#8244),
// whose source is "app" unless the body names one. A read of a device the caller does not own
// is a 404, as a read that is not there.
func (s *Scans) codes(r *http.Request) (any, error) {
	body, err := crud.ReadBody(r, "key", "read_id", "source", "location")
	if err != nil {
		return nil, err
	}
	var key, readID string
	source := "shutter"
	if body.Present("read_id") {
		source = "app"
		readID = body.Text("read_id", 1, 64)
		if body.Present("key") {
			body.Fail([]string{"key"}, "read_id comes in place of key")
		}
	} else {
		key = body.Text("key", 1, 128)
	}
	in := input(r, body, source)
	if err := body.Err(); err != nil {
		return nil, err
	}
	if readID != "" {
		read, err := s.Devices.OwnRead(r.Context(), readID)
		if err != nil {
			return nil, err
		}
		if read.Kind != "photo" {
			return nil, refused(http.StatusUnprocessableEntity, "read_not_a_photo", "this device read is a tag, not a photo")
		}
		key = read.BlobKey
	}
	return s.Decode(r.Context(), key, in)
}

func (s *Scans) list(r *http.Request) (any, error) {
	limit, given, err := crud.QueryInt(r, "limit", 1, listMax)
	if err != nil {
		return nil, err
	}
	if !given {
		limit = 50
	}
	before := r.URL.Query().Get("before")
	everyone, me := whose(r.Context())
	var rows pgx.Rows
	if before != "" {
		if !crud.IsUUID(before) {
			return nil, notFound()
		}
		rows, err = s.Pool.Query(r.Context(), "SELECT "+columns+` FROM code_scans
			WHERE (at, id) < (SELECT at, id FROM code_scans WHERE id = $1) AND ($3 OR actor IS NOT DISTINCT FROM $4)
			ORDER BY at DESC, id DESC LIMIT $2`, before, limit, everyone, me)
	} else {
		rows, err = s.Pool.Query(r.Context(), "SELECT "+columns+
			" FROM code_scans WHERE ($2 OR actor IS NOT DISTINCT FROM $3) ORDER BY at DESC, id DESC LIMIT $1", limit, everyone, me)
	}
	if err != nil {
		return nil, err
	}
	type listed struct {
		scan  Scan
		codes []Code
	}
	var all []listed
	for rows.Next() {
		sc, codes, err := scanRow(rows)
		if err != nil {
			rows.Close()
			return nil, err
		}
		all = append(all, listed{sc, codes})
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	scans := make([]Scan, 0, len(all))
	for _, one := range all {
		sc, err := s.withResolved(r.Context(), one.scan, one.codes)
		if err != nil {
			return nil, err
		}
		scans = append(scans, sc)
	}
	return map[string]any{"scans": scans}, nil
}

func (s *Scans) get(r *http.Request) (any, error) {
	return s.one(r.Context(), r.PathValue("id"))
}

func (s *Scans) image(r *http.Request) (any, error) {
	sc, err := s.one(r.Context(), r.PathValue("id"))
	if err != nil {
		return nil, err
	}
	body, err := s.read(r.Context(), sc.Image.Key)
	if err != nil {
		return nil, err
	}
	mediaType := sc.Image.MediaType
	headers := map[string]string{"X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=86400, immutable"}
	if !strings.HasPrefix(mediaType, "image/") {
		mediaType = "application/octet-stream"
		headers["Content-Disposition"] = "attachment"
	}
	return &crud.Reply{Status: http.StatusOK, MediaType: mediaType, Headers: headers, Body: body}, nil
}

// decodeAgain decodes a scan's photo again and keeps what it finds now.
func (s *Scans) decodeAgain(r *http.Request) (any, error) {
	ctx := r.Context()
	sc, err := s.one(ctx, r.PathValue("id"))
	if err != nil {
		return nil, err
	}
	body, err := s.read(ctx, sc.Image.Key)
	if err != nil {
		return nil, err
	}
	p, codes, failure, err := s.decoded(ctx, body)
	if err != nil {
		return nil, err
	}
	raw, _ := json.Marshal(nonNil(codes))
	tag, err := s.Pool.Exec(ctx, `UPDATE code_scans SET codes = $2, error = $3, media_type = $4, width = $5,
		height = $6, decoded_at = now() WHERE id = $1`, sc.ID, raw, nullable(failure), p.MediaType,
		positive(p.Width), positive(p.Height))
	if err != nil {
		return nil, err
	}
	if tag.RowsAffected() == 0 {
		return nil, notFound()
	}
	return s.one(ctx, sc.ID)
}

// remove deletes a scan and its photo's hold: the sweep takes the photo once no other scan
// holds it and its grace has passed.
func (s *Scans) remove(r *http.Request) (any, error) {
	ctx := r.Context()
	id := r.PathValue("id")
	if !crud.IsUUID(id) {
		return nil, notFound()
	}
	everyone, me := whose(ctx)
	var gone bool
	err := pgx.BeginFunc(ctx, s.Pool, func(tx pgx.Tx) error {
		tag, err := tx.Exec(ctx, "DELETE FROM code_scans WHERE id = $1 AND ($2 OR actor IS NOT DISTINCT FROM $3)", id, everyone, me)
		if err != nil {
			return err
		}
		gone = tag.RowsAffected() == 1
		if !gone {
			return nil
		}
		_, err = tx.Exec(ctx, "DELETE FROM blob_refs WHERE holder_kind = $1 AND holder_id = $2", Holder, id)
		return err
	})
	if err != nil {
		return nil, err
	}
	if !gone {
		return nil, notFound()
	}
	return &crud.Reply{Status: http.StatusNoContent, MediaType: "text/plain", Body: []byte{}}, nil
}
