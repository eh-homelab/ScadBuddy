// Package ordercosts is the order items a session revised and its spend's report to them:
// backend/scadbuddy/api/crud_agent_order_costs.py (removed in #7667) and agent_data/order_costs.py (removed in #7667), statement for
// statement (area session-order-costs).
//
// A report runs under the session's lease (order-cost:<session id>). Each write of a report
// checks in its own transaction that the caller's token still holds the lease, so a report
// whose lease lapsed writes nothing.
package ordercosts

import (
	"context"
	"errors"
	"fmt"
	"math"
	"net/http"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
)

const (
	idMax        = 200
	itemsMax     = 1000
	sweepMax     = 100
	bodyBytesMax = 512 * 1024
	// sweepAfter is how long a row waits before the sweep reports it; idleOverAfter how long
	// an idle session is not over.
	sweepAfter    = "15 minutes"
	idleOverAfter = "1 hour"
)

// Costs is the area's routes.
type Costs struct{ Pool *pgxpool.Pool }

// Routes are the area's routes: every one writes.
func (c *Costs) Routes() []crud.Route {
	return []crud.Route{
		{Method: "POST", Path: "/sessions/{session_id}/order-costs", Handle: c.insert, MaxBody: bodyBytesMax},
		{Method: "POST", Path: "/order-costs/sweep", Handle: c.sweep, MaxBody: bodyBytesMax},
		{Method: "POST", Path: "/sessions/{session_id}/order-costs/report", Handle: c.begin, MaxBody: bodyBytesMax},
		{Method: "POST", Path: "/sessions/{session_id}/order-costs/attempt", Handle: c.attempt, MaxBody: bodyBytesMax},
		{Method: "POST", Path: "/sessions/{session_id}/order-costs/settle", Handle: c.settle, MaxBody: bodyBytesMax},
	}
}

type done struct {
	Done bool `json:"done"`
}

func (c *Costs) insert(r *http.Request, p crud.Params) (any, error) {
	session, err := p.UUID("session_id")
	if err != nil {
		return nil, err
	}
	body, err := crud.ReadBody(r, "order_id", "order_item_id")
	if err != nil {
		return nil, err
	}
	order := body.Get("order_id").String(1, idMax, nil)
	item := body.Get("order_item_id").String(1, idMax, nil)
	if err := body.Err(); err != nil {
		return nil, err
	}
	_, err = c.Pool.Exec(r.Context(), `INSERT INTO ai_session_order_costs (session_id, order_id, order_item_id)
		SELECT $1::uuid, $2, $3
		WHERE NOT EXISTS (SELECT 1 FROM ai_session_order_costs
		WHERE session_id = $1::uuid AND order_id = $2 AND reported_at IS NOT NULL)
		AND EXISTS (SELECT 1 FROM ai_sessions WHERE id = $1::uuid)
		ON CONFLICT DO NOTHING`, session, order, item)
	if err != nil {
		return nil, err
	}
	return done{true}, nil
}

func (c *Costs) sweep(r *http.Request, _ crud.Params) (any, error) {
	body, err := crud.ReadBody(r, "limit")
	if err != nil {
		return nil, err
	}
	limit := body.Get("limit").Int(1, sweepMax, false)
	if err := body.Err(); err != nil {
		return nil, err
	}
	rows, err := c.Pool.Query(r.Context(), `SELECT c.session_id::text AS session_id
		FROM ai_session_order_costs c JOIN ai_sessions s ON s.id = c.session_id
		WHERE c.reported_at IS NULL AND c.at < now() - $1::interval
		AND s.status NOT IN ('running', 'waiting_approval', 'waiting_input')
		GROUP BY c.session_id ORDER BY max(c.tried_at) NULLS FIRST, min(c.at) LIMIT $2`,
		sweepAfter, limit)
	if err != nil {
		return nil, err
	}
	sessions, err := pgx.CollectRows(rows, pgx.RowTo[string])
	if err != nil {
		return nil, err
	}
	if sessions == nil {
		sessions = []string{}
	}
	return struct {
		Sessions []string `json:"sessions"`
	}{sessions}, nil
}

// leased is a write under the session's lease: refused 409 lease-lost, writing nothing, once
// token no longer holds it.
func (c *Costs) leased(ctx context.Context, session, token string, write func(pgx.Tx) error) error {
	return pgx.BeginFunc(ctx, c.Pool, func(tx pgx.Tx) error {
		var one int
		err := tx.QueryRow(ctx, `SELECT 1 FROM agent_leases WHERE key = $1 AND token = $2
			AND expires_at > now() FOR SHARE`, "order-cost:"+session, token).Scan(&one)
		if errors.Is(err, pgx.ErrNoRows) {
			return httpx.WithReason(http.StatusConflict,
				fmt.Sprintf("the report lease of session %s is not held", session), "lease-lost")
		}
		if err != nil {
			return err
		}
		return write(tx)
	})
}

type pending struct {
	OrderID      string   `json:"order_id"`
	OrderItemID  string   `json:"order_item_id"`
	AttemptedUSD *float64 `json:"attempted_usd"`
}

