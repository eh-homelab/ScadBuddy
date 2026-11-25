// Package approvals is the agent's classic approvals: backend/scadbuddy/agent_data/
// approvals.py and api/crud_agent_approvals.py, statement for statement (area approvals).
package approvals

import (
	"context"
	"encoding/json"
	"errors"
	"math"
	"net/http"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/audit"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/gate"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/jsonv"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/pending"
)

const (
	expiryMinS  = 10
	expiryMaxS  = 86_400
	listMax     = 500
	prepareLock = "scadbuddy:ai_approvals:mcp_prepare"
	triedMax    = 5000
	evictMax    = 1000
)

var decisions = []string{"approved", "denied", "expired", "cancelled"}

const columns = `id::text AS id, session_id::text AS session_id, turn_id::text AS turn_id,
 tool_use_id, tool, input_summary, input_hash, tier, requested_by_kind, requested_by_id,
 requested_by_label, requested_tiers, created_at, expires_at, decision, decided_by_kind,
 decided_by_id, decided_by_label, decided_at, reason, usable_until,
 resume_turn_id::text AS resume_turn_id, consumed_at, revoked_at, traceparent,
 decision_traceparent, (decision IS NULL AND expires_at <= now()) AS due`

const usable = `decision = 'approved' AND consumed_at IS NULL AND revoked_at IS NULL
 AND usable_until > now()`

// Row is an ai_approvals row as columns reads it.
type Row struct {
	ID                  string      `json:"id"`
	SessionID           *string     `json:"session_id"`
	TurnID              *string     `json:"turn_id"`
	ToolUseID           string      `json:"tool_use_id"`
	Tool                string      `json:"tool"`
	InputSummary        string      `json:"input_summary"`
	InputHash           string      `json:"input_hash"`
	Tier                string      `json:"tier"`
	RequestedByKind     string      `json:"requested_by_kind"`
	RequestedByID       string      `json:"requested_by_id"`
	RequestedByLabel    string      `json:"requested_by_label"`
	RequestedTiers      []string    `json:"requested_tiers"`
	CreatedAt           jsonv.Time  `json:"created_at"`
	ExpiresAt           jsonv.Time  `json:"expires_at"`
	Decision            *string     `json:"decision"`
	DecidedByKind       *string     `json:"decided_by_kind"`
	DecidedByID         *string     `json:"decided_by_id"`
	DecidedByLabel      *string     `json:"decided_by_label"`
	DecidedAt           *jsonv.Time `json:"decided_at"`
	Reason              *string     `json:"reason"`
	UsableUntil         *jsonv.Time `json:"usable_until"`
	ResumeTurnID        *string     `json:"resume_turn_id"`
	ConsumedAt          *jsonv.Time `json:"consumed_at"`
	RevokedAt           *jsonv.Time `json:"revoked_at"`
	Traceparent         *string     `json:"traceparent"`
	DecisionTraceparent *string     `json:"decision_traceparent"`
	Due                 bool        `json:"due"`
}

// Approvals is the area's routes.
type Approvals struct{ Pool *pgxpool.Pool }

// Routes are the area's routes.
func (a *Approvals) Routes() []crud.Route {
	return []crud.Route{
		{Method: "GET", Path: "/approvals/row", Handle: a.row},
		{Method: "GET", Path: "/approvals/session", Handle: a.session},
		{Method: "POST", Path: "/approvals/visible-pending", Handle: a.visiblePending},
		{Method: "POST", Path: "/approvals/prepared", Handle: a.prepared},
		{Method: "POST", Path: "/approvals/list", Handle: a.list},
		{Method: "POST", Path: "/approvals/due", Handle: a.due},
		{Method: "POST", Path: "/approvals/create", Handle: a.create},
		{Method: "POST", Path: "/approvals/create-prepared", Handle: a.createPrepared},
		{Method: "POST", Path: "/approvals/settle", Handle: a.settle},
		{Method: "POST", Path: "/approvals/consume", Handle: a.consume},
	}
}

type querier interface {
	Query(ctx context.Context, sql string, args ...any) (pgx.Rows, error)
}

