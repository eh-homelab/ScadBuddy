// Package turns is the turn reads of backend/scadbuddy/api/crud_agent_turns.py that the
// agent polls (area session-turns): the rest of the area is still Python's.
package turns

import (
	"errors"
	"net/http"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
)

// graceMax is the longest grace asked for, in milliseconds: a day.
const graceMax = 24 * 3600 * 1000

// Turns is the area's routes.
type Turns struct{ Pool *pgxpool.Pool }

// Polled is the read each running turn makes once a second.
func (t *Turns) Polled() []crud.Route {
	return []crud.Route{{Method: "GET", Path: "/sessions/{session_id}/turn", Handle: t.state}}
}

type state struct {
	Status             string  `json:"status"`
	DurableTurnID      *string `json:"durable_turn_id"`
	TurnAttempt        *string `json:"turn_attempt"`
	InterruptRequested bool    `json:"interrupt_requested"`
	OwnerKind          string  `json:"owner_kind"`
	Runtime            *string `json:"runtime"`
	EventSeq           string  `json:"event_seq"`
	CostUSD            float64 `json:"cost_usd"`
	// Settled is no write for grace_ms.
	Settled bool `json:"settled"`
}

func (t *Turns) state(r *http.Request, p crud.Params) (any, error) {
	session, err := p.UUID("session_id")
	if err != nil {
		return nil, err
	}
	grace, _, err := crud.QueryInt(r, "grace_ms", 0, graceMax)
	if err != nil {
		return nil, err
	}
	rows, err := t.Pool.Query(r.Context(),
		`SELECT status, durable_turn_id::text, turn_attempt, interrupt_requested, owner_kind,
		 runtime, event_seq::text, cost_usd::float8,
		 updated_at < now() - make_interval(secs => $1) AS settled
		 FROM ai_sessions WHERE id = $2::text::uuid`, float64(grace)/1000, session)
	if err != nil {
		return nil, err
	}
	found, err := pgx.CollectExactlyOneRow(rows, pgx.RowToAddrOfStructByPos[state])
	if err != nil && !errors.Is(err, pgx.ErrNoRows) {
		return nil, err
	}
	return struct {
		Turn *state `json:"turn"`
	}{found}, nil
}
