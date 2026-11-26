package agentreads

import (
	"encoding/json"
	"net/http"
	"strconv"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
)

// fleetKindPrefix starts every fleet event's kind: Python's FLEET_KIND_PREFIX
// (backend/scadbuddy/api/crud_agent_changes.py), which the agent's change stream leaves out.
const fleetKindPrefix = "fleet."

const (
	// eventsPageMax is the agent's replay limit plus one.
	eventsPageMax = 5001
	seqMax        = 1<<63 - 1
)

// EventLog is area events' routes: the backend's event log's head and its gap after a seq,
// for the LISTEN replay of an agent older than the change stream.
func (a *Reads) EventLog() []crud.Route {
	return []crud.Route{
		{Method: "GET", Path: "/events/head", Handle: a.head},
		{Method: "GET", Path: "/events", Handle: a.gap},
	}
}

func (a *Reads) head(r *http.Request, _ crud.Params) (any, error) {
	var seq int64
	var at time.Time
	err := a.Pool.QueryRow(r.Context(),
		`SELECT coalesce(max(seq), 0) AS seq, now() AS at FROM events`).Scan(&seq, &at)
	if err != nil {
		return nil, err
	}
	return struct {
		Seq string    `json:"seq"`
		At  crud.Time `json:"at"`
	}{strconv.FormatInt(seq, 10), crud.Time(at)}, nil
}

type loggedEvent struct {
	Seq      string          `json:"seq"`
	EventID  string          `json:"event_id"`
	Payload  json.RawMessage `json:"payload"`
	LoggedAt crud.Time       `json:"logged_at"`
}

// gap is the logged events after a seq, newest first, and the clock at the start of the
// transaction that read them, before the rows were read.
func (a *Reads) gap(r *http.Request, _ crud.Params) (any, error) {
	after, given, err := crud.QueryInt(r, "after", 0, seqMax)
	if err == nil && !given {
		_, err = crud.QueryString(r, "after", 0, 0, true)
	}
	if err != nil {
		return nil, err
	}
	limit, given, err := crud.QueryInt(r, "limit", 1, eventsPageMax)
	if err != nil {
		return nil, err
	}
	if !given {
		limit = 1001
	}
	ctx := r.Context()
	events := []loggedEvent{}
	var clock time.Time
	err = pgx.BeginFunc(ctx, a.Pool, func(tx pgx.Tx) error {
		// The fleet's kinds name its session ids, its only identity: they are for the fleet
		// caller alone (#7818), as in the agent's change stream (crud_agent_changes.py).
		rows, err := tx.Query(ctx, `SELECT seq::text, event_id, payload, logged_at FROM events
			WHERE seq > $1 AND NOT starts_with(kind, $3) ORDER BY seq DESC LIMIT $2`,
			after, limit, fleetKindPrefix)
		if err != nil {
			return err
		}
		found, err := pgx.CollectRows(rows, pgx.RowToStructByPos[loggedEvent])
		if err != nil {
			return err
		}
		if found != nil {
			events = found
		}
		return tx.QueryRow(ctx, `SELECT now() AS at`).Scan(&clock)
	})
	if err != nil {
		return nil, err
	}
	return struct {
		Events []loggedEvent `json:"events"`
		ReadAt crud.Time     `json:"read_at"`
	}{events, crud.Time(clock)}, nil
}