func rows(ctx context.Context, q querier, sql string, args ...any) ([]Row, error) {
	found, err := q.Query(ctx, sql, args...)
	if err != nil {
		return nil, err
	}
	out, err := pgx.CollectRows(found, pgx.RowToStructByPos[Row])
	if out == nil {
		out = []Row{}
	}
	return out, err
}

// one is the first row of sql, nil for none.
func one(ctx context.Context, q querier, sql string, args ...any) (*Row, error) {
	found, err := rows(ctx, q, sql, args...)
	if err != nil || len(found) == 0 {
		return nil, err
	}
	return &found[0], nil
}

type oneAnswer struct {
	Approval *Row `json:"approval"`
}

type rowsAnswer struct {
	Approvals []Row `json:"approvals"`
}

func (a *Approvals) row(r *http.Request, _ crud.Params) (any, error) {
	id, err := crud.QueryText(r, "id", gate.NameMax)
	if err != nil {
		return nil, err
	}
	if !audit.IsUUID(id) {
		return oneAnswer{}, nil
	}
	found, err := one(r.Context(), a.Pool, `SELECT `+columns+` FROM ai_approvals WHERE id = $1::text::uuid`, id)
	if err != nil {
		return nil, err
	}
	return oneAnswer{found}, nil
}

type access struct {
	OwnerKind   string  `json:"owner_kind"`
	OwnerID     string  `json:"owner_id"`
	OwnerLabel  string  `json:"owner_label"`
	CreatorKind string  `json:"creator_kind"`
	CreatorID   string  `json:"creator_id"`
	OfferKind   *string `json:"offer_kind"`
	OfferID     *string `json:"offer_id"`
}

func (a *Approvals) session(r *http.Request, _ crud.Params) (any, error) {
	sid, err := crud.QueryText(r, "session_id", gate.NameMax)
	if err != nil {
		return nil, err
	}
	out := struct {
		Session *access `json:"session"`
	}{}
	if !audit.IsUUID(sid) {
		return out, nil
	}
	var s access
	err = a.Pool.QueryRow(r.Context(), `SELECT owner_kind, owner_id, owner_label, creator_kind,
		creator_id, CASE WHEN pending_owner_until > now() THEN pending_owner_kind END AS offer_kind,
		CASE WHEN pending_owner_until > now() THEN pending_owner_id END AS offer_id
		FROM ai_sessions WHERE id = $1::text::uuid`, sid).Scan(&s.OwnerKind, &s.OwnerID,
		&s.OwnerLabel, &s.CreatorKind, &s.CreatorID, &s.OfferKind, &s.OfferID)
	if errors.Is(err, pgx.ErrNoRows) {
		return out, nil
	}
	if err != nil {
		return nil, err
	}
	out.Session = &s
	return out, nil
}

// who is a body of {kind, id}.
func who(r *http.Request) (string, string, error) {
	b, err := crud.ReadBody(r, "kind", "id")
	if err != nil {
		return "", "", err
	}
	kind, id := b.Text("kind", 0, gate.NameMax), b.Text("id", 0, gate.NameMax)
	return kind, id, b.Err()
}

func (a *Approvals) visiblePending(r *http.Request, _ crud.Params) (any, error) {
	kind, id, err := who(r)
	if err != nil {
		return nil, err
	}
	found, err := rows(r.Context(), a.Pool, `SELECT `+columns+` FROM ai_approvals a
		WHERE a.decision IS NULL AND (
		 (a.session_id IS NULL AND a.requested_by_kind = $1 AND a.requested_by_id = $2)
		 OR EXISTS (SELECT 1 FROM ai_sessions s WHERE s.id = a.session_id
		 AND ((s.owner_kind = $1 AND s.owner_id = $2)
		 OR (s.creator_kind = $1 AND s.creator_id = $2))))
		ORDER BY a.created_at, a.id LIMIT $3`, kind, id, listMax)
	if err != nil {
		return nil, err
	}
	return rowsAnswer{found}, nil
}

func (a *Approvals) prepared(r *http.Request, _ crud.Params) (any, error) {
	kind, id, err := who(r)
	if err != nil {
		return nil, err
	}
	found, err := rows(r.Context(), a.Pool, `SELECT `+columns+` FROM ai_approvals
		WHERE session_id IS NULL AND requested_by_kind = $1 AND requested_by_id = $2
		AND decision IS NULL AND expires_at > now() ORDER BY created_at, id LIMIT $3`,
		kind, id, listMax)
	if err != nil {
		return nil, err
	}
	return rowsAnswer{found}, nil
}

