package fleet

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

// Virtual senders (#8026, #7971 part 6) are reserved one-way names, rows of
// fleet_virtual_senders: `rotator`, `github`. Only the machine service's caller posts as one;
// the `fleet` caller, which serves fleet chat's unauthenticated MCP, cannot. A DM to one is
// refused with its reject message.

// VirtualCaller is the service that posts as a virtual sender: the machine service
// (spec 2026-11-26-fleet-session-registry-and-rotator-design §6.2, §6.7).
const VirtualCaller = "fleet-rotator"

// VirtualRoutes are the virtual senders' routes, under /api/v1/internal/crud/fleet-rotator.
func VirtualRoutes(pool *pgxpool.Pool) []crud.Route {
	f := &Fleet{Pool: pool}
	return []crud.Route{
		{Method: "POST", Path: "/virtual-messages", Handle: f.sendVirtual, MaxBody: messageBodyMax},
	}
}

// virtualRefusal is the answer to a DM to name: a 422 with the virtual sender's reject
// message (reason virtual-sender), or unknownAgent when no virtual sender has the name.
func virtualRefusal(ctx context.Context, tx pgx.Tx, name string) error {
	var reject string
	err := tx.QueryRow(ctx, `SELECT reject_message FROM fleet_virtual_senders WHERE name = $1`, name).Scan(&reject)
	if errors.Is(err, pgx.ErrNoRows) {
		return unknownAgent()
	}
	if err != nil {
		return err
	}
	return httpx.WithReason(http.StatusUnprocessableEntity, reject, "virtual-sender")
}

func textTooLong() error {
	p := httpx.WithReason(http.StatusUnprocessableEntity,
		fmt.Sprintf("text is longer than %d bytes", textMax), "text-too-long")
	p.Extensions = append(p.Extensions, httpx.Field{Key: "errors", Value: []httpx.Issue{{
		Loc: []string{"body", "text"}, Msg: fmt.Sprintf("text is at most %d bytes", textMax),
	}}})
	return p
}

// sendVirtual stores a message from a virtual sender: a post to a channel's members at send
// time, or a DM to one agent by its handle. The sender is in no channel and gets no delivery.
func (f *Fleet) sendVirtual(r *http.Request, _ crud.Params) (any, error) {
	b, err := crud.ReadBody(r, "sender", "channel", "agent", "text")
	if err != nil {
		return nil, err
	}
	sender := b.Match("sender", 1, 40, channelPattern)
	channel := b.OptText("channel", 1, 40, channelPattern)
	agent := b.OptText("agent", 8, 8, handlePattern)
	text := b.Text("text", 1, math.MaxInt)
	if err := b.Err(); err != nil {
		return nil, err
	}
	if (channel == nil) == (agent == nil) {
		return nil, httpx.Invalid(httpx.Issue{Loc: []string{"body"}, Msg: "give exactly one of channel or agent"})
	}
	if len(text) > textMax {
		return nil, textTooLong()
	}
	ctx := r.Context()
	var seq int64
	var recipients []recipient
	err = pgx.BeginFunc(ctx, f.Pool, func(tx pgx.Tx) error {
		if _, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock($1)`, sendLock); err != nil {
			return err
		}
		var one int
		err := tx.QueryRow(ctx, `SELECT 1 FROM fleet_virtual_senders WHERE name = $1`, sender).Scan(&one)
		if errors.Is(err, pgx.ErrNoRows) {
			return httpx.WithReason(http.StatusNotFound,
				fmt.Sprintf(`no virtual sender is named "%s"`, sender), "unknown-sender")
		}
		if err != nil {
			return err
		}
		if agent != nil {
			var to string
			err := tx.QueryRow(ctx, `SELECT session_id FROM fleet_agents WHERE handle = $1`, *agent).Scan(&to)
			if errors.Is(err, pgx.ErrNoRows) {
				return unknownAgent()
			}
			if err != nil {
				return err
			}
			if err := tx.QueryRow(ctx, `INSERT INTO fleet_messages (from_sender, to_session, text)
				VALUES ($1, $2, $3) RETURNING seq`, sender, to, text).Scan(&seq); err != nil {
				return err
			}
			if _, err := tx.Exec(ctx, `INSERT INTO fleet_deliveries (session_id, seq) VALUES ($1, $2)`,
				to, seq); err != nil {
				return err
			}
		} else {
			if err := channelExists(ctx, tx, *channel); err != nil {
				return err
			}
			if err := tx.QueryRow(ctx, `INSERT INTO fleet_messages (from_sender, channel, text)
				VALUES ($1, $2, $3) RETURNING seq`, sender, *channel, text).Scan(&seq); err != nil {
				return err
			}
			if _, err := tx.Exec(ctx, `INSERT INTO fleet_deliveries (session_id, seq)
				SELECT session_id, $2 FROM fleet_members WHERE channel = $1`, *channel, seq); err != nil {
				return err
			}
		}
		rows, err := tx.Query(ctx, `SELECT a.session_id::text, a.handle FROM fleet_deliveries d
			JOIN fleet_agents a USING (session_id) WHERE d.seq = $1 ORDER BY a.handle`, seq)
		if err != nil {
			return err
		}
		recipients, err = pgx.CollectRows(rows, func(row pgx.CollectableRow) (recipient, error) {
			var rc recipient
			return rc, row.Scan(&rc.Session, &rc.Handle)
		})
		return err
	})
	if err != nil {
		return nil, err
	}
	target := ""
	if channel != nil {
		target = *channel
	}
	f.announce(r, seq, recipients, target)
	ids := make([]string, len(recipients))
	for i, rc := range recipients {
		ids[i] = rc.Session
	}
	return map[string]any{"seq": seq, "recipients": ids}, nil
}
