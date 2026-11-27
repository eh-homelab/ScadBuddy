// Package workspace is the routes of backend/scadbuddy/api/crud_agent_workspace.py, over
// agent_data/workspace.py, statement for statement. Areas workspace:read and workspace:sql
// are the routes that need neither the blob store nor the KEK: the metadata reads, a
// delete, the sweep and the reconcile. Area workspace (Routes, bytes.go) is every route:
// those, and the status, a file's bytes, a write, a fork's copy and the move to blobs,
// sealed as Python seals them (seal.go). The KEK rotation's rewrap stays Python's.
//
// A refusal is a problem whose reason is the agent's WorkspaceErrorCode.
package workspace

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"math"
	"net/http"
	"regexp"
	"slices"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/seal"
)

const (
	pathMax       = 512
	workspaceMax  = 700
	sessionsMax   = 500
	eventsMax     = 4
	eventBytesMax = 4096
	eventType     = "workspace.file"
	ttlMs         = 24 * 60 * 60 * 1000
	reconcileLock = "scadbuddy:ai_session_files:reconcile"
	holderKind    = "ai_session_file"
	bodyMax       = 8 * 1024 * 1024
	info          = "path, media_type, size, sha256, source, created_at, updated_at"
)

// The patterns end as Python's re `$` does: before a last newline too.
const segment = `[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}`

var (
	pathPattern      = regexp.MustCompile(`^` + segment + `(/` + segment + `){0,3}\n?\z`)
	uuidText         = `[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}`
	workspacePattern = regexp.MustCompile(`^(session:` + uuidText +
		`|principal:(browser|bearer|oidc|anonymous|flow):[^\x00-\x1f\x7f]{1,600})\n?\z`)
	sessionID = regexp.MustCompile(`^` + uuidText + `$`)
	sha256Hex = regexp.MustCompile(`^[0-9a-f]{64}$`)
	dotSlash  = regexp.MustCompile(`^(\./)+`)
)

// Workspace is the area's routes.
type Workspace struct{ Pool *pgxpool.Pool }

// Areas are the routes of areas workspace:read and workspace:sql.
func Areas(pool *pgxpool.Pool, _ *seal.Keys) map[string][]crud.Route {
	ws := &Workspace{Pool: pool}
	return map[string][]crud.Route{
		"workspace:read": ws.Reads(),
		"workspace:sql":  append(ws.Reads(), ws.Writes()...),
	}
}

// Reads are the metadata reads.
func (ws *Workspace) Reads() []crud.Route {
	return []crud.Route{
		{Method: "GET", Path: "/workspace/files", Handle: ws.files},
		{Method: "GET", Path: "/workspace/file", Handle: ws.file},
		{Method: "GET", Path: "/workspace/usage", Handle: ws.usage},
		{Method: "POST", Path: "/workspace/session-files", Handle: ws.sessionFiles, MaxBody: bodyMax},
	}
}

// Writes are the writes that need no blob store and no KEK.
func (ws *Workspace) Writes() []crud.Route {
	return []crud.Route{
		{Method: "POST", Path: "/workspace/file-deletes", Handle: ws.delete, MaxBody: bodyMax},
		{Method: "POST", Path: "/workspace/sweeps", Handle: ws.sweep, MaxBody: bodyMax},
		{Method: "POST", Path: "/workspace/reconciles", Handle: ws.reconcile, MaxBody: bodyMax},
	}
}

var statuses = map[string]int{
	"invalid_path": http.StatusUnprocessableEntity,
	"not_found":    http.StatusNotFound,
	"conflict":     http.StatusConflict,
	"too_large":    http.StatusRequestEntityTooLarge,
	"quota":        http.StatusConflict,
	"unavailable":  http.StatusServiceUnavailable,
}

func refused(code, message string) error { return httpx.WithReason(statuses[code], message, code) }

func unprocessable(detail string) error {
	return &httpx.Problem{Status: http.StatusUnprocessableEntity, Detail: detail}
}

func checkWorkspace(workspace string) error {
	if utf8.RuneCountInString(workspace) > workspaceMax || !workspacePattern.MatchString(workspace) {
		return refused("invalid_path", "not a workspace")
	}
	return nil
}

