// Package edits is an edit of a session: backend/scadbuddy/api/crud_agent_edits.py and
// agent_data/edits.py, statement for statement (area session-edits).
//
// Under the row's lock, taken only while the principal owns it: a done refused while a turn
// holds the row, an archive refused while a turn holds it or anything is parked on the person;
// the row written; the warm process evicted on commit; an undismissed done summary dismissed
// with an archive; and their events, in one transaction. A done this transaction would newly
// write needs told, and untold is the last check.
package edits

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"slices"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
)

const (
	nameMax      = 512
	titleMax     = 4096
	bodyBytesMax = 64 * 1024
	// poolWait and lockTimeoutMs bound a write's waits under the agent's 30 s request
	// (crud_agent_turns.py POOL_WAIT_S, agent_data/turns.py LOCK_TIMEOUT_MS).
	poolWait      = 10 * time.Second
	lockTimeoutMs = 10_000
	// archivedReason is why a done summary dismissed by an archive ended.
	archivedReason = "the chat was archived"
	// done is the agent's isDoneSummary.
	doneSummary = "(q.attention_reason IS NOT DISTINCT FROM 'done' AND q.expires_at IS NULL)"
)

var ownerKinds = []string{"browser", "bearer", "oidc", "anonymous", "flow"}

// Edits is the area's route.
type Edits struct{ Pool *pgxpool.Pool }

// Routes are the area's route.
func (e *Edits) Routes() []crud.Route {
	return []crud.Route{
		{Method: "POST", Path: "/sessions/{session_id}/edit", Handle: e.edit, MaxBody: bodyBytesMax},
	}
}

// logged is events appended in the transaction, as crud_agent_gate.py Logged.
type logged struct {
	SessionID string            `json:"session_id"`
	Events    []json.RawMessage `json:"events"`
	Seqs      []int64           `json:"seqs"`
}

type dismissed struct {
	ID        string     `json:"id"`
	TurnID    string     `json:"turn_id"`
	Tool      string     `json:"tool"`
	ToolUseID string     `json:"tool_use_id"`
	CreatedAt crud.Stamp `json:"created_at"`
}

func refused(why string) error {
	status := http.StatusConflict
	if why == "gone" {
		status = http.StatusNotFound
	}
	return httpx.WithReason(status, "the edit was refused: "+why, why)
}