func (a *Approvals) list(r *http.Request, _ crud.Params) (any, error) {
	b, err := crud.ReadBody(r, "session_id", "pending")
	if err != nil {
		return nil, err
	}
	sid := b.OptText("session_id", 0, gate.NameMax)
	onlyPending := b.Bool("pending", crud.False)
	if err := b.Err(); err != nil {
		return nil, err
	}
	var found []Row
	switch {
	case sid == nil:
		found, err = rows(r.Context(), a.Pool, `SELECT `+columns+` FROM ai_approvals
			WHERE decision IS NULL ORDER BY created_at, id LIMIT $1`, listMax)
	case !audit.IsUUID(*sid):
		found = []Row{}
	default:
		only := ""
		if onlyPending {
			only = " AND decision IS NULL"
		}
		found, err = rows(r.Context(), a.Pool, `SELECT `+columns+` FROM ai_approvals
			WHERE session_id = $1::text::uuid`+only+` ORDER BY created_at, id LIMIT $2`, *sid, listMax)
	}
	if err != nil {
		return nil, err
	}
	return rowsAnswer{found}, nil
}

func (a *Approvals) due(r *http.Request, _ crud.Params) (any, error) {
	b, err := crud.ReadBody(r, "tried", "limit")
	if err != nil {
		return nil, err
	}
	tried := b.TextItems("tried", present(b, "tried", triedMax), 0, gate.NameMax)
	limit := b.IntIn("limit", 1, listMax)
	if err := b.Err(); err != nil {
		return nil, err
	}
	ids := []string{}
	for _, t := range tried {
		if audit.IsUUID(t) {
			ids = append(ids, t)
		}
	}
	found, err := a.Pool.Query(r.Context(), `SELECT id::text AS id FROM ai_approvals
		WHERE decision IS NULL AND expires_at <= now() AND NOT (id = ANY($1::text[]::uuid[]))
		ORDER BY expires_at LIMIT $2`, ids, limit)
	if err != nil {
		return nil, err
	}
	out, err := pgx.CollectRows(found, pgx.RowTo[string])
	if err != nil {
		return nil, err
	}
	if out == nil {
		out = []string{}
	}
	return struct {
		IDs []string `json:"ids"`
	}{out}, nil
}

// newApproval is a pending approval's row; its summary and hash are the agent's.
type newApproval struct {
	id, toolUseID, tool, summary, hash, tier string
	sessionID, turnID, traceparent           *string
	by                                       jsonv.Object
	tiers                                    []string
	expiryS                                  int64
}

var newNames = []string{"id", "session_id", "turn_id", "tool_use_id", "tool", "input_summary",
	"input_hash", "tier", "requested_by", "requested_tiers", "expiry_s", "traceparent"}

func pattern(b *crud.Body, name string, s *string) {
	if s != nil {
		b.Pattern(name, *s, gate.SessionPattern)
	}
}

// readNew reads a New from b, whose issues are b's.
func readNew(b *crud.Body) *newApproval {
	n := &newApproval{}
	n.id = b.Text("id", 0, 1<<20)
	if b.Has("id") {
		b.Pattern("id", n.id, gate.SessionPattern)
	}
	n.sessionID = gate.Nullable(b, "session_id", 1<<20)
	pattern(b, "session_id", n.sessionID)
	n.turnID = gate.Nullable(b, "turn_id", 1<<20)
	pattern(b, "turn_id", n.turnID)
	n.toolUseID = b.Text("tool_use_id", 0, gate.NameMax)
	n.tool = b.Text("tool", 0, gate.NameMax)
	n.summary = b.Text("input_summary", 0, gate.TextMax)
	n.hash = b.Text("input_hash", 0, gate.NameMax)
	n.tier = b.Enum("tier", nil, "read", "write", "outward")
	n.by = gate.Principal(b, "requested_by")
	if items := b.List("requested_tiers", true, 8); items != nil {
		n.tiers = b.TextItems("requested_tiers", items, 0, gate.NameMax)
	}
	n.expiryS = b.IntIn("expiry_s", math.MinInt64, math.MaxInt64)
	n.traceparent = b.OptText("traceparent", 1, pending.TraceparentMax)
	return n
}