// checkPath is path as stored (a leading ./ dropped), or invalid_path.
func checkPath(path string) (string, error) {
	clean := dotSlash.ReplaceAllString(path, "")
	if clean == "" || utf8.RuneCountInString(clean) > pathMax || !pathPattern.MatchString(clean) {
		shown := []rune(path)
		if len(shown) > 200 {
			shown = shown[:200]
		}
		return "", refused("invalid_path", pyJSON(string(shown))+` is not a workspace path: use up`+
			` to 4 "/"-separated parts, each starting with a letter, digit or "_" and holding`+
			` only letters, digits, "_", "." and "-" (e.g. "downloads/part.3mf")`)
	}
	return clean, nil
}

// pyJSON is s as Python's json.dumps writes it: every character outside ' '..'~' escaped.
func pyJSON(s string) string {
	var b strings.Builder
	b.WriteByte('"')
	for _, r := range s {
		switch {
		case r == '"' || r == '\\':
			b.WriteByte('\\')
			b.WriteRune(r)
		case r == '\n':
			b.WriteString(`\n`)
		case r == '\r':
			b.WriteString(`\r`)
		case r == '\t':
			b.WriteString(`\t`)
		case r == '\b':
			b.WriteString(`\b`)
		case r == '\f':
			b.WriteString(`\f`)
		case r >= ' ' && r <= '~':
			b.WriteRune(r)
		case r > 0xffff:
			r -= 0x10000
			fmt.Fprintf(&b, `\u%04x\u%04x`, 0xd800+(r>>10), 0xdc00+(r&0x3ff))
		default:
			fmt.Fprintf(&b, `\u%04x`, r)
		}
	}
	b.WriteByte('"')
	return b.String()
}

// queryWorkspace is the workspace query parameter, checked.
func queryWorkspace(r *http.Request) (string, error) {
	workspace, err := crud.QueryText(r, "workspace", workspaceMax)
	if err != nil {
		return "", err
	}
	return workspace, checkWorkspace(workspace)
}

type fileInfo struct {
	Path      string          `json:"path"`
	MediaType string          `json:"media_type"`
	Size      int64           `json:"size"`
	Sha256    string          `json:"sha256"`
	Source    json.RawMessage `json:"source"`
	CreatedAt crud.Time       `json:"created_at"`
	UpdatedAt crud.Time       `json:"updated_at"`
}

func scanInfo(row pgx.CollectableRow) (fileInfo, error) {
	var f fileInfo
	var created, updated time.Time
	err := row.Scan(&f.Path, &f.MediaType, &f.Size, &f.Sha256, &f.Source, &created, &updated)
	f.CreatedAt, f.UpdatedAt = crud.Time(created), crud.Time(updated)
	return f, err
}

func (ws *Workspace) files(r *http.Request, _ crud.Params) (any, error) {
	workspace, err := crud.QueryText(r, "workspace", workspaceMax)
	if err != nil {
		return nil, err
	}
	prefix, err := crud.QueryOptText(r, "prefix", 0, pathMax+64)
	if err != nil {
		return nil, err
	}
	if err := checkWorkspace(workspace); err != nil {
		return nil, err
	}
	var under *string
	if prefix != nil {
		clean, err := checkPath(strings.TrimRight(*prefix, "/"))
		if err != nil {
			return nil, err
		}
		clean += "/"
		under = &clean
	}
	rows, err := ws.Pool.Query(r.Context(), `SELECT `+info+` FROM ai_session_files WHERE workspace = $1
		AND ($2::text IS NULL OR starts_with(path, $2)) ORDER BY path`, workspace, under)
	if err != nil {
		return nil, err
	}
	found, err := pgx.CollectRows(rows, scanInfo)
	if err != nil {
		return nil, err
	}
	return struct {
		Files []fileInfo `json:"files"`
	}{append([]fileInfo{}, found...)}, nil
}

func (ws *Workspace) file(r *http.Request, _ crud.Params) (any, error) {
	workspace, err := crud.QueryText(r, "workspace", workspaceMax)
	if err != nil {
		return nil, err
	}
	path, err := crud.QueryText(r, "path", pathMax+64)
	if err != nil {
		return nil, err
	}
	if err := checkWorkspace(workspace); err != nil {
		return nil, err
	}
	if path, err = checkPath(path); err != nil {
		return nil, err
	}
	rows, err := ws.Pool.Query(r.Context(),
		`SELECT `+info+` FROM ai_session_files WHERE workspace = $1 AND path = $2`, workspace, path)
	if err != nil {
		return nil, err
	}
	found, err := pgx.CollectExactlyOneRow(rows, scanInfo)
	out := struct {
		File *fileInfo `json:"file"`
	}{}
	if errors.Is(err, pgx.ErrNoRows) {
		return out, nil
	}
	out.File = &found
	return out, err
}

