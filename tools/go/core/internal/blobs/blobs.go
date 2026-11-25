// Package blobs is the assistant's session images, attachments and saved HTTP bodies:
// backend/scadbuddy/api/crud_agent_blobs.py, statement for statement (areas session-blobs,
// attachments and http-bodies). Bytes travel as bytes: a write is multipart/form-data, a
// read answers the bytes as its body.
package blobs

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"regexp"
	"slices"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/seal"
)

const (
	imageBytesMax   = 5 * 1024 * 1024 / 4 * 3
	pdfBytesMax     = 8 * 1024 * 1024 / 4 * 3
	previewBytesMax = 48 * 1024
	imagesPerWrite  = 8
	writeBytesMax   = pdfBytesMax + (imagesPerWrite-1)*imageBytesMax
	toolUseIDMax    = 256
	attachmentTTLMs = 60 * 60 * 1000
	ownerRowsMax    = 24
	ownerBytesMax   = 40 * 1024 * 1024
	attachmentsMax  = 4
	httpBodyMax     = 20 * 1024 * 1024
	maxSaved        = 10
	urlMax          = 16 * 1024
	contentTypeMax  = 16 * 1024
	offsetMax       = 1<<31 - 2
)

var (
	blobName  = regexp.MustCompile(`^[0-9a-f]{64}\.(png|jpg|gif|webp|pdf)$`)
	extension = map[string]string{
		"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp",
		"application/pdf": "pdf",
	}
	blobTypes       = []string{"image/png", "image/jpeg", "image/gif", "image/webp", "application/pdf"}
	attachmentTypes = []string{"image/png", "image/jpeg", "image/gif", "image/webp"}
	previewTypes    = []string{"image/png", "image/jpeg", "image/webp"}
	ownerKinds      = []string{"browser", "bearer", "oidc", "anonymous", "flow"}
	// imageEventTypes are the events a fork takes that can name an image.
	imageEventTypes = []string{"tool.result", "user.turn"}
)

// Blobs is the three areas' routes.
type Blobs struct{ Pool *pgxpool.Pool }

// Areas are the routes of areas session-blobs, attachments and http-bodies, with their :read.
func Areas(pool *pgxpool.Pool, _ *seal.Keys) map[string][]crud.Route {
	b := &Blobs{Pool: pool}
	return map[string][]crud.Route{
		"session-blobs:read": b.SessionReads(),
		"session-blobs":      append(b.SessionReads(), b.SessionWrites()...),
		"attachments:read":   b.AttachmentReads(),
		"attachments":        append(b.AttachmentReads(), b.AttachmentWrites()...),
		"http-bodies:read":   b.BodyReads(),
		"http-bodies":        append(b.BodyReads(), b.BodyWrites()...),
	}
}

// SessionReads are area session-blobs' GET routes.
func (b *Blobs) SessionReads() []crud.Route {
	return []crud.Route{{Method: "GET", Path: "/session-blobs", Handle: b.sessionBlob}}
}

// SessionWrites are area session-blobs' POST routes.
func (b *Blobs) SessionWrites() []crud.Route {
	return []crud.Route{
		{Method: "POST", Path: "/session-blobs", Handle: b.sessionPut, MaxBody: MultipartBodyMax},
		{Method: "POST", Path: "/session-blobs/deletes", Handle: b.sessionDelete, MaxBody: TextBodyMax},
	}
}

// AttachmentReads are area attachments' GET routes.
func (b *Blobs) AttachmentReads() []crud.Route {
	return []crud.Route{
		{Method: "GET", Path: "/attachments/{attachment_id}/image", Handle: b.attachmentPart("data", "media_type")},
		{Method: "GET", Path: "/attachments/{attachment_id}/preview", Handle: b.attachmentPart("preview", "preview_media_type")},
	}
}

// AttachmentWrites are area attachments' POST routes.
func (b *Blobs) AttachmentWrites() []crud.Route {
	return []crud.Route{
		{Method: "POST", Path: "/attachments", Handle: b.attachmentPut, MaxBody: MultipartBodyMax,
			Status: http.StatusCreated},
		{Method: "POST", Path: "/attachments/resolve", Handle: b.attachmentsResolve, MaxBody: TextBodyMax},
		{Method: "POST", Path: "/attachments/deletes", Handle: b.attachmentsDelete, MaxBody: TextBodyMax},
		{Method: "POST", Path: "/attachments/sweeps", Handle: b.attachmentsSweep},
	}
}