// present is the list name, empty when absent; a null one is an issue.
func present(b *crud.Body, name string, most int) []json.RawMessage {
	if !b.Has(name) {
		return nil
	}
	return b.List(name, false, most)
}

func ttl(seconds int64) int64 { return min(max(seconds, expiryMinS), expiryMaxS) }

func str(o jsonv.Object, key string) string {
	v, _ := o.Get(key)
	return gate.Str(v)
}

func insert(ctx context.Context, tx pgx.Tx, n *newApproval) (*Row, error) {
	made, err := one(ctx, tx, `INSERT INTO ai_approvals (id, session_id, turn_id, tool_use_id,
		tool, input_summary, input_hash, tier, requested_by_kind, requested_by_id,
		requested_by_label, requested_tiers, expires_at, traceparent)
		VALUES ($1::text::uuid, $2::text::uuid, $3::text::uuid, $4, $5, $6, $7, $8, $9, $10, $11,
		$12::text[], now() + ($13 * interval '1 second'), $14) RETURNING `+columns,
		n.id, n.sessionID, n.turnID, n.toolUseID, n.tool, n.summary, n.hash, n.tier,
		str(n.by, "kind"), str(n.by, "id"), str(n.by, "label"), n.tiers, ttl(n.expiryS),
		n.traceparent)
	if err == nil && made == nil {
		err = errors.New("approval vanished after insert")
	}
	return made, err
}

// entry is the agent's approvalEntry.
func entry(r *Row) jsonv.Object {
	m := gate.M
	return jsonv.Object{m("id", "approval:"+r.ID), m("kind", "approval"),
		m("session_id", *r.SessionID), m("tool", r.Tool), m("summary", r.InputSummary),
		m("input_hash", r.InputHash), m("prompt", ""),
		m("requested_by", jsonv.Object{m("kind", r.RequestedByKind), m("id", r.RequestedByID),
			m("label", r.RequestedByLabel)}),
		m("responders", []string{"browser", "grant"}),
		m("created_at", jsonv.ISO(r.CreatedAt.Time)), m("expires_at", jsonv.ISO(r.ExpiresAt.Time))}
}

type created struct {
	Approval *Row         `json:"approval"`
	Logged   gate.Batches `json:"logged"`
}

func (a *Approvals) create(r *http.Request, _ crud.Params) (any, error) {
	b, err := crud.ReadBody(r, newNames...)
	if err != nil {
		return nil, err
	}
	n := readNew(b)
	if err := b.Err(); err != nil {
		return nil, err
	}
	var out created
	err = pgx.BeginFunc(r.Context(), a.Pool, func(tx pgx.Tx) error {
		ctx := r.Context()
		made, err := insert(ctx, tx, n)
		if err != nil {
			return err
		}
		out.Approval = made
		if made.SessionID == nil {
			return nil
		}
		sid, m := *made.SessionID, gate.M
		return gate.Append(ctx, tx, &out.Logged, sid, []jsonv.Object{
			gate.Event(m("type", "approval.required"), m("sessionId", sid), m("id", made.ID),
				m("tool", n.toolUseID),
				m("summary", gate.Cap(n.tool+" "+n.summary, gate.ApprovalSummaryMax)),
				m("risk", "outward")),
			gate.Event(m("type", "input.requested"), m("sessionId", sid), m("entry", entry(made))),
		})
	})
	if err != nil {
		return nil, err
	}
	return out, nil
}