func (ws *Workspace) usage(r *http.Request, _ crud.Params) (any, error) {
	workspace, err := queryWorkspace(r)
	if err != nil {
		return nil, err
	}
	var out struct {
		Files int64 `json:"files"`
		Bytes int64 `json:"bytes"`
	}
	err = ws.Pool.QueryRow(r.Context(), `SELECT count(*)::int AS files, coalesce(sum(size), 0)::bigint
		FROM ai_session_files WHERE workspace = $1`, workspace).Scan(&out.Files, &out.Bytes)
	return out, err
}

type sessionFile struct {
	fileInfo
	Sealed bool `json:"sealed"`
}

func (ws *Workspace) sessionFiles(r *http.Request, _ crud.Params) (any, error) {
	body, err := crud.ReadBody(r, "session_ids")
	if err != nil {
		return nil, err
	}
	ids := body.Matches("session_ids", 0, sessionsMax, sessionID)
	if err := body.Err(); err != nil {
		return nil, err
	}
	out := map[string][]sessionFile{}
	if len(ids) > 0 {
		workspaces := make([]string, len(ids))
		for i, id := range ids {
			workspaces[i] = "session:" + id
		}
		rows, err := ws.Pool.Query(r.Context(), `SELECT workspace, `+info+`, blob_key IS NOT NULL AS sealed
			FROM ai_session_files WHERE workspace = ANY($1::text[]) ORDER BY workspace, path`, workspaces)
		if err != nil {
			return nil, err
		}
		defer rows.Close()
		for rows.Next() {
			var workspace string
			var f sessionFile
			var created, updated time.Time
			if err := rows.Scan(&workspace, &f.Path, &f.MediaType, &f.Size, &f.Sha256, &f.Source,
				&created, &updated, &f.Sealed); err != nil {
				return nil, err
			}
			f.CreatedAt, f.UpdatedAt = crud.Time(created), crud.Time(updated)
			id := strings.TrimPrefix(workspace, "session:")
			out[id] = append(out[id], f)
		}
		if err := rows.Err(); err != nil {
			return nil, err
		}
	}
	return struct {
		Sessions map[string][]sessionFile `json:"sessions"`
	}{out}, nil
}

// checkEvents checks that each event is a workspace.file event of the workspace's own
// session; only a session's workspace logs any.
func checkEvents(workspace string, events []string) error {
	if len(events) == 0 {
		return nil
	}
	session, ok := strings.CutPrefix(workspace, "session:")
	if !ok {
		return unprocessable("events: only a session's workspace logs events")
	}
	for _, text := range events {
		if len(text) > eventBytesMax {
			return unprocessable(fmt.Sprintf("events: an event is at most %d bytes", eventBytesMax))
		}
		var parsed map[string]any
		if err := json.Unmarshal([]byte(text), &parsed); err != nil {
			var other any
			if json.Unmarshal([]byte(text), &other) != nil {
				return unprocessable("events: " + err.Error())
			}
		}
		if parsed == nil || parsed["type"] != eventType || parsed["sessionId"] != session {
			return unprocessable(fmt.Sprintf("events: each event is a %s event of session %s", eventType, session))
		}
	}
	return nil
}

// appendEvents is eventLog.ts append in the caller's transaction: the seqs, oldest first.
func appendEvents(ctx context.Context, tx pgx.Tx, session string, events []string) ([]int64, error) {
	rows, err := tx.Query(ctx,
		`WITH s AS (UPDATE ai_sessions SET event_seq = event_seq + $1, updated_at = now()
		 WHERE id = $2::text::uuid RETURNING event_seq - $1 AS base)
		 INSERT INTO ai_session_events (session_id, seq, event)
		 SELECT $2::text::uuid, s.base + e.ord, e.event
		 FROM s, unnest($3::text[]) WITH ORDINALITY AS e(event, ord) RETURNING seq`,
		int64(len(events)), session, events)
	if err != nil {
		return nil, err
	}
	seqs, err := pgx.CollectRows(rows, pgx.RowTo[int64])
	if err != nil {
		return nil, err
	}
	if len(seqs) != len(events) {
		return nil, refused("not_found", fmt.Sprintf("session %s does not exist", session))
	}
	slices.Sort(seqs)
	return seqs, nil
}