// BodyReads are area http-bodies' GET routes.
func (b *Blobs) BodyReads() []crud.Route {
	return []crud.Route{
		{Method: "GET", Path: "/http-bodies/meta", Handle: b.bodyMeta},
		{Method: "GET", Path: "/http-bodies/bytes", Handle: b.bodyBytes},
	}
}

// BodyWrites are area http-bodies' POST routes.
func (b *Blobs) BodyWrites() []crud.Route {
	return []crud.Route{{Method: "POST", Path: "/http-bodies", Handle: b.bodySave, MaxBody: MultipartBodyMax}}
}

// sniff is the media type data's signature names (sessions/images.ts sniff).
func sniff(data []byte) string {
	switch {
	case bytes.HasPrefix(data, []byte("\x89PNG\r\n\x1a\n")):
		return "image/png"
	case bytes.HasPrefix(data, []byte("\xff\xd8\xff")):
		return "image/jpeg"
	case bytes.HasPrefix(data, []byte("GIF87a")), bytes.HasPrefix(data, []byte("GIF89a")):
		return "image/gif"
	case bytes.HasPrefix(data, []byte("RIFF")) && len(data) >= 12 && string(data[8:12]) == "WEBP":
		return "image/webp"
	case bytes.HasPrefix(data, []byte("%PDF-")):
		return "application/pdf"
	}
	return ""
}

func imageName(mediaType string, data []byte) string {
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:]) + "." + extension[mediaType]
}

func checkedImage(what, mediaType string, data []byte, most int) error {
	if mediaType == "application/pdf" {
		most = pdfBytesMax
	}
	switch {
	case len(data) == 0:
		return unprocessable(what + " is empty")
	case len(data) > most:
		return tooLarge(fmt.Sprintf("%s is larger than %d bytes", what, most))
	case sniff(data) != mediaType:
		return unprocessable(fmt.Sprintf("%s is not a %s image", what, mediaType))
	}
	return nil
}

// meta is a write's meta part as an object of names; done gives its first issue as the
// write's 422.
func meta(raw string, names ...string) (*crud.Body, error) {
	body, err := crud.ParseBody([]byte(raw), names...)
	if err != nil {
		return nil, unprocessable("meta: Invalid JSON")
	}
	return body, nil
}

func done(b *crud.Body) error {
	if msg := b.First(); msg != "" {
		return unprocessable("meta: " + msg)
	}
	return nil
}

func pgCode(err error) string {
	var pg *pgconn.PgError
	if errors.As(err, &pg) {
		return pg.Code
	}
	return ""
}

// utcMicros is at as the agent's blobs.ts writes it: microseconds, UTC, Z.
func utcMicros(at time.Time) string { return at.UTC().Format("2006-01-02T15:04:05.000000Z") }

type fence struct{ attempt, turnID string }

func readFence(b *crud.Body) *fence {
	f := b.Object("fence", "attempt", "turn_id")
	if f == nil {
		return nil
	}
	defer f.Done()
	return &fence{f.Text("attempt", 1, 500), f.Text("turn_id", 1, 500)}
}

// held is whether the session's row still names the fence's turn attempt, locked FOR SHARE.
func held(r *http.Request, tx pgx.Tx, session string, f *fence) (bool, error) {
	var one int
	err := tx.QueryRow(r.Context(),
		`SELECT 1 FROM ai_sessions WHERE id = $1::text::uuid AND turn_attempt = $2
		 AND durable_turn_id = $3::text::uuid FOR SHARE`, session, f.attempt, f.turnID).Scan(&one)
	if errors.Is(err, pgx.ErrNoRows) {
		return false, nil
	}
	return err == nil, err
}

type image struct {
	name, mediaType string
	data            []byte
}

type blobsStored struct {
	Stored  bool     `json:"stored"`
	Created []string `json:"created"`
	At      *string  `json:"at"`
}

