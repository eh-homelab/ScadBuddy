package blobs

import (
	"crypto/rand"
	"errors"
	"fmt"
	"net/http"
	"slices"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
)

func notFound(detail string) error {
	return &httpx.Problem{Status: http.StatusNotFound, Detail: detail}
}

type blobsDeleted struct {
	Ran     bool     `json:"ran"`
	Deleted []string `json:"deleted"`
}

// sessionDelete deletes the named images a write made, unless a write stored them again
// since or a tool call names them (#6413).
func (b *Blobs) sessionDelete(r *http.Request, _ crud.Params) (any, error) {
	body, err := crud.ReadBody(r, "session_id", "names", "before", "fence")
	if err != nil {
		return nil, err
	}
	session := body.PyUUID("session_id")
	names := body.Matches("names", 1, imagesPerWrite, blobName)
	before := body.Time("before")
	fenced := readFence(body)
	if err := body.Err(); err != nil {
		return nil, err
	}
	out := blobsDeleted{Deleted: []string{}}
	err = pgx.BeginFunc(r.Context(), b.Pool, func(tx pgx.Tx) error {
		if fenced != nil {
			if ok, err := held(r, tx, session, fenced); err != nil || !ok {
				return err
			}
		}
		rows, err := tx.Query(r.Context(),
			`DELETE FROM ai_session_blobs b WHERE b.session_id = $1::text::uuid AND b.name = ANY($2)
			 AND b.created_at <= $3 AND NOT EXISTS (
			 SELECT 1 FROM ai_session_tool_images t
			 WHERE t.session_id = b.session_id AND t.name = b.name)
			 RETURNING b.name`, session, names, before)
		if err != nil {
			return err
		}
		deleted, err := pgx.CollectRows(rows, pgx.RowTo[string])
		if err != nil {
			return err
		}
		slices.Sort(deleted)
		out = blobsDeleted{Ran: true, Deleted: append([]string{}, deleted...)}
		return nil
	})
	if err != nil {
		return nil, err
	}
	return out, nil
}

// sessionBlob is the image, its media type the answer's. A fork's is its own, else an
// ancestor's that an event it reads through its chain names.
func (b *Blobs) sessionBlob(r *http.Request, _ crud.Params) (any, error) {
	session, err := crud.QueryUUID(r, "session_id")
	if err != nil {
		return nil, err
	}
	name, err := crud.QueryMatch(r, "name", blobName)
	if err != nil {
		return nil, err
	}
	var out crud.Bytes
	err = b.Pool.QueryRow(r.Context(),
		`WITH RECURSIVE chain (id, parent, cut, bound, depth) AS (
		 SELECT id, fork_parent_id, fork_event_cut, NULL::bigint, 0
		 FROM ai_sessions WHERE id = $1::text::uuid
		 UNION ALL
		 SELECT s.id, s.fork_parent_id, s.fork_event_cut, LEAST(c.bound, c.cut), c.depth + 1
		 FROM ai_sessions s JOIN chain c ON s.id = c.parent WHERE c.cut IS NOT NULL)
		 SELECT b.media_type, b.data FROM chain c
		 JOIN ai_session_blobs b ON b.session_id = c.id AND b.name = $2
		 WHERE c.depth = 0 OR EXISTS (
		 SELECT 1 FROM chain r JOIN ai_session_events e ON e.session_id = r.id
		 WHERE r.depth > 0 AND e.seq <= r.bound AND strpos(e.event, $2) > 0
		 AND e.event::jsonb->>'type' = ANY($3::text[]))
		 ORDER BY c.depth LIMIT 1`, session, name, imageEventTypes).Scan(&out.MediaType, &out.Body)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, notFound("no such image in the session")
	}
	if err != nil {
		return nil, err
	}
	return out, nil
}

type owner struct{ kind, id string }