func release(ctx context.Context, tx pgx.Tx, blobKey, workspace, path string) error {
	_, err := tx.Exec(ctx, `DELETE FROM blob_refs WHERE key = $1 AND holder_kind = $2 AND holder_id = $3`,
		blobKey, holderKind, workspace+"/"+path)
	return err
}

// dropUnnamedBytes deletes the ai_session_file_bytes rows no file names, of shas or (nil)
// all; a row a write or a fork holds FOR KEY SHARE is skipped.
func dropUnnamedBytes(ctx context.Context, pool *pgxpool.Pool, shas []string) error {
	if shas != nil && len(shas) == 0 {
		return nil
	}
	list := shas
	if list == nil {
		list = []string{}
	}
	_, err := pool.Exec(ctx, `DELETE FROM ai_session_file_bytes WHERE sha256 IN (
		 SELECT b.sha256 FROM ai_session_file_bytes b
		 WHERE ($1 OR b.sha256 = ANY($2::text[]))
		 AND NOT EXISTS (SELECT 1 FROM ai_session_files f WHERE f.bytes_sha256 = b.sha256)
		 FOR UPDATE SKIP LOCKED)`, shas == nil, list)
	return err
}

type deleted struct {
	Deleted bool    `json:"deleted"`
	Seqs    []int64 `json:"seqs"`
}

// delete deletes a file, its blob's hold and logs its events in one transaction; conflict
// when expected_sha256 does not hold.
func (ws *Workspace) delete(r *http.Request, _ crud.Params) (any, error) {
	body, err := crud.ReadBody(r, "workspace", "path", "expected_sha256", "events")
	if err != nil {
		return nil, err
	}
	workspace := body.Text("workspace", 1, workspaceMax)
	path := body.Text("path", 1, pathMax+64)
	expected := body.OptText("expected_sha256", 0, math.MaxInt, sha256Hex)
	events := body.Strings("events", true, 0, eventsMax, 0, eventBytesMax)
	if err := body.Err(); err != nil {
		return nil, err
	}
	if err := checkWorkspace(workspace); err != nil {
		return nil, err
	}
	if path, err = checkPath(path); err != nil {
		return nil, err
	}
	if err := checkEvents(workspace, events); err != nil {
		return nil, err
	}
	session, isSession := strings.CutPrefix(workspace, "session:")
	out := deleted{Seqs: []int64{}}
	var named *string
	ctx := r.Context()
	err = pgx.BeginFunc(ctx, ws.Pool, func(tx pgx.Tx) error {
		if len(events) > 0 && isSession {
			// The session's row first, as a session delete takes it before it cascades.
			if _, err := tx.Exec(ctx, `SELECT 1 FROM ai_sessions WHERE id = $1::text::uuid
				FOR NO KEY UPDATE`, session); err != nil {
				return err
			}
		}
		var sha string
		err := tx.QueryRow(ctx, `SELECT sha256 FROM ai_session_files WHERE workspace = $1 AND path = $2
			FOR UPDATE`, workspace, path).Scan(&sha)
		if errors.Is(err, pgx.ErrNoRows) {
			return nil
		}
		if err != nil {
			return err
		}
		if expected != nil && sha != *expected {
			return refused("conflict", fmt.Sprintf(
				"%s changed since it was read: its sha256 is %s; nothing was deleted", path, sha))
		}
		var blobKey *string
		if err := tx.QueryRow(ctx, `DELETE FROM ai_session_files WHERE workspace = $1 AND path = $2
			RETURNING blob_key, bytes_sha256`, workspace, path).Scan(&blobKey, &named); err != nil {
			return err
		}
		if blobKey != nil {
			if err := release(ctx, tx, *blobKey, workspace, path); err != nil {
				return err
			}
		}
		if len(events) > 0 && isSession {
			if out.Seqs, err = appendEvents(ctx, tx, session, events); err != nil {
				return err
			}
		}
		out.Deleted = true
		return nil
	})
	if err != nil {
		return nil, err
	}
	if named != nil {
		dropBestEffort(ctx, ws.Pool, []string{*named})
	}
	return out, nil
}

