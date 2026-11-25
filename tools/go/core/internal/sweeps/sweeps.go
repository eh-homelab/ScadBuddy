// Package sweeps is the agent's directory sweeps and a forget's own deletes:
// backend/scadbuddy/api/crud_agent_sweeps.py, statement for statement (areas session-sweeps
// and forget-rows).
package sweeps

import (
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"regexp"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
)

const (
	liveIDsMax = 1000
	// bodyBytesMax is the event-log routes' bound (crud_agent_events.py BODY_BYTES_MAX).
	bodyBytesMax = 32 * 1024 * 1024
)

var uuidPattern = regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`)

// Sweeps is the areas' routes.
type Sweeps struct{ Pool *pgxpool.Pool }

// Live is area session-sweeps' route, a read.
func (s *Sweeps) Live() []crud.Route {
	return []crud.Route{{Method: "POST", Path: "/sessions/live", Handle: s.live, MaxBody: bodyBytesMax}}
}

// ForgetRows is area forget-rows' route.
func (s *Sweeps) ForgetRows() []crud.Route {
	return []crud.Route{{Method: "POST", Path: "/sessions/{session_id}/forget-rows", Handle: s.forgetRows}}
}

func (s *Sweeps) live(r *http.Request, _ crud.Params) (any, error) {
	body, err := crud.ReadBody(r, "ids")
	if err != nil {
		return nil, err
	}
	ids := []string{}
	for _, id := range body.Get("ids").List(0, liveIDsMax) {
		ids = append(ids, id.String(0, 1<<30, uuidPattern))
	}
	if err := body.Err(); err != nil {
		return nil, err
	}
	live := []string{}
	if len(ids) > 0 {
		rows, err := s.Pool.Query(r.Context(),
			`SELECT id::text AS id FROM ai_sessions WHERE id = ANY($1::uuid[])`, ids)
		if err != nil {
			return nil, err
		}
		if live, err = pgx.CollectRows(rows, pgx.RowTo[string]); err != nil {
			return nil, err
		}
		if live == nil {
			live = []string{}
		}
	}
	return struct {
		IDs []string `json:"ids"`
	}{live}, nil
}

const dropUnnamed = `DELETE FROM ai_session_file_bytes WHERE sha256 IN (
	SELECT b.sha256 FROM ai_session_file_bytes b WHERE b.sha256 = ANY($1::text[])
	AND NOT EXISTS (SELECT 1 FROM ai_session_files f WHERE f.bytes_sha256 = b.sha256)
	FOR UPDATE SKIP LOCKED)`

// forgetRows is the last step of a forgotten session's forget: its transcript rows and its row
// (whose cascade takes the rest), then the workspace bytes no file names, then the warm evict.
func (s *Sweeps) forgetRows(r *http.Request, p crud.Params) (any, error) {
	session, err := p.UUID("session_id")
	if err != nil {
		return nil, err
	}
	ctx := r.Context()
	conn, err := s.Pool.Acquire(ctx)
	if err != nil {
		return nil, err
	}
	defer conn.Release()
	var shared []string
	var removed int64
	err = pgx.BeginFunc(ctx, conn, func(tx pgx.Tx) error {
		// The tombstone, held in the delete's transaction: no session is deleted unforgotten.
		var one int
		err := tx.QueryRow(ctx, `SELECT 1 FROM ai_forgotten_subjects WHERE subject = $1 FOR SHARE`,
			"session-"+session).Scan(&one)
		if errors.Is(err, pgx.ErrNoRows) {
			return httpx.WithReason(http.StatusConflict,
				fmt.Sprintf("session %s is not forgotten: forget its subject first", session), "not-forgotten")
		}
		if err != nil {
			return err
		}
		rows, err := tx.Query(ctx, `SELECT DISTINCT bytes_sha256 FROM ai_session_files
			WHERE session_id = $1::uuid AND bytes_sha256 IS NOT NULL`, session)
		if err != nil {
			return err
		}
		if shared, err = pgx.CollectRows(rows, pgx.RowTo[string]); err != nil {
			return err
		}
		for _, statement := range []string{
			`DELETE FROM ai_session_entries WHERE session_id = $1`,
			`DELETE FROM ai_sessions WHERE id = $1::uuid`,
		} {
			tag, err := tx.Exec(ctx, statement, session)
			if err != nil {
				return err
			}
			removed += tag.RowsAffected()
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	// A write naming the same bytes meanwhile fails this on the foreign key: the sweep deletes
	// them later.
	if len(shared) > 0 {
		err := pgx.BeginFunc(ctx, conn, func(tx pgx.Tx) error {
			_, err := tx.Exec(ctx, dropUnnamed, shared)
			return err
		})
		var pg *pgconn.PgError
		if errors.As(err, &pg) {
			slog.WarnContext(ctx, "forget: unnamed workspace bytes left for the sweep", "code", pg.Code)
		} else if err != nil {
			return nil, err
		}
	}
	// A session's warm process, on whichever replica holds it: sent once the rows' transaction
	// has committed, not in it.
	if _, err := conn.Exec(ctx, `SELECT pg_notify($1, $2)`, "scadbuddy_warm_evict", session); err != nil {
		return nil, err
	}
	return struct {
		Rows int64 `json:"rows"`
	}{removed}, nil
}