func readOwner(b *crud.Body) owner {
	raw, ok := b.Raw("owner")
	if !ok || string(raw) == "null" {
		b.Fail([]string{"owner"}, "Field required")
		return owner{}
	}
	o := b.Object("owner", "kind", "id")
	defer o.Done()
	return owner{o.Enum("kind", ownerKinds...), o.Text("id", 1, 1000)}
}

type preview struct {
	Name      string `json:"name"`
	MediaType string `json:"media_type"`
}

type attachmentView struct {
	ID        string  `json:"id"`
	Name      string  `json:"name"`
	MediaType string  `json:"media_type"`
	Preview   preview `json:"preview"`
}

func newUUID() string {
	b := make([]byte, 16)
	_, _ = rand.Read(b)
	b[6] = b[6]&0x0f | 0x40
	b[8] = b[8]&0x3f | 0x80
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[:4], b[4:6], b[6:8], b[8:10], b[10:])
}

// attachmentPut stages image and preview for the owner: 409 quota past the owner's caps,
// the count and the insert under one per-owner lock.
func (b *Blobs) attachmentPut(r *http.Request, _ crud.Params) (any, error) {
	raw, files, err := readMultipart(r, 2, imageBytesMax+previewBytesMax)
	if err != nil {
		return nil, err
	}
	body, err := meta(raw, "owner", "media_type", "preview_media_type")
	if err != nil {
		return nil, err
	}
	who := readOwner(body)
	mediaType := body.Enum("media_type", attachmentTypes...)
	previewType := body.Enum("preview_media_type", previewTypes...)
	if err := done(body); err != nil {
		return nil, err
	}
	image, hasImage := files["image"]
	shown, hasPreview := files["preview"]
	if len(files) != 2 || !hasImage || !hasPreview {
		return nil, unprocessable("an attachment is the parts meta, image and preview")
	}
	if err := checkedImage("the image", mediaType, image, imageBytesMax); err != nil {
		return nil, err
	}
	if err := checkedImage("the preview", previewType, shown, previewBytesMax); err != nil {
		return nil, err
	}
	name, id := imageName(mediaType, image), newUUID()
	placed := false
	err = pgx.BeginFunc(r.Context(), b.Pool, func(tx pgx.Tx) error {
		ctx := r.Context()
		if _, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock(hashtextextended($1, 0))`,
			"scadbuddy:ai_attachments:"+who.kind+":"+who.id); err != nil {
			return err
		}
		if _, err := tx.Exec(ctx, `DELETE FROM ai_attachments WHERE owner_kind = $1 AND owner_id = $2
			AND expires_at <= now()`, who.kind, who.id); err != nil {
			return err
		}
		var n, total int64
		if err := tx.QueryRow(ctx, `SELECT count(*)::int AS n, coalesce(sum(octet_length(data)), 0)::bigint
			FROM ai_attachments WHERE owner_kind = $1 AND owner_id = $2`, who.kind, who.id).Scan(&n, &total); err != nil {
			return err
		}
		if n+1 > ownerRowsMax || total+int64(len(image)) > ownerBytesMax {
			return nil
		}
		_, err := tx.Exec(ctx, `INSERT INTO ai_attachments (id, owner_kind, owner_id, name, media_type, data,
			preview_media_type, preview, expires_at)
			VALUES ($1::text::uuid, $2, $3, $4, $5, $6, $7, $8, now() + $9::text::interval)`,
			id, who.kind, who.id, name, mediaType, image, previewType, shown,
			fmt.Sprintf("%d milliseconds", attachmentTTLMs))
		placed = err == nil
		return err
	})
	if err != nil {
		return nil, err
	}
	if !placed {
		return nil, httpx.WithReason(http.StatusConflict, fmt.Sprintf(
			"too many images waiting to be sent: at most %d images or %d bytes; send or remove"+
				" some, or wait for them to expire", ownerRowsMax, ownerBytesMax), "quota")
	}
	return attachmentView{
		ID: id, Name: name, MediaType: mediaType,
		Preview: preview{Name: imageName(previewType, shown), MediaType: previewType},
	}, nil
}

func readIDs(r *http.Request) (owner, []string, error) {
	body, err := crud.ReadBody(r, "owner", "ids")
	if err != nil {
		return owner{}, nil, err
	}
	who := readOwner(body)
	ids := body.PyUUIDs("ids", 1, attachmentsMax)
	return who, ids, body.Err()
}

type resolved struct {
	ID               string `json:"id"`
	Name             string `json:"name"`
	MediaType        string `json:"media_type"`
	PreviewMediaType string `json:"preview_media_type"`
}

func (b *Blobs) attachmentsResolve(r *http.Request, _ crud.Params) (any, error) {
	who, ids, err := readIDs(r)
	if err != nil {
		return nil, err
	}
	rows, err := b.Pool.Query(r.Context(),
		`SELECT id::text, name, media_type, preview_media_type FROM ai_attachments
		 WHERE id = ANY($1::text[]::uuid[]) AND owner_kind = $2 AND owner_id = $3 AND expires_at > now()`,
		ids, who.kind, who.id)
	if err != nil {
		return nil, err
	}
	found, err := pgx.CollectRows(rows, pgx.RowToStructByPos[resolved])
	if err != nil {
		return nil, err
	}
	return struct {
		Attachments []resolved `json:"attachments"`
	}{append([]resolved{}, found...)}, nil
}

func (b *Blobs) attachmentPart(column, media string) crud.Handler {
	return func(r *http.Request, p crud.Params) (any, error) {
		id, err := p.PyUUID("attachment_id")
		if err != nil {
			return nil, err
		}
		kind, err := crud.QueryEnum(r, "owner_kind", ownerKinds...)
		if err != nil {
			return nil, err
		}
		ownerID, err := crud.QueryText(r, "owner_id", 1000)
		if err != nil {
			return nil, err
		}
		var out crud.Bytes
		err = b.Pool.QueryRow(r.Context(),
			`SELECT `+media+`, `+column+` FROM ai_attachments
			 WHERE id = $1::text::uuid AND owner_kind = $2 AND owner_id = $3 AND expires_at > now()`,
			id, kind, ownerID).Scan(&out.MediaType, &out.Body)
		if errors.Is(err, pgx.ErrNoRows) {
			return nil, notFound("the attachment is unknown or has expired")
		}
		if err != nil {
			return nil, err
		}
		return out, nil
	}
}

type deleted struct {
	Deleted int64 `json:"deleted"`
}

func (b *Blobs) attachmentsDelete(r *http.Request, _ crud.Params) (any, error) {
	who, ids, err := readIDs(r)
	if err != nil {
		return nil, err
	}
	tag, err := b.Pool.Exec(r.Context(),
		`DELETE FROM ai_attachments WHERE id = ANY($1::text[]::uuid[]) AND owner_kind = $2 AND owner_id = $3`,
		ids, who.kind, who.id)
	if err != nil {
		return nil, err
	}
	return deleted{tag.RowsAffected()}, nil
}

func (b *Blobs) attachmentsSweep(r *http.Request, _ crud.Params) (any, error) {
	tag, err := b.Pool.Exec(r.Context(), `DELETE FROM ai_attachments WHERE expires_at <= now()`)
	if err != nil {
		return nil, err
	}
	return deleted{tag.RowsAffected()}, nil
}

// bodySave keeps a saved body (the part body) and prunes all but the session's newest
// maxSaved, in one transaction (httpBodies.ts save).
func (b *Blobs) bodySave(r *http.Request, _ crud.Params) (any, error) {
	raw, files, err := readMultipart(r, 1, httpBodyMax)
	if err != nil {
		return nil, err
	}
	body, err := meta(raw, "url", "content_type", "size_bytes", "complete", "session_id", "id")
	if err != nil {
		return nil, err
	}
	url := body.Text("url", 0, urlMax)
	contentType := body.NullText("content_type", contentTypeMax)
	size := body.IntIn("size_bytes", 0, 1<<63-1, nil)
	complete := body.Bool("complete")
	session := body.PyUUID("session_id")
	id := body.PyUUID("id")
	if err := done(body); err != nil {
		return nil, err
	}
	data, ok := files["body"]
	if len(files) != 1 || !ok {
		return nil, unprocessable("a saved body is the parts meta and body")
	}
	err = pgx.BeginFunc(r.Context(), b.Pool, func(tx pgx.Tx) error {
		if _, err := tx.Exec(r.Context(), `INSERT INTO ai_session_http_bodies
			(session_id, id, url, content_type, size_bytes, complete, data)
			VALUES ($1::text::uuid, $2::text::uuid, $3, $4, $5, $6, $7)`,
			session, id, url, contentType, size, complete, data); err != nil {
			return err
		}
		_, err := tx.Exec(r.Context(), `DELETE FROM ai_session_http_bodies WHERE session_id = $1::text::uuid
			AND id NOT IN (SELECT id FROM ai_session_http_bodies WHERE session_id = $1::text::uuid
			ORDER BY created_at DESC LIMIT $2)`, session, maxSaved)
		return err
	})
	switch pgCode(err) {
	case "23503":
		return nil, notFound("no such session")
	case "23505":
		return nil, &httpx.Problem{Status: http.StatusConflict, Detail: "that body is saved already"}
	}
	if err != nil {
		return nil, err
	}
	return struct {
		Stored bool `json:"stored"`
	}{true}, nil
}

func bodyKey(r *http.Request) (string, string, error) {
	session, err := crud.QueryUUID(r, "session_id")
	if err != nil {
		return "", "", err
	}
	id, err := crud.QueryUUID(r, "id")
	return session, id, err
}

type savedMeta struct {
	URL         string    `json:"url"`
	ContentType *string   `json:"content_type"`
	SizeBytes   int64     `json:"size_bytes"`
	Complete    bool      `json:"complete"`
	CreatedAt   crud.Time `json:"created_at"`
}

func (b *Blobs) bodyMeta(r *http.Request, _ crud.Params) (any, error) {
	session, id, err := bodyKey(r)
	if err != nil {
		return nil, err
	}
	var out savedMeta
	var created time.Time
	err = b.Pool.QueryRow(r.Context(),
		`SELECT url, content_type, size_bytes, complete, created_at
		 FROM ai_session_http_bodies WHERE session_id = $1::text::uuid AND id = $2::text::uuid`,
		session, id).Scan(&out.URL, &out.ContentType, &out.SizeBytes, &out.Complete, &created)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, notFound("no such saved body")
	}
	if err != nil {
		return nil, err
	}
	out.CreatedAt = crud.Time(created)
	return out, nil
}

// bodyBytes is up to length bytes from offset, fewer at the end, none past it.
func (b *Blobs) bodyBytes(r *http.Request, _ crud.Params) (any, error) {
	session, id, err := bodyKey(r)
	if err != nil {
		return nil, err
	}
	offset, _, err := crud.QueryInt(r, "offset", 0, offsetMax)
	if err != nil {
		return nil, err
	}
	length, given, err := crud.QueryInt(r, "length", 0, httpBodyMax)
	if err != nil {
		return nil, err
	}
	if !given {
		length = httpBodyMax
	}
	out := crud.Bytes{MediaType: "application/octet-stream"}
	// substring() is 1-based; past the end it is empty.
	err = b.Pool.QueryRow(r.Context(),
		`SELECT substring(data FROM $1::int4 FOR $2::int4) FROM ai_session_http_bodies
		 WHERE session_id = $3::text::uuid AND id = $4::text::uuid`,
		offset+1, length, session, id).Scan(&out.Body)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, notFound("no such saved body")
	}
	if err != nil {
		return nil, err
	}
	if out.Body == nil {
		out.Body = []byte{}
	}
	return out, nil
}