// dropBestEffort deletes shas' bytes once no file names them; the sweep deletes what fails.
func dropBestEffort(ctx context.Context, pool *pgxpool.Pool, shas []string) {
	if err := dropUnnamedBytes(ctx, pool, shas); err != nil {
		slog.WarnContext(ctx, "workspace files: unnamed bytes not deleted; the sweep deletes them")
	}
}

// sweep deletes principals' files last written more than ttl_ms ago with their blobs'
// holds, then every bytes row no file names.
func (ws *Workspace) sweep(r *http.Request, _ crud.Params) (any, error) {
	body, err := crud.ReadBody(r, "ttl_ms")
	if err != nil {
		return nil, err
	}
	fallback := int64(ttlMs)
	ttl := body.IntIn("ttl_ms", 60_000, 365*86_400_000, &fallback)
	if err := body.Err(); err != nil {
		return nil, err
	}
	ctx := r.Context()
	swept := 0
	err = pgx.BeginFunc(ctx, ws.Pool, func(tx pgx.Tx) error {
		rows, err := tx.Query(ctx, `DELETE FROM ai_session_files WHERE session_id IS NULL
			AND updated_at < now() - ($1 * interval '1 millisecond')
			RETURNING workspace, path, blob_key`, ttl)
		if err != nil {
			return err
		}
		type gone struct {
			Workspace, Path string
			BlobKey         *string
		}
		all, err := pgx.CollectRows(rows, pgx.RowToStructByPos[gone])
		if err != nil {
			return err
		}
		for _, g := range all {
			if g.BlobKey != nil {
				if err := release(ctx, tx, *g.BlobKey, g.Workspace, g.Path); err != nil {
					return err
				}
			}
		}
		swept = len(all)
		return nil
	})
	if err != nil {
		return nil, err
	}
	if err := dropUnnamedBytes(ctx, ws.Pool, nil); err != nil {
		return nil, err
	}
	return struct {
		Swept int `json:"swept"`
	}{swept}, nil
}

type reconciled struct {
	Released int64   `json:"released"`
	Held     int64   `json:"held"`
	Skipped  *string `json:"skipped"`
}

// reconcile makes the holds match the rows in one transaction; skipped held, doing nothing,
// while another reconcile holds the lock.
func (ws *Workspace) reconcile(r *http.Request, _ crud.Params) (any, error) {
	body, err := crud.ReadBody(r, "grace_seconds")
	if err != nil {
		return nil, err
	}
	fallback := int64(3600)
	grace := body.IntIn("grace_seconds", 0, 31_536_000, &fallback)
	if err := body.Err(); err != nil {
		return nil, err
	}
	ctx := r.Context()
	var out reconciled
	err = pgx.BeginFunc(ctx, ws.Pool, func(tx pgx.Tx) error {
		var locked bool
		if err := tx.QueryRow(ctx, `SELECT pg_try_advisory_xact_lock(hashtextextended($1, 0))`,
			reconcileLock).Scan(&locked); err != nil {
			return err
		}
		if !locked {
			skipped := "held"
			out.Skipped = &skipped
			return nil
		}
		tag, err := tx.Exec(ctx, `DELETE FROM blob_refs r WHERE r.holder_kind = $1
			AND r.created_at < now() - make_interval(secs => $2)
			AND NOT EXISTS (SELECT 1 FROM ai_session_files f WHERE f.blob_key = r.key
			AND f.workspace || '/' || f.path = r.holder_id)`, holderKind, float64(grace))
		if err != nil {
			return err
		}
		out.Released = tag.RowsAffected()
		tag, err = tx.Exec(ctx, `INSERT INTO blob_refs (key, holder_kind, holder_id)
			SELECT f.blob_key, $1, f.workspace || '/' || f.path FROM ai_session_files f
			WHERE f.blob_key IS NOT NULL AND NOT EXISTS (SELECT 1 FROM blob_refs r
			WHERE r.key = f.blob_key AND r.holder_kind = $1
			AND r.holder_id = f.workspace || '/' || f.path)
			ON CONFLICT (key, holder_kind, holder_id) DO NOTHING`, holderKind)
		out.Held = tag.RowsAffected()
		return err
	})
	if err != nil {
		return nil, err
	}
	return out, nil
}