func (e *Edits) edit(r *http.Request, p crud.Params) (any, error) {
	session, err := p.UUID("session_id")
	if err != nil {
		return nil, err
	}
	body, err := crud.ReadBody(r, "principal", "title", "done", "archived", "told")
	if err != nil {
		return nil, err
	}
	var kind, owner string
	if w := body.Get("principal").Object("kind", "id"); w != nil {
		kind, owner = w.Get("kind").Enum(ownerKinds...), w.Get("id").String(1, nameMax, nil)
		w.Close()
	}
	title := body.Get("title").OptString(1, titleMax, nil)
	done := body.Get("done").BoolOr(false)
	var archived *bool
	if v := body.Get("archived"); !v.Null() {
		a := v.Bool()
		archived = &a
	}
	told := body.Get("told").BoolOr(false)
	if err := body.Err(); err != nil {
		return nil, err
	}
	archive := archived != nil && *archived
	unarchive := archived != nil && !*archived

	ctx := r.Context()
	wait, cancel := context.WithTimeout(ctx, poolWait)
	conn, err := e.Pool.Acquire(wait)
	cancel()
	if err != nil {
		if ctx.Err() == nil && errors.Is(err, context.DeadlineExceeded) {
			return nil, httpx.WithReason(http.StatusServiceUnavailable,
				"no database connection was free; send the write again", "pool_busy")
		}
		return nil, err
	}
	defer conn.Release()

	out := struct {
		Logged    []logged    `json:"logged"`
		Dismissed []dismissed `json:"dismissed"`
	}{[]logged{}, []dismissed{}}
	err = pgx.BeginFunc(ctx, conn, func(tx pgx.Tx) error {
		if _, err := tx.Exec(ctx, fmt.Sprintf("SET LOCAL lock_timeout = %d", lockTimeoutMs)); err != nil {
			return err
		}
		var was string
		var wasArchived, busy, parked bool
		err := tx.QueryRow(ctx, `SELECT status AS was, archived_at IS NOT NULL AS archived,
			status IN ('running', 'waiting_approval', 'waiting_input') AS busy,
			(EXISTS (SELECT 1 FROM ai_approvals WHERE session_id = $1 AND decision IS NULL)
			OR EXISTS (SELECT 1 FROM ai_questions q WHERE q.session_id = $1
			AND q.outcome IS NULL AND NOT `+doneSummary+`)
			OR EXISTS (SELECT 1 FROM ai_pending_input WHERE session_id = $1)) AS parked
			FROM ai_sessions
			WHERE id = $1 AND owner_kind = $2 AND (owner_id = $3 OR owner_kind = 'browser')
			FOR UPDATE`, session, kind, owner).Scan(&was, &wasArchived, &busy, &parked)
		if errors.Is(err, pgx.ErrNoRows) {
			var one int
			err := tx.QueryRow(ctx, `SELECT 1 FROM ai_sessions WHERE id = $1`, session).Scan(&one)
			if errors.Is(err, pgx.ErrNoRows) {
				return refused("gone")
			}
			if err != nil {
				return err
			}
			return refused("changed_owner")
		}
		if err != nil {
			return err
		}
		if done && busy {
			return refused("busy_done")
		}
		if archive && !wasArchived {
			if busy {
				return refused("busy_archive")
			}
			if parked {
				return refused("parked")
			}
		}
		newlyDone := done && was != "done"
		// Last, after every other refusal: untold tells the agent the edit would pass.
		if newlyDone && !told {
			return refused("untold")
		}
		if _, err := tx.Exec(ctx, `UPDATE ai_sessions SET title = coalesce($1::text, title),
			status = CASE WHEN $2::boolean THEN 'done' ELSE status END,
			archived_at = CASE WHEN $3::boolean THEN coalesce(archived_at, now())
			WHEN $4::boolean THEN NULL ELSE archived_at END,
			updated_at = now() WHERE id = $5`, title, done, archive, unarchive, session); err != nil {
			return err
		}
		// Its warm process on every replica goes with it: sent on commit.
		if newlyDone || (archive && !wasArchived) {
			if _, err := tx.Exec(ctx, `SELECT pg_notify($1, $2)`, "scadbuddy_warm_evict", session); err != nil {
				return err
			}
		}
		var events []any
		if newlyDone {
			events = append(events, struct {
				V         int    `json:"v"`
				Type      string `json:"type"`
				SessionID string `json:"sessionId"`
				Status    string `json:"status"`
			}{1, "session.status", session, "done"})
		}
		if archive {
			rows, err := tx.Query(ctx, `UPDATE ai_questions q SET outcome = 'cancelled', reason = $1,
				resolved_at = now()
				WHERE q.session_id = $2 AND q.outcome IS NULL AND `+doneSummary+`
				RETURNING q.id::text AS id, q.turn_id::text AS turn_id, q.tool,
				q.tool_use_id, q.created_at`, archivedReason, session)
			if err != nil {
				return err
			}
			gone, err := pgx.CollectRows(rows, pgx.RowToStructByPos[dismissed])
			if err != nil {
				return err
			}
			for _, q := range gone {
				events = append(events, struct {
					V         int    `json:"v"`
					Type      string `json:"type"`
					SessionID string `json:"sessionId"`
					ID        string `json:"id"`
					Answered  bool   `json:"answered"`
					Reason    string `json:"reason"`
				}{1, "question.resolved", session, q.ID, false, archivedReason}, struct {
					V         int    `json:"v"`
					Type      string `json:"type"`
					SessionID string `json:"sessionId"`
					ID        string `json:"id"`
					Kind      string `json:"kind"`
					Outcome   string `json:"outcome"`
					Reason    string `json:"reason"`
				}{1, "input.resolved", session, "question:" + q.ID, "answer", "cancelled", archivedReason})
			}
			if gone != nil {
				out.Dismissed = gone
			}
		}
		if len(events) == 0 {
			return nil
		}
		batch := logged{SessionID: session}
		texts := make([]string, len(events))
		for i, event := range events {
			text, err := httpx.Marshal(event)
			if err != nil {
				return err
			}
			texts[i] = string(text)
			batch.Events = append(batch.Events, text)
		}
		if batch.Seqs, err = appendIn(ctx, tx, session, texts); err != nil {
			return err
		}
		out.Logged = append(out.Logged, batch)
		return nil
	})
	var pg *pgconn.PgError
	if errors.As(err, &pg) && pg.Code == "55P03" {
		return nil, httpx.WithReason(http.StatusServiceUnavailable,
			"the session's row stayed locked; send the write again", "row_busy")
	}
	if err != nil {
		return nil, err
	}
	return out, nil
}

// appendIn is the event log's append in the caller's transaction (agent_data/events.py
// append_in); the seqs, oldest first.
func appendIn(ctx context.Context, tx pgx.Tx, session string, texts []string) ([]int64, error) {
	rows, err := tx.Query(ctx, `WITH s AS (
		UPDATE ai_sessions SET event_seq = event_seq + $1, updated_at = now()
		WHERE id = $2::text::uuid RETURNING event_seq - $1 AS base)
		INSERT INTO ai_session_events (session_id, seq, event)
		SELECT $2::text::uuid, s.base + e.ord, e.event
		FROM s, unnest($3::text[]) WITH ORDINALITY AS e(event, ord)
		RETURNING seq`, int64(len(texts)), session, texts)
	if err != nil {
		return nil, err
	}
	seqs, err := pgx.CollectRows(rows, pgx.RowTo[int64])
	if err != nil {
		return nil, err
	}
	if len(seqs) != len(texts) {
		return nil, httpx.WithReason(http.StatusNotFound,
			fmt.Sprintf("session %s does not exist", session), "session_gone")
	}
	slices.Sort(seqs)
	return seqs, nil
}
