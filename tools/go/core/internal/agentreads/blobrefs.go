// Package agentreads is backend/scadbuddy/api/crud_agent.py but its assistant-cost route,
// statement for statement: the holds of the workspace files' blobs (area
// workspace-blob-refs, store/refs.py), the backend's event log's head and gap (area events),
// and the agent's reads of the flow tables (area flows, flows/agent_reads.py).
//
// POST …/orders/{id}/assistant-cost (area order-costs) is internal/assistantcost's.
package agentreads

import (
	"net/http"
	"regexp"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
)

const (
	// workspaceHolder is the blob_refs.holder_kind of a workspace file.
	workspaceHolder = "ai_session_file"
	holdsPageMax    = 1000
	// olderThanMax is a year: far past any grace.
	olderThanMax  = 31_536_000
	jsonBodyBytes = 8 * 1024 * 1024 // api/limits.py MAX_TEXT_BODY_BYTES
)

var (
	workspaceKey  = regexp.MustCompile(`^workspace-[0-9a-f]{64}$`)
	holderPattern = regexp.MustCompile(`^(session|principal):[^/]+(/[^/]+)*/[A-Za-z0-9_][A-Za-z0-9_.-]*$`)
)

// Reads is the backend's tables as the agent reads them.
type Reads struct{ Pool *pgxpool.Pool }

// HoldReads are area workspace-blob-refs' GET route.
func (a *Reads) HoldReads() []crud.Route {
	return []crud.Route{{Method: "GET", Path: "/workspace-blob-refs", Handle: a.listHolds}}
}

// HoldWrites are area workspace-blob-refs' writes.
func (a *Reads) HoldWrites() []crud.Route {
	return []crud.Route{
		{Method: "POST", Path: "/workspace-blob-refs", Handle: a.hold, MaxBody: jsonBodyBytes},
		{Method: "POST", Path: "/workspace-blob-refs/release", Handle: a.release, MaxBody: jsonBodyBytes},
	}
}

type hold struct {
	Key      string `json:"key"`
	HolderID string `json:"holder_id"`
}

func readHold(body *crud.Body) hold {
	return hold{
		Key:      body.Get("key").String(0, 1<<30, workspaceKey),
		HolderID: body.Get("holder_id").String(3, 1024, holderPattern),
	}
}

// hold holds a key for a holder; a hold sent again refreshes its created_at.
func (a *Reads) hold(r *http.Request, _ crud.Params) (any, error) {
	body, err := crud.ReadBody(r, "key", "holder_id")
	if err != nil {
		return nil, err
	}
	h := readHold(body)
	if err := body.Err(); err != nil {
		return nil, err
	}
	_, err = a.Pool.Exec(r.Context(), `INSERT INTO blob_refs (key, holder_kind, holder_id) VALUES ($1, $2, $3)
		ON CONFLICT (key, holder_kind, holder_id) DO UPDATE SET created_at = now()`,
		h.Key, workspaceHolder, h.HolderID)
	if err != nil {
		return nil, err
	}
	return &crud.Reply{Status: http.StatusNoContent}, nil
}

// release drops one hold; with older_than_seconds only one taken longer ago than that.
func (a *Reads) release(r *http.Request, _ crud.Params) (any, error) {
	body, err := crud.ReadBody(r, "key", "holder_id", "older_than_seconds")
	if err != nil {
		return nil, err
	}
	h := readHold(body)
	var older *int64
	if v := body.Get("older_than_seconds"); !v.Null() {
		n := v.Int(0, olderThanMax, false)
		older = &n
	}
	if err := body.Err(); err != nil {
		return nil, err
	}
	tag, err := a.Pool.Exec(r.Context(), `DELETE FROM blob_refs WHERE key = $1 AND holder_kind = $2
		AND holder_id = $3
		AND ($4::bigint IS NULL OR created_at < now() - $4::bigint * interval '1 second')`,
		h.Key, workspaceHolder, h.HolderID, older)
	if err != nil {
		return nil, err
	}
	return struct {
		Released bool `json:"released"`
	}{tag.RowsAffected() > 0}, nil
}

func (a *Reads) listHolds(r *http.Request, _ crud.Params) (any, error) {
	older, _, err := crud.QueryInt(r, "older_than_seconds", 0, olderThanMax)
	if err != nil {
		return nil, err
	}
	afterHolder, err := crud.QueryString(r, "after_holder_id", 0, 1024, false)
	if err != nil {
		return nil, err
	}
	afterKey, err := crud.QueryString(r, "after_key", 0, 200, false)
	if err != nil {
		return nil, err
	}
	limit, given, err := crud.QueryInt(r, "limit", 1, holdsPageMax)
	if err != nil {
		return nil, err
	}
	if !given {
		limit = 500
	}
	holder, key := "", ""
	if afterHolder != nil {
		holder = *afterHolder
		if afterKey != nil {
			key = *afterKey
		}
	}
	rows, err := a.Pool.Query(r.Context(), `SELECT key, holder_id FROM blob_refs WHERE holder_kind = $1
		AND created_at < now() - $2::bigint * interval '1 second' AND (holder_id, key) > ($3, $4)
		ORDER BY holder_id, key LIMIT $5`, workspaceHolder, older, holder, key, limit)
	if err != nil {
		return nil, err
	}
	holds, err := pgx.CollectRows(rows, pgx.RowToStructByPos[hold])
	if err != nil {
		return nil, err
	}
	out := struct {
		Holds []hold `json:"holds"`
		// Next is the last hold of a full page; null on the last page.
		Next *hold `json:"next"`
	}{Holds: []hold{}}
	if holds != nil {
		out.Holds = holds
	}
	if int64(len(holds)) == limit {
		out.Next = &holds[len(holds)-1]
	}
	return out, nil
}