type report struct {
	Pending   []pending `json:"pending"`
	Cost      float64   `json:"cost"`
	Carried   float64   `json:"carried"`
	Principal string    `json:"principal"`
	Over      bool      `json:"over"`
}

func (c *Costs) begin(r *http.Request, p crud.Params) (any, error) {
	session, err := p.UUID("session_id")
	if err != nil {
		return nil, err
	}
	body, err := crud.ReadBody(r, "token")
	if err != nil {
		return nil, err
	}
	token := body.Get("token").UUID()
	if err := body.Err(); err != nil {
		return nil, err
	}
	ctx := r.Context()
	var found *report
	err = c.leased(ctx, session, token, func(tx pgx.Tx) error {
		rows, err := tx.Query(ctx, `SELECT order_id, order_item_id, attempted_usd FROM ai_session_order_costs
			WHERE session_id = $1::uuid AND reported_at IS NULL
			ORDER BY at, order_id, order_item_id`, session)
		if err != nil {
			return err
		}
		left, err := pgx.CollectRows(rows, pgx.RowToStructByPos[pending])
		if err != nil || len(left) == 0 {
			return err
		}
		if _, err := tx.Exec(ctx, `UPDATE ai_session_order_costs SET tried_at = now()
			WHERE session_id = $1::uuid AND reported_at IS NULL`, session); err != nil {
			return err
		}
		out := report{Pending: left}
		err = tx.QueryRow(ctx, `SELECT s.cost_usd AS cost, s.owner_id AS principal,
			(s.status IN ('done', 'failed') OR (s.status = 'idle'
			AND s.updated_at < now() - $1::interval)) AS over,
			(SELECT COALESCE(SUM(COALESCE(reported_usd, 0) + COALESCE(attempted_usd, 0)), 0)
			FROM ai_session_order_costs WHERE session_id = s.id) AS carried
			FROM ai_sessions s WHERE s.id = $2::uuid`, idleOverAfter, session).
			Scan(&out.Cost, &out.Principal, &out.Over, &out.Carried)
		if errors.Is(err, pgx.ErrNoRows) {
			return nil
		}
		if err != nil {
			return err
		}
		found = &out
		return nil
	})
	if err != nil {
		return nil, err
	}
	return struct {
		Report *report `json:"report"`
	}{found}, nil
}

func (c *Costs) attempt(r *http.Request, p crud.Params) (any, error) {
	session, err := p.UUID("session_id")
	if err != nil {
		return nil, err
	}
	body, err := crud.ReadBody(r, "token", "order_id", "items", "amount_usd")
	if err != nil {
		return nil, err
	}
	token := body.Get("token").UUID()
	order := body.Get("order_id").String(1, idMax, nil)
	var items []string
	for _, item := range body.Get("items").List(1, itemsMax) {
		items = append(items, item.String(1, idMax, nil))
	}
	amount := body.Get("amount_usd").Float(0, true, math.MaxFloat64)
	if err := body.Err(); err != nil {
		return nil, err
	}
	ctx := r.Context()
	err = c.leased(ctx, session, token, func(tx pgx.Tx) error {
		_, err := tx.Exec(ctx, `UPDATE ai_session_order_costs SET attempted_usd = $1::float8
			WHERE session_id = $2::uuid AND order_id = $3 AND order_item_id = ANY($4::text[])
			AND reported_at IS NULL`, amount/float64(len(items)), session, order, items)
		return err
	})
	if err != nil {
		return nil, err
	}
	return done{true}, nil
}

var settles = map[string]string{
	// Recorded (or refused for good): reported, carrying what was sent.
	"reported": "reported_at = now(), reported_usd = COALESCE(attempted_usd, 0), attempted_usd = NULL",
	// Under one minor unit while the session goes on: sent again with more spend.
	"unsent": "attempted_usd = NULL",
	// A share that can never be written: reported, nothing carried, out of the sweep.
	"closed": "reported_at = now(), reported_usd = 0, attempted_usd = NULL",
}

func (c *Costs) settle(r *http.Request, p crud.Params) (any, error) {
	session, err := p.UUID("session_id")
	if err != nil {
		return nil, err
	}
	body, err := crud.ReadBody(r, "token", "order_id", "outcome")
	if err != nil {
		return nil, err
	}
	token := body.Get("token").UUID()
	order := body.Get("order_id").String(1, idMax, nil)
	outcome := body.Get("outcome").Enum("reported", "unsent", "closed")
	if err := body.Err(); err != nil {
		return nil, err
	}
	ctx := r.Context()
	err = c.leased(ctx, session, token, func(tx pgx.Tx) error {
		_, err := tx.Exec(ctx, `UPDATE ai_session_order_costs SET `+settles[outcome]+
			` WHERE session_id = $1::uuid AND order_id = $2 AND reported_at IS NULL`, session, order)
		return err
	})
	if err != nil {
		return nil, err
	}
	return done{true}, nil
}
