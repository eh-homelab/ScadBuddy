// Package agentstart is what the agent reads at its start and per request: backend/scadbuddy/
// api/crud_agent_start.py (removed in #7667), statement for statement (areas migrations and auth-sessions).
package agentstart

import (
	"encoding/hex"
	"errors"
	"net/http"
	"regexp"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
)

const (
	migrationIDsMax = 5000
	// bodyBytesMax is the event-log routes' bound (crud_agent_events.py (removed in #7667) BODY_BYTES_MAX).
	bodyBytesMax = 32 * 1024 * 1024
)

var (
	migrationIDPattern = regexp.MustCompile(`^[0-9]{8}T[0-9]{4}Z_[a-z0-9_]{1,100}$`)
	hashPattern        = regexp.MustCompile(`^[0-9a-f]{64}$`)
)

// Start is the areas' routes.
type Start struct{ Pool *pgxpool.Pool }

// Ledger is area migrations' route, a read.
func (s *Start) Ledger() []crud.Route {
	return []crud.Route{{Method: "POST", Path: "/migrations/ledger", Handle: s.ledger, MaxBody: bodyBytesMax}}
}

// Lookup is area auth-sessions' route, a read.
func (s *Start) Lookup() []crud.Route {
	return []crud.Route{{Method: "POST", Path: "/auth-sessions/lookup", Handle: s.lookup, MaxBody: bodyBytesMax}}
}

type ledgerRow struct {
	ID       string  `json:"id"`
	Checksum *string `json:"checksum"`
}

type ledger struct {
	// Found is whether ai_migrations exists.
	Found bool `json:"found"`
	// Keyed is whether it is keyed by file id; false while it is still positional.
	Keyed bool        `json:"keyed"`
	Rows  []ledgerRow `json:"rows"`
}

func (s *Start) ledger(r *http.Request, _ crud.Params) (any, error) {
	body, err := crud.ReadBody(r, "ids")
	if err != nil {
		return nil, err
	}
	ids := []string{}
	for _, id := range body.Get("ids").List(0, migrationIDsMax) {
		ids = append(ids, id.String(0, 1<<30, migrationIDPattern))
	}
	if err := body.Err(); err != nil {
		return nil, err
	}
	conn, err := s.Pool.Acquire(r.Context())
	if err != nil {
		return nil, err
	}
	defer conn.Release()
	out := ledger{Rows: []ledgerRow{}}
	err = conn.QueryRow(r.Context(), `SELECT to_regclass('ai_migrations') IS NOT NULL AS found,
		EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = to_regclass('ai_migrations')
		AND attname = 'id' AND NOT attisdropped) AS keyed`).Scan(&out.Found, &out.Keyed)
	if err != nil {
		return nil, err
	}
	if !out.Found || !out.Keyed {
		return out, nil
	}
	rows, err := conn.Query(r.Context(),
		`SELECT id, checksum FROM ai_migrations WHERE id = ANY($1::text[])`, ids)
	if err != nil {
		return nil, err
	}
	found, err := pgx.CollectRows(rows, pgx.RowToStructByPos[ledgerRow])
	if err != nil {
		return nil, err
	}
	if found != nil {
		out.Rows = found
	}
	return out, nil
}

type person struct {
	Principal string  `json:"principal"`
	Name      *string `json:"name"`
	Kind      string  `json:"kind"`
}

func (s *Start) lookup(r *http.Request, _ crud.Params) (any, error) {
	body, err := crud.ReadBody(r, "id_hash")
	if err != nil {
		return nil, err
	}
	hash := body.Get("id_hash").String(0, 1<<30, hashPattern)
	if err := body.Err(); err != nil {
		return nil, err
	}
	raw, _ := hex.DecodeString(hash)
	var found *person
	var p person
	err = s.Pool.QueryRow(r.Context(),
		`SELECT principal, name, kind FROM auth_sessions_agent WHERE id_hash = $1`, raw).
		Scan(&p.Principal, &p.Name, &p.Kind)
	switch {
	case err == nil:
		found = &p
	case !errors.Is(err, pgx.ErrNoRows):
		return nil, err
	}
	return struct {
		Session *person `json:"session"`
	}{found}, nil
}
