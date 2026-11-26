package notify

import (
	"net/http"
	"strconv"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
)

const (
	// pushWindow is the agent's notify/dispatcher.ts WAITING_PUSH_WINDOW_MS: an older request
	// is not pushed, so it is not listed.
	pushWindow = 24 * time.Hour
	// grantRequestsMax is api/flow_triggers.py GRANT_REQUESTS_MAX.
	grantRequestsMax = 100
)

// terminal is flows/models.py TERMINAL, sorted as the query sends it.
var terminal = []string{"failed", "succeeded", "terminated"}

type waitingRequest struct {
	ID     string    `json:"id"`
	Reason string    `json:"reason"`
	Since  crud.Time `json:"since"`
}

// waiting is #6399: the flow grants waiting for a person (a run's and a trigger's standing
// grant) requested within pushWindow, by id only, newest first, at most grantRequestsMax of
// each (flows/store.py _requested_grants, flow_triggers/store.py _requested_grant_ids). A
// standing grant's id carries its requested_at in whole epoch seconds, as the page's does.
func (s *Store) waiting(r *http.Request, _ crud.Params) (any, error) {
	ctx := r.Context()
	newerThan := time.Now().Add(-pushWindow)
	requests := []waitingRequest{}
	runs, err := s.Pool.Query(ctx, `SELECT g.id::text, g.created_at FROM flow_grants g
		WHERE g.status = 'requested' AND g.revoked_at IS NULL
		AND EXISTS (SELECT 1 FROM workflow_runs r WHERE r.id = g.run_id AND NOT (r.status = ANY($1)))
		AND ($2::timestamptz IS NULL OR g.created_at > $2)
		ORDER BY g.created_at DESC, g.id DESC LIMIT $3`, terminal, newerThan, grantRequestsMax)
	if err != nil {
		return nil, err
	}
	err = collect(runs, func(id string, at time.Time) {
		requests = append(requests, waitingRequest{"run-grant:" + id, "grant", crud.Time(at)})
	})
	if err != nil {
		return nil, err
	}
	standing, err := s.Pool.Query(ctx, `SELECT id::text, requested_at FROM flow_trigger_grants
		WHERE status = 'requested' AND revoked_at IS NULL AND requested_at > $1
		ORDER BY requested_at DESC, id DESC LIMIT $2`, newerThan, grantRequestsMax)
	if err != nil {
		return nil, err
	}
	err = collect(standing, func(id string, at time.Time) {
		epoch := at.Unix() // floored, as Math.floor(Date.parse(at) / 1000)
		requests = append(requests, waitingRequest{
			"trigger-grant:" + id + ":" + strconv.FormatInt(epoch, 10), "grant", crud.Time(at)})
	})
	if err != nil {
		return nil, err
	}
	return struct {
		Requests []waitingRequest `json:"requests"`
	}{requests}, nil
}

func collect(rows pgx.Rows, each func(string, time.Time)) error {
	defer rows.Close()
	for rows.Next() {
		var id string
		var at time.Time
		if err := rows.Scan(&id, &at); err != nil {
			return err
		}
		each(id, at)
	}
	return rows.Err()
}