func (b *Blobs) sessionPut(r *http.Request, _ crud.Params) (any, error) {
	raw, files, err := readMultipart(r, imagesPerWrite, writeBytesMax)
	if err != nil {
		return nil, err
	}
	body, err := meta(raw, "session_id", "images", "tool_use_id", "fence")
	if err != nil {
		return nil, err
	}
	session := body.PyUUID("session_id")
	var images []image
	metas, ok := body.Raw("images")
	if !ok {
		body.Fail([]string{"images"}, "Field required")
	} else {
		images = readImages(body, metas)
	}
	toolUseID := body.OptText("tool_use_id", 1, toolUseIDMax)
	fenced := readFence(body)
	if err := done(body); err != nil {
		return nil, err
	}
	if toolUseID != nil && fenced != nil {
		return nil, unprocessable("a write names a tool_use_id or a fence, not both")
	}
	want := map[string]bool{}
	for i := range images {
		want[fmt.Sprintf("image%d", i)] = true
	}
	same := len(want) == len(files)
	for k := range files {
		same = same && want[k]
	}
	if !same {
		return nil, unprocessable("each image named in meta is the part image<index>")
	}
	for i := range images {
		data := files[fmt.Sprintf("image%d", i)]
		what := fmt.Sprintf("images[%d]", i)
		if err := checkedImage(what, images[i].mediaType, data, imageBytesMax); err != nil {
			return nil, err
		}
		if imageName(images[i].mediaType, data) != images[i].name {
			return nil, unprocessable(what + ": a name is its bytes' sha256 and type")
		}
		images[i].data = data
	}
	out := blobsStored{Created: []string{}}
	err = pgx.BeginFunc(r.Context(), b.Pool, func(tx pgx.Tx) error {
		if fenced != nil {
			if ok, err := held(r, tx, session, fenced); err != nil || !ok {
				return err
			}
		}
		var at *time.Time
		for _, im := range images {
			var created time.Time
			var made bool
			err := tx.QueryRow(r.Context(),
				`INSERT INTO ai_session_blobs (session_id, name, media_type, data, created_at)
				 VALUES ($1::text::uuid, $2, $3, $4, clock_timestamp())
				 ON CONFLICT (session_id, name) DO UPDATE SET created_at = clock_timestamp()
				 RETURNING created_at, (xmax = 0) AS made`,
				session, im.name, im.mediaType, im.data).Scan(&created, &made)
			if err != nil {
				return err
			}
			if made && !slices.Contains(out.Created, im.name) {
				out.Created = append(out.Created, im.name)
			}
			if at == nil || created.After(*at) {
				at = &created
			}
		}
		if toolUseID != nil {
			if _, err := tx.Exec(r.Context(),
				`DELETE FROM ai_session_tool_images WHERE session_id = $1::text::uuid AND tool_use_id = $2`,
				session, *toolUseID); err != nil {
				return err
			}
			for ord, im := range images {
				if _, err := tx.Exec(r.Context(),
					`INSERT INTO ai_session_tool_images (session_id, tool_use_id, ord, name)
					 VALUES ($1::text::uuid, $2, $3, $4)`, session, *toolUseID, ord, im.name); err != nil {
					return err
				}
			}
		}
		out.Stored = true
		if at != nil {
			s := utcMicros(*at)
			out.At = &s
		}
		return nil
	})
	if pgCode(err) == "23503" {
		return nil, &httpx.Problem{Status: http.StatusNotFound, Detail: "no such session"}
	}
	if err != nil {
		return nil, err
	}
	if !out.Stored {
		out.Created = []string{}
	}
	return out, nil
}

func readImages(body *crud.Body, raw []byte) []image {
	var items []json.RawMessage
	if json.Unmarshal(raw, &items) != nil || items == nil {
		body.Fail([]string{"images"}, "Input should be a valid list")
		return nil
	}
	if len(items) > imagesPerWrite {
		body.Fail([]string{"images"}, fmt.Sprintf("List should have at most %d items after validation", imagesPerWrite))
		return nil
	}
	out := make([]image, len(items))
	for i, item := range items {
		one, err := crud.ParseBody(item, "name", "media_type")
		if err != nil {
			body.Fail([]string{"images", fmt.Sprint(i)}, "Input should be a valid dictionary or object to extract fields from")
			continue
		}
		out[i] = image{name: one.Match("name", blobName), mediaType: one.Enum("media_type", blobTypes...)}
		if msg := one.First(); msg != "" {
			body.Fail([]string{"images", fmt.Sprint(i)}, msg)
		}
	}
	return out
}