func (a *Approvals) createPrepared(r *http.Request, _ crud.Params) (any, error) {
	b, err := crud.ReadBody(r, "approval", "per_principal", "total", "evict_reason",
		"evict_traceparents")
	if err != nil {
		return nil, err
	}
	var n *newApproval
	if nested := b.Need("approval", newNames...); nested != nil {
		n = readNew(&nested.Body)
		nested.Done()
	}
	per := b.IntIn("per_principal", 1, math.MaxInt64)
	total := b.IntIn("total", 1, math.MaxInt64)
	reason := b.Text("evict_reason", 0, gate.TextMax)
	traceparents := b.TextItems("evict_traceparents",
		present(b, "evict_traceparents", evictMax), 1, pending.TraceparentMax)
	if err := b.Err(); err != nil {
		return nil, err
	}
	out := struct {
		Approval *Row  `json:"approval"`
		Evicted  []Row `json:"evicted"`
	}{Evicted: []Row{}}
	err = pgx.BeginFunc(r.Context(), a.Pool, func(tx pgx.Tx) error {
		ctx := r.Context()
		if _, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock(hashtextextended($1, 0))`, prepareLock); err != nil {
			return err
		}
		ownRows, err := tx.Query(ctx, `SELECT id::text AS id FROM ai_approvals
			WHERE session_id IS NULL AND requested_by_kind = $1 AND requested_by_id = $2
			AND decision IS NULL AND expires_at > now() ORDER BY created_at, id`,
			str(n.by, "kind"), str(n.by, "id"))
		if err != nil {
			return err
		}
		own, err := pgx.CollectRows(ownRows, pgx.RowTo[string])
		if err != nil {
			return err
		}
		evict := own[:max(int64(len(own))-per+1, 0)]
		if len(evict) > 0 {
			cancelled, err := rows(ctx, tx, `UPDATE ai_approvals SET decision = 'cancelled',
				decided_at = now(), reason = $1 WHERE id = ANY($2::text[]::uuid[])
				AND decision IS NULL RETURNING `+columns, reason, evict)
			if err != nil {
				return err
			}
			for i := range cancelled {
				if i >= len(traceparents) {
					break
				}
				if _, err := tx.Exec(ctx, `UPDATE ai_approvals SET decision_traceparent = $1
					WHERE id = $2::text::uuid`, traceparents[i], cancelled[i].ID); err != nil {
					return err
				}
				cancelled[i].DecisionTraceparent = &traceparents[i]
			}
			out.Evicted = cancelled
		} else {
			var count int64
			if err := tx.QueryRow(ctx, `SELECT count(*)::int AS n FROM ai_approvals
				WHERE session_id IS NULL AND decision IS NULL AND expires_at > now()`).Scan(&count); err != nil {
				return err
			}
			if count >= total {
				return nil
			}
		}
		out.Approval, err = insert(ctx, tx, n)
		return err
	})
	if err != nil {
		return nil, err
	}
	return out, nil
}

func (a *Approvals) settle(r *http.Request, _ crud.Params) (any, error) {
	b, err := crud.ReadBody(r, "id", "decision", "by", "reason", "expiry_s", "traceparent",
		"surface", "client_ip")
	if err != nil {
		return nil, err
	}
	id := b.Text("id", 0, gate.NameMax)
	decision := b.Enum("decision", nil, decisions...)
	var by jsonv.Object
	if !b.Null("by") {
		by = gate.Principal(b, "by")
	}
	reason := b.OptText("reason", 0, gate.TextMax)
	expiry := b.IntOr("expiry_s", 0, math.MinInt64, math.MaxInt64)
	traceparent := b.OptText("traceparent", 1, pending.TraceparentMax)
	surface := b.OptEnum("surface", audit.Surfaces...)
	ip := b.OptText("client_ip", 0, 200)
	if err := b.Err(); err != nil {
		return nil, err
	}
	if (decision == "approved" || decision == "denied") && by == nil {
		// A person's decision names who made it; only the system expires or cancels.
		return nil, &httpx.Problem{Status: http.StatusUnprocessableEntity,
			Detail: "an approval or a denial names who decided it"}
	}
	out := struct {
		Approval *Row         `json:"approval"`
		Logged   gate.Batches `json:"logged"`
	}{}
	if !audit.IsUUID(id) {
		return out, nil
	}
	var seconds int64
	if decision == "approved" {
		seconds = ttl(expiry)
	}
	var kind, who, label *string
	if by != nil {
		kind, who, label = audit.Ptr(str(by, "kind")), audit.Ptr(str(by, "id")), audit.Ptr(str(by, "label"))
	}
	err = pgx.BeginFunc(r.Context(), a.Pool, func(tx pgx.Tx) error {
		ctx := r.Context()
		settled, err := one(ctx, tx, `UPDATE ai_approvals SET decision = $2::text,
			decided_by_kind = $3, decided_by_id = $4, decided_by_label = $5, decided_at = now(),
			reason = $6, usable_until = CASE WHEN $2::text = 'approved'
			THEN now() + ($7 * interval '1 second') END, decision_traceparent = $8
			WHERE id = $1::text::uuid AND decision IS NULL
			AND ($2::text IN ('expired', 'cancelled') OR expires_at > now())
			RETURNING `+columns, id, decision, kind, who, label, reason, seconds, traceparent)
		if err != nil || settled == nil {
			return err
		}
		out.Approval = settled
		given := reason != nil && *reason != ""
		late := decision == "expired" || decision == "cancelled"
		if settled.SessionID != nil {
			sid, m := *settled.SessionID, gate.M
			resolved := gate.Event(m("type", "approval.resolved"), m("sessionId", sid), m("id", id),
				m("approved", decision == "approved"), m("decision", decision))
			if by != nil && !late {
				resolved = append(resolved, m("by", by))
			}
			if given && late {
				resolved = append(resolved, m("reason", *reason))
			}
			ended := gate.Event(m("type", "input.resolved"), m("sessionId", sid),
				m("id", "approval:"+id), m("kind", "approval"), m("outcome", decision))
			if given && late {
				ended = append(ended, m("reason", *reason))
			}
			if err := gate.Append(ctx, tx, &out.Logged, sid, []jsonv.Object{resolved, ended}); err != nil {
				return err
			}
		}
		actor, where := by, "system"
		if by == nil {
			actor = gate.System()
		} else {
			where = "http"
			if surface != nil {
				where = *surface
			}
		}
		result := "refused"
		switch decision {
		case "approved":
			result = "ok"
		case "denied":
			result = "denied"
		}
		detail := settled.Tool
		if given {
			detail += ": " + *reason
		}
		detail += " (requested by " + settled.RequestedByLabel + ")"
		return audit.Insert(ctx, tx, audit.Entry{
			Kind: "approval", Action: decision, Surface: where,
			ActorKind: audit.Cap(str(actor, "kind"), 50), ActorID: audit.Cap(str(actor, "id"), 200),
			ActorLabel: audit.Cap(str(actor, "label"), 200), Outcome: result, ClientIP: ip,
			SessionID: settled.SessionID, TurnID: settled.TurnID,
			ToolUseID: &settled.ToolUseID, Tier: &settled.Tier, InputHash: &settled.InputHash,
			InputSummary: audit.Ptr(audit.Cap(settled.InputSummary, 1000)),
			ApprovalID:   &settled.ID, Detail: audit.Ptr(audit.Cap(detail, 500)),
			StartedAt: settled.CreatedAt.Time, FinishedAt: settled.DecidedAt.Time,
		})
	})
	if err != nil {
		return nil, err
	}
	return out, nil
}

func (a *Approvals) consume(r *http.Request, _ crud.Params) (any, error) {
	b, err := crud.ReadBody(r, "id", "by", "input_hash")
	if err != nil {
		return nil, err
	}
	id := b.Text("id", 0, gate.NameMax)
	var kind, who string
	if n := b.Need("by", "kind", "id"); n != nil {
		kind, who = n.Text("kind", 0, gate.NameMax), n.Text("id", 0, gate.NameMax)
		n.Done()
	}
	hash := b.Text("input_hash", 0, gate.NameMax)
	if err := b.Err(); err != nil {
		return nil, err
	}
	if !audit.IsUUID(id) {
		return oneAnswer{}, nil
	}
	var used *Row
	err = pgx.BeginFunc(r.Context(), a.Pool, func(tx pgx.Tx) error {
		used, err = one(r.Context(), tx, `UPDATE ai_approvals SET consumed_at = now()
			WHERE id = $1::text::uuid AND session_id IS NULL AND requested_by_kind = $2
			AND requested_by_id = $3 AND input_hash = $4 AND `+usable+` RETURNING `+columns,
			id, kind, who, hash)
		return err
	})
	if err != nil {
		return nil, err
	}
	return oneAnswer{used}, nil
}
