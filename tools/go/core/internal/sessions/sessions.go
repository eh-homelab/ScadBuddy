// Package sessions is a session's row and what its tool calls touched: backend/scadbuddy/
// api/crud_agent_sessions.py and agent_data/sessions.py, statement for statement (areas
// sessions and session-resources).
//
// The insert takes the owner's start lock before it counts the owner's recent sessions, and a
// fork's parent's payload-key lock shared before it reads the parent's tombstone, as Python
// and the agent's SQL do, so every path excludes the others.
package sessions

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"regexp"
	"strings"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
)

const (
	nameMax         = 512
	titleMax        = 4096
	tagsMax         = 64
	tagMax          = 200
	scopeBytesMax   = 64 * 1024
	bodyBytesMax    = 160 * 1024
	searchMax       = 500
	acceptMax       = 500
	acceptEntryMax  = 200
	resourcesMax    = 100
	resourceIDMax   = 300
	toolMax         = 300
	budgetMaxUSD    = 100.0
	maxTurnsMax     = 1_000_000
	windowMsMax     = 24 * 3600 * 1000
	limitMax        = 10_000
	offerTTLMsMax   = 7 * 24 * 3600 * 1000
	forkCutMax      = 1<<31 - 1
	viewportWMin    = 320
	viewportWMax    = 3840
	viewportHMin    = 320
	viewportHMax    = 2160
	browserLocaleMx = 35
)

var (
	// UUIDPattern is a session id as the routes take it.
	UUIDPattern   = regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`)
	uaPattern     = regexp.MustCompile(`^[\x20-\x7E]{1,512}$`)
	localePattern = regexp.MustCompile(`^[A-Za-z]{2,8}(-[A-Za-z0-9]{1,8})*$`)
	modelPattern  = regexp.MustCompile(`^[A-Za-z0-9._:\[\]-]{1,100}$`)
	typePattern   = regexp.MustCompile(`^[a-z0-9][a-z0-9-]{0,63}$`)

	ownerKinds    = []string{"browser", "bearer", "oidc", "anonymous", "flow"}
	origins       = []string{"chat", "mcp", "analyzer", "hook"}
	statuses      = []string{"running", "waiting_input", "waiting_approval", "idle", "done", "failed"}
	resourceTypes = []string{
		"model", "revision", "preset", "asset", "render_job", "output", "print_run", "print",
		"library", "font", "setting", "project", "bambuddy_file", "print_archive", "library_file",
		"unclassified",
	}
)

// liveOffer is a pending handoff offer, read as none once it has expired.
const liveOffer = "pending_owner_until > now()"

const root = "coalesce(ai_sessions.budget_root_id, ai_sessions.id)"

const ms = `'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"'`

func iso(column, name string) string {
	return "to_char(" + column + " AT TIME ZONE 'UTC', " + ms + ") AS " + name
}

// columns is the row as the session manager reads it, timestamps as text.
var columns = "id::text AS id, origin, owner_kind, owner_id, owner_label, creator_kind, creator_id," +
	" CASE WHEN " + liveOffer + " THEN pending_owner_kind END AS offer_kind," +
	" CASE WHEN " + liveOffer + " THEN pending_owner_id END AS offer_id," +
	" CASE WHEN " + liveOffer + " THEN pending_owner_label END AS offer_label," +
	" CASE WHEN " + liveOffer + " THEN" +
	" to_char(pending_owner_until AT TIME ZONE 'UTC', " + ms + ") END AS offer_until," +
	" status, title, tags, scope, browser, parent_id::text AS parent_id, runtime, max_turns," +
	" coalesce((SELECT r.budget_usd FROM ai_sessions r WHERE r.id = " + root + ")," +
	" ai_sessions.budget_usd) AS budget_usd, cost_usd," +
	" (SELECT sum(m.cost_usd) FROM ai_sessions m WHERE coalesce(m.budget_root_id, m.id) = " + root + ")" +
	" AS pool_cost_usd, turns," +
	" (runtime = 'unified' AND durable_turn_id IS NOT NULL) AS turn_active," +
	" " + iso("created_at", "created_at") + ", " + iso("updated_at", "updated_at") + "," +
	" CASE WHEN archived_at IS NULL THEN NULL ELSE" +
	" to_char(archived_at AT TIME ZONE 'UTC', " + ms + ") END AS archived_at"

// turnColumns is what a turn's claim reads besides: claimed_at at microseconds.
const turnColumns = "unpriced_cost_usd, agent_model, agent_effort, agent_type, background_tools, turn_flow," +
	" turn_tiers, durable_turn_id::text AS durable_turn_id," +
	" to_char(coalesce(turn_started_at, updated_at) AT TIME ZONE 'UTC'," +
	` 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"') AS claimed_at`

// Sessions is the areas' routes.
type Sessions struct{ Pool *pgxpool.Pool }

// Reads are area sessions' GET routes.
func (s *Sessions) Reads() []crud.Route {
	return []crud.Route{
		{Method: "GET", Path: "/session-starts", Handle: s.startedRecently},
		{Method: "GET", Path: "/sessions/{session_id}", Handle: s.get},
		{Method: "POST", Path: "/sessions/list", Handle: s.list, MaxBody: bodyBytesMax},
	}
}

// Writes are area sessions' writes.
func (s *Sessions) Writes() []crud.Route {
	return []crud.Route{
		{Method: "POST", Path: "/sessions", Handle: s.create, MaxBody: bodyBytesMax},
		{Method: "POST", Path: "/sessions/{session_id}/agent", Handle: s.setAgent, MaxBody: bodyBytesMax},
		{Method: "POST", Path: "/sessions/{session_id}/offer", Handle: s.offer, MaxBody: bodyBytesMax},
		{Method: "POST", Path: "/sessions/{session_id}/offer/cancel", Handle: s.cancelOffer, MaxBody: bodyBytesMax},
		{Method: "POST", Path: "/sessions/{session_id}/owner", Handle: s.transfer, MaxBody: bodyBytesMax},
		{Method: "POST", Path: "/sessions/{session_id}/budget", Handle: s.raiseBudget, MaxBody: bodyBytesMax},
		{Method: "POST", Path: "/sessions/{session_id}/discard", Handle: s.discard},
	}
}

// ResourceReads are area session-resources' GET route.
func (s *Sessions) ResourceReads() []crud.Route {
	return []crud.Route{{Method: "GET", Path: "/sessions/{session_id}/resources", Handle: s.listResources}}
}

// ResourceWrites are area session-resources' write.
func (s *Sessions) ResourceWrites() []crud.Route {
	return []crud.Route{
		{Method: "POST", Path: "/sessions/{session_id}/resources", Handle: s.recordResources, MaxBody: bodyBytesMax},
	}
}

// Row is the row as the session manager reads it.
type Row struct {
	ID          string          `json:"id"`
	Origin      string          `json:"origin"`
	OwnerKind   string          `json:"owner_kind"`
	OwnerID     string          `json:"owner_id"`
	OwnerLabel  string          `json:"owner_label"`
	CreatorKind string          `json:"creator_kind"`
	CreatorID   string          `json:"creator_id"`
	OfferKind   *string         `json:"offer_kind"`
	OfferID     *string         `json:"offer_id"`
	OfferLabel  *string         `json:"offer_label"`
	OfferUntil  *string         `json:"offer_until"`
	Status      string          `json:"status"`
	Title       string          `json:"title"`
	Tags        []string        `json:"tags"`
	Scope       json.RawMessage `json:"scope"`
	Browser     json.RawMessage `json:"browser"`
	ParentID    *string         `json:"parent_id"`
	Runtime     string          `json:"runtime"`
	MaxTurns    int64           `json:"max_turns"`
	BudgetUSD   float64         `json:"budget_usd"`
	CostUSD     float64         `json:"cost_usd"`
	PoolCostUSD *float64        `json:"pool_cost_usd"`
	Turns       int64           `json:"turns"`
	TurnActive  *bool           `json:"turn_active"`
	CreatedAt   string          `json:"created_at"`
	UpdatedAt   string          `json:"updated_at"`
	ArchivedAt  *string         `json:"archived_at"`
}

// FullRow is the row with what a turn's claim reads.
type FullRow struct {
	Row
	UnpricedCostUSD float64         `json:"unpriced_cost_usd"`
	AgentModel      *string         `json:"agent_model"`
	AgentEffort     *string         `json:"agent_effort"`
	AgentType       *string         `json:"agent_type"`
	BackgroundTools bool            `json:"background_tools"`
	TurnFlow        bool            `json:"turn_flow"`
	TurnTiers       json.RawMessage `json:"turn_tiers"`
	DurableTurnID   *string         `json:"durable_turn_id"`
	ClaimedAt       string          `json:"claimed_at"`
}

func (r *Row) targets() []any {
	return []any{&r.ID, &r.Origin, &r.OwnerKind, &r.OwnerID, &r.OwnerLabel, &r.CreatorKind,
		&r.CreatorID, &r.OfferKind, &r.OfferID, &r.OfferLabel, &r.OfferUntil, &r.Status, &r.Title,
		&r.Tags, &r.Scope, &r.Browser, &r.ParentID, &r.Runtime, &r.MaxTurns, &r.BudgetUSD,
		&r.CostUSD, &r.PoolCostUSD, &r.Turns, &r.TurnActive, &r.CreatedAt, &r.UpdatedAt, &r.ArchivedAt}
}

func (r *Row) done() *Row {
	if r.Tags == nil {
		r.Tags = []string{}
	}
	if r.Browser == nil {
		r.Browser = json.RawMessage("null")
	}
	return r
}

type querier interface {
	Query(ctx context.Context, sql string, args ...any) (pgx.Rows, error)
}

// Get is the row with what a turn's claim reads, or nil.
func Get(ctx context.Context, q querier, session string) (*FullRow, error) {
	rows, err := q.Query(ctx, "SELECT "+columns+", "+turnColumns+" FROM ai_sessions WHERE id = $1", session)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	if !rows.Next() {
		return nil, rows.Err()
	}
	var f FullRow
	targets := append(f.Row.targets(), &f.UnpricedCostUSD, &f.AgentModel, &f.AgentEffort,
		&f.AgentType, &f.BackgroundTools, &f.TurnFlow, &f.TurnTiers, &f.DurableTurnID, &f.ClaimedAt)
	if err := rows.Scan(targets...); err != nil {
		return nil, err
	}
	f.Row.done()
	if f.TurnTiers == nil {
		f.TurnTiers = json.RawMessage("null")
	}
	return &f, rows.Err()
}

type one struct {
	Session *FullRow `json:"session"`
}

func (s *Sessions) get(r *http.Request, p crud.Params) (any, error) {
	session, err := p.UUID("session_id")
	if err != nil {
		return nil, err
	}
	row, err := Get(r.Context(), s.Pool, session)
	if err != nil {
		return nil, err
	}
	return one{row}, nil
}

func (s *Sessions) startedRecently(r *http.Request, _ crud.Params) (any, error) {
	kind, err := crud.QueryOptChoice(r, "owner_kind", ownerKinds...)
	issues := crud.Issues(err)
	if err == nil && kind == nil {
		issues = append(issues, httpx.Issue{Loc: []string{"query", "owner_kind"}, Msg: "Field required"})
	}
	owner, err := crud.QueryText(r, "owner_id", nameMax)
	issues = append(issues, crud.Issues(err)...)
	window, given, err := crud.QueryInt(r, "window_ms", 1, windowMsMax)
	issues = append(issues, crud.Issues(err)...)
	if err == nil && !given {
		issues = append(issues, httpx.Issue{Loc: []string{"query", "window_ms"}, Msg: "Field required"})
	}
	if len(issues) > 0 {
		return nil, httpx.Invalid(issues...)
	}
	n, err := recent(r.Context(), s.Pool, *kind, owner, window)
	if err != nil {
		return nil, err
	}
	return struct {
		Count int64 `json:"count"`
	}{n}, nil
}

type rowQuerier interface {
	QueryRow(ctx context.Context, sql string, args ...any) pgx.Row
}

func recent(ctx context.Context, q rowQuerier, kind, owner string, windowMs int64) (int64, error) {
	var n int64
	err := q.QueryRow(ctx, `SELECT count(*)::int AS n FROM ai_sessions
		WHERE owner_kind = $1 AND owner_id = $2
		AND created_at > now() - ($3::bigint * interval '1 millisecond')`,
		kind, owner, windowMs).Scan(&n)
	return n, err
}

// who reads a principal as a condition: its kind and id.
func who(b *crud.Body) (string, string) {
	if b == nil {
		return "", ""
	}
	defer b.Close()
	return b.Get("kind").Enum(ownerKinds...), b.Get("id").String(1, nameMax, nil)
}

type owner struct{ kind, id, label string }

func ownerOf(b *crud.Body) owner {
	if b == nil {
		return owner{}
	}
	defer b.Close()
	return owner{
		b.Get("kind").Enum(ownerKinds...),
		b.Get("id").String(1, nameMax, nil),
		b.Get("label").String(0, nameMax, nil),
	}
}

// likeLiteral is text as a literal part of an ILIKE pattern.
func likeLiteral(text string) string {
	return strings.NewReplacer(`\`, `\\`, `%`, `\%`, `_`, `\_`).Replace(text)
}

func (s *Sessions) list(r *http.Request, _ crud.Params) (any, error) {
	body, err := crud.ReadBody(r, "principal", "status", "origin", "archived", "resource",
		"search", "accept", "with_file", "limit")
	if err != nil {
		return nil, err
	}
	var principal *[2]string
	if v := body.Get("principal"); !v.Given() || !v.Null() {
		if o := v.Object("kind", "id"); o != nil {
			kind, id := who(o)
			principal = &[2]string{kind, id}
		}
	}
	status := body.Get("status").OptEnum(statuses...)
	origin := body.Get("origin").OptEnum(origins...)
	archived := "exclude"
	if v := body.Get("archived"); v.Given() {
		archived = v.Enum("exclude", "only", "include")
	}
	var resource *[2]string
	if o := body.Get("resource").OptObject("type", "id"); o != nil {
		resource = &[2]string{o.Get("type").Enum(resourceTypes...), o.Get("id").String(1, resourceIDMax, nil)}
		o.Close()
	}
	search := body.Get("search").OptString(0, searchMax, nil)
	var accept []string
	if v := body.Get("accept"); v.Given() {
		for _, item := range v.List(0, acceptMax) {
			accept = append(accept, item.String(0, acceptEntryMax, nil))
		}
	}
	withFile := body.Get("with_file").BoolOr(false)
	limit := int64(100)
	if v := body.Get("limit"); v.Given() {
		limit = v.Int(1, 500, false)
	}
	if err := body.Err(); err != nil {
		return nil, err
	}

	var where []string
	var args []any
	arg := func(v any) string {
		args = append(args, v)
		return fmt.Sprintf("$%d", len(args))
	}
	if principal != nil {
		kind, pid := arg(principal[0]), arg(principal[1])
		where = append(where, "((owner_kind = "+kind+" AND owner_id = "+pid+")"+
			" OR (creator_kind = "+kind+" AND creator_id = "+pid+")"+
			" OR (pending_owner_kind = "+kind+" AND pending_owner_id = "+pid+" AND "+liveOffer+"))")
	}
	if status != nil && *status != "" {
		where = append(where, "status = "+arg(*status))
	}
	if origin != nil && *origin != "" {
		where = append(where, "origin = "+arg(*origin))
	}
	if archived != "include" {
		if archived == "only" {
			where = append(where, "archived_at IS NOT NULL")
		} else {
			where = append(where, "archived_at IS NULL")
		}
	}
	if resource != nil {
		var match string
		if resource[0] == "model" {
			match = "r.model_slug = " + arg(resource[1])
		} else {
			match = "r.resource_type = " + arg(resource[0]) + " AND r.resource_id = " + arg(resource[1])
		}
		where = append(where, "EXISTS (SELECT 1 FROM ai_session_resources r"+
			" WHERE r.session_id = ai_sessions.id AND "+match+")")
	}
	searching := search != nil && *search != ""
	var accepted []string
	if searching || withFile {
		for _, entry := range accept {
			switch {
			case strings.HasPrefix(entry, "."):
				accepted = append(accepted, "lower(f.path) LIKE "+arg("%"+likeLiteral(entry)))
			case strings.HasSuffix(entry, "/*"):
				accepted = append(accepted, "lower(f.media_type) LIKE "+arg(likeLiteral(entry[:len(entry)-1])+"%"))
			default:
				accepted = append(accepted, "lower(f.media_type) = "+arg(entry))
			}
		}
	}
	fileWhere := func(extra string) string {
		kinds := ""
		if len(accepted) > 0 {
			kinds = " AND (" + strings.Join(accepted, " OR ") + ")"
		}
		return "EXISTS (SELECT 1 FROM ai_session_files f" +
			" WHERE f.workspace = 'session:' || ai_sessions.id::text" + kinds + extra + ")"
	}
	if searching {
		pattern := arg("%" + likeLiteral(*search) + "%")
		where = append(where, "(title ILIKE "+pattern+" OR "+fileWhere(" AND f.path ILIKE "+pattern)+")")
	}
	if withFile {
		where = append(where, fileWhere(""))
	}
	text := "SELECT " + columns + " FROM ai_sessions"
	if len(where) > 0 {
		text += " WHERE " + strings.Join(where, " AND ")
	}
	text += " ORDER BY updated_at DESC LIMIT " + arg(min(max(limit, 1), 500))
	rows, err := s.Pool.Query(r.Context(), text, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []*Row{}
	for rows.Next() {
		var row Row
		if err := rows.Scan(row.targets()...); err != nil {
			return nil, err
		}
		out = append(out, row.done())
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	return struct {
		Sessions []*Row `json:"sessions"`
	}{out}, nil
}

type newSession struct {
	id, origin, title, runtime string
	owner                      owner
	tags                       []string
	scope                      json.RawMessage
	parent, budgetOf           *string
	maxTurns                   int64
	budget                     float64
	forkParent                 *string
	forkCut                    *int64
	background                 *bool
	browser                    []byte
	limit                      *[2]int64
}

func (s *Sessions) create(r *http.Request, _ crud.Params) (any, error) {
	body, err := crud.ReadBody(r, "id", "origin", "owner", "title", "tags", "scope", "parent_id",
		"runtime", "max_turns", "budget_usd", "budget_of", "fork_of", "background_tools", "browser", "limit")
	if err != nil {
		return nil, err
	}
	n := newSession{
		id:     body.Get("id").String(0, 1<<30, UUIDPattern),
		origin: body.Get("origin").Enum(origins...),
		owner:  ownerOf(body.Get("owner").Object("kind", "id", "label")),
		title:  body.Get("title").String(0, titleMax, nil),
	}
	n.tags = []string{}
	for _, tag := range body.Get("tags").List(0, tagsMax) {
		n.tags = append(n.tags, tag.String(0, tagMax, nil))
	}
	scope := body.Get("scope")
	if n.scope = scope.Dict(); n.scope != nil {
		if size, err := pythonDumpsLen(n.scope); err != nil || size > scopeBytesMax {
			crud.Fail(scope, fmt.Sprintf("Value error, a scope is at most %d bytes as JSON", scopeBytesMax))
		}
	}
	n.parent = body.Get("parent_id").OptString(0, 1<<30, UUIDPattern)
	// Absent or null is unified: an older agent pod may send null during a roll (#7323).
	n.runtime = "unified"
	if runtime := body.Get("runtime").OptEnum("durable", "unified"); runtime != nil {
		n.runtime = *runtime
	}
	n.maxTurns = body.Get("max_turns").Int(1, maxTurnsMax, false)
	n.budget = body.Get("budget_usd").Float(0, false, budgetMaxUSD)
	n.budgetOf = body.Get("budget_of").OptString(0, 1<<30, UUIDPattern)
	if fork := body.Get("fork_of").OptObject("parent_id", "cut"); fork != nil {
		parent := fork.Get("parent_id").String(0, 1<<30, UUIDPattern)
		cut := fork.Get("cut").Int(0, forkCutMax, false)
		n.forkParent, n.forkCut = &parent, &cut
		fork.Close()
	}
	if v := body.Get("background_tools"); !v.Null() {
		var b *bool
		if json.Unmarshal(v.Raw(), &b) != nil || b == nil || *b {
			crud.Fail(v, "Input should be False")
		}
		f := false
		n.background = &f
	}
	if br := body.Get("browser").OptObject("user_agent", "viewport", "locale"); br != nil {
		n.browser = browserOf(br)
		br.Close()
	}
	if l := body.Get("limit").OptObject("max", "window_ms"); l != nil {
		n.limit = &[2]int64{l.Get("max").Int(1, limitMax, false), l.Get("window_ms").Int(1, windowMsMax, false)}
		l.Close()
	}
	if err := body.Err(); err != nil {
		return nil, err
	}
	refused, err := s.insert(r.Context(), n)
	var pg *pgconn.PgError
	switch {
	case errors.As(err, &pg) && pg.Code == "23505":
		return nil, httpx.WithReason(http.StatusConflict, fmt.Sprintf("session %s exists", n.id), "exists")
	case errors.As(err, &pg) && pg.Code == "23503":
		return nil, httpx.WithReason(http.StatusNotFound, "the parent session is gone", "parent_gone")
	case err != nil:
		return nil, err
	case refused == "rate_limited":
		return nil, httpx.WithReason(http.StatusTooManyRequests,
			"the owner has started too many sessions in the window", "rate_limited")
	case refused == "forgotten":
		return nil, httpx.WithReason(http.StatusNotFound, "the parent session was forgotten", "forgotten")
	}
	row, err := Get(r.Context(), s.Pool, n.id)
	if err != nil {
		return nil, err
	}
	return one{row}, nil
}

// browserOf is the session's browser options with the absent ones left out; nil for none.
func browserOf(b *crud.Body) []byte {
	type viewport struct {
		Width  int64 `json:"width"`
		Height int64 `json:"height"`
	}
	out := struct {
		UserAgent *string   `json:"user_agent,omitempty"`
		Viewport  *viewport `json:"viewport,omitempty"`
		Locale    *string   `json:"locale,omitempty"`
	}{
		UserAgent: b.Get("user_agent").OptString(0, 1<<30, uaPattern),
	}
	if v := b.Get("viewport").OptObject("width", "height"); v != nil {
		out.Viewport = &viewport{
			v.Get("width").Int(viewportWMin, viewportWMax, true),
			v.Get("height").Int(viewportHMin, viewportHMax, true),
		}
		v.Close()
	}
	out.Locale = b.Get("locale").OptString(0, browserLocaleMx, localePattern)
	if out.UserAgent == nil && out.Viewport == nil && out.Locale == nil {
		return nil
	}
	raw, _ := json.Marshal(out)
	return raw
}

func (s *Sessions) insert(ctx context.Context, n newSession) (refused string, err error) {
	err = pgx.BeginFunc(ctx, s.Pool, func(tx pgx.Tx) error {
		if n.limit != nil {
			// Per owner, held until commit, so concurrent starts count each other.
			if _, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock(hashtextextended($1, 0))`,
				"ai_sessions.start:"+n.owner.kind+":"+n.owner.id); err != nil {
				return err
			}
			count, err := recent(ctx, tx, n.owner.kind, n.owner.id, n.limit[1])
			if err != nil {
				return err
			}
			if count >= n.limit[0] {
				refused = "rate_limited"
				return nil
			}
		}
		if n.forkParent != nil {
			// A forget of the parent either went first, and its tombstone refuses the fork
			// here, or waits for this to commit and then finds the fork to copy its range into.
			subject := "session-" + *n.forkParent
			if _, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock_shared(hashtext($1))`,
				PayloadKeyLock(subject)); err != nil {
				return err
			}
			var one int
			err := tx.QueryRow(ctx, `SELECT 1 FROM ai_forgotten_subjects WHERE subject = $1`, subject).Scan(&one)
			if err == nil {
				refused = "forgotten"
				return nil
			}
			if !errors.Is(err, pgx.ErrNoRows) {
				return err
			}
		}
		var browser any
		if n.browser != nil {
			browser = string(n.browser)
		}
		_, err := tx.Exec(ctx, `INSERT INTO ai_sessions (id, origin, owner_kind, owner_id, owner_label,
			creator_kind, creator_id, status, title, tags, scope, parent_id, runtime, max_turns, budget_usd,
			budget_root_id, fork_parent_id, fork_cut, background_tools, browser)
			VALUES ($1::uuid, $2, $3, $4, $5, $3, $4, 'idle',
			$6, $7::text[], $8::jsonb, $9::uuid, $10, $11::int, $12::float8,
			(SELECT coalesce(budget_root_id, id) FROM ai_sessions WHERE id = $13::uuid),
			$14::uuid, $15::int,
			CASE WHEN $16::boolean IS NOT NULL THEN $16::boolean
			WHEN $9::uuid IS NOT NULL THEN coalesce((SELECT background_tools
			FROM ai_sessions WHERE id = $9::uuid), true) ELSE true END,
			CASE WHEN $17::jsonb IS NOT NULL THEN $17::jsonb
			WHEN $9::uuid IS NOT NULL THEN (SELECT browser FROM ai_sessions
			WHERE id = $9::uuid) END)`,
			n.id, n.origin, n.owner.kind, n.owner.id, n.owner.label, n.title, n.tags, string(n.scope),
			n.parent, n.runtime, n.maxTurns, n.budget, n.budgetOf, n.forkParent, n.forkCut,
			n.background, browser)
		return err
	})
	return refused, err
}

// PayloadKeyLock is the advisory lock key a payload key's creation and a forget take
// (workflows/payload_codec.py payload_key_lock).
func PayloadKeyLock(subject string) string { return "scadbuddy:ai_payload_keys:" + subject }

type ok struct {
	OK bool `json:"ok"`
}

type changed struct {
	Changed bool `json:"changed"`
}

func (s *Sessions) setAgent(r *http.Request, p crud.Params) (any, error) {
	session, err := p.UUID("session_id")
	if err != nil {
		return nil, err
	}
	body, err := crud.ReadBody(r, "model", "effort", "type")
	if err != nil {
		return nil, err
	}
	model := body.Get("model").OptString(0, 1<<30, modelPattern)
	effort := body.Get("effort").OptEnum("low", "medium", "high", "xhigh", "max")
	kind := body.Get("type").OptString(0, 1<<30, typePattern)
	if err := body.Err(); err != nil {
		return nil, err
	}
	_, err = s.Pool.Exec(r.Context(),
		`UPDATE ai_sessions SET agent_model = $1, agent_effort = $2, agent_type = $3 WHERE id = $4`,
		model, effort, kind, session)
	if err != nil {
		return nil, err
	}
	return ok{true}, nil
}

func (s *Sessions) offer(r *http.Request, p crud.Params) (any, error) {
	session, err := p.UUID("session_id")
	if err != nil {
		return nil, err
	}
	body, err := crud.ReadBody(r, "owner", "to", "ttl_ms")
	if err != nil {
		return nil, err
	}
	kind, id := who(body.Get("owner").Object("kind", "id"))
	to := ownerOf(body.Get("to").Object("kind", "id", "label"))
	ttl := body.Get("ttl_ms").Int(1, offerTTLMsMax, false)
	if err := body.Err(); err != nil {
		return nil, err
	}
	tag, err := s.Pool.Exec(r.Context(), `UPDATE ai_sessions SET pending_owner_kind = $1,
		pending_owner_id = $2, pending_owner_label = $3,
		pending_owner_until = now() + ($4::bigint * interval '1 millisecond'), updated_at = now()
		WHERE id = $5 AND owner_kind = $6 AND owner_id = $7`,
		to.kind, to.id, to.label, ttl, session, kind, id)
	if err != nil {
		return nil, err
	}
	return changed{tag.RowsAffected() > 0}, nil
}

func (s *Sessions) cancelOffer(r *http.Request, p crud.Params) (any, error) {
	session, err := p.UUID("session_id")
	if err != nil {
		return nil, err
	}
	body, err := crud.ReadBody(r, "to")
	if err != nil {
		return nil, err
	}
	kind, id := who(body.Get("to").Object("kind", "id"))
	if err := body.Err(); err != nil {
		return nil, err
	}
	tag, err := s.Pool.Exec(r.Context(), `UPDATE ai_sessions SET pending_owner_kind = NULL,
		pending_owner_id = NULL, pending_owner_label = NULL, pending_owner_until = NULL, updated_at = now()
		WHERE id = $1 AND pending_owner_kind = $2 AND pending_owner_id = $3 AND `+liveOffer,
		session, kind, id)
	if err != nil {
		return nil, err
	}
	return changed{tag.RowsAffected() > 0}, nil
}

func (s *Sessions) transfer(r *http.Request, p crud.Params) (any, error) {
	session, err := p.UUID("session_id")
	if err != nil {
		return nil, err
	}
	body, err := crud.ReadBody(r, "owner", "to", "accepting")
	if err != nil {
		return nil, err
	}
	kind, id := who(body.Get("owner").Object("kind", "id"))
	to := ownerOf(body.Get("to").Object("kind", "id", "label"))
	accepting := body.Get("accepting").BoolOr(false)
	if err := body.Err(); err != nil {
		return nil, err
	}
	offerStill := ""
	if accepting {
		offerStill = " AND pending_owner_kind = $1 AND pending_owner_id = $2 AND " + liveOffer
	}
	tag, err := s.Pool.Exec(r.Context(), `UPDATE ai_sessions SET owner_kind = $1, owner_id = $2,
		owner_label = $3, pending_owner_kind = NULL, pending_owner_id = NULL,
		pending_owner_label = NULL, pending_owner_until = NULL, updated_at = now()
		WHERE id = $4 AND owner_kind = $5 AND owner_id = $6`+offerStill,
		to.kind, to.id, to.label, session, kind, id)
	if err != nil {
		return nil, err
	}
	return changed{tag.RowsAffected() > 0}, nil
}

func (s *Sessions) raiseBudget(r *http.Request, p crud.Params) (any, error) {
	session, err := p.UUID("session_id")
	if err != nil {
		return nil, err
	}
	body, err := crud.ReadBody(r, "by", "add_usd", "max_usd")
	if err != nil {
		return nil, err
	}
	kind, id := who(body.Get("by").Object("kind", "id"))
	add := body.Get("add_usd").Float(0, false, budgetMaxUSD)
	most := body.Get("max_usd").Float(0, false, budgetMaxUSD)
	if err := body.Err(); err != nil {
		return nil, err
	}
	var after *float64
	err = s.Pool.QueryRow(r.Context(), `UPDATE ai_sessions r
		SET budget_usd = round((r.budget_usd + $1::float8)::numeric, 2)::double precision, updated_at = now()
		FROM ai_sessions s
		WHERE s.id = $2 AND s.owner_kind = $3
		AND (s.owner_id = $4 OR s.owner_kind = 'browser')
		AND r.id = coalesce(s.budget_root_id, s.id)
		AND round((r.budget_usd + $1::float8)::numeric, 2) <= $5::float8
		RETURNING r.budget_usd`, add, session, kind, id, most).Scan(&after)
	if err != nil && !errors.Is(err, pgx.ErrNoRows) {
		return nil, err
	}
	return struct {
		BudgetUSD *float64 `json:"budget_usd"`
	}{after}, nil
}

// discard is a fork that failed before it was announced: its own transcript rows, its row
// (whose cascade takes the rest) and its payload key, under the key's lock. Only a session
// that has logged no event.
func (s *Sessions) discard(r *http.Request, p crud.Params) (any, error) {
	session, err := p.UUID("session_id")
	if err != nil {
		return nil, err
	}
	discarded := false
	ctx := r.Context()
	err = pgx.BeginFunc(ctx, s.Pool, func(tx pgx.Tx) error {
		// The lock a key's creation takes, held over the row's delete.
		subject := "session-" + session
		if _, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock(hashtext($1))`, PayloadKeyLock(subject)); err != nil {
			return err
		}
		var one int
		err := tx.QueryRow(ctx,
			`SELECT 1 FROM ai_sessions WHERE id = $1 AND event_seq = 0 FOR UPDATE`, session).Scan(&one)
		if errors.Is(err, pgx.ErrNoRows) {
			return nil
		}
		if err != nil {
			return err
		}
		for _, statement := range []string{
			`DELETE FROM ai_session_entries WHERE session_id = $1`,
			`DELETE FROM ai_sessions WHERE id = $1`,
		} {
			if _, err := tx.Exec(ctx, statement, session); err != nil {
				return err
			}
		}
		if _, err := tx.Exec(ctx, `DELETE FROM ai_payload_keys WHERE subject = $1`, subject); err != nil {
			return err
		}
		discarded = true
		return nil
	})
	if err != nil {
		return nil, err
	}
	return struct {
		Discarded bool `json:"discarded"`
	}{discarded}, nil
}

type touch struct {
	tool, resourceType, action           string
	resourceID, modelSlug, before, after *string
}

// nullable is a required member that may be null, at most most characters.
func nullable(v crud.Value, most int) *string {
	if !v.Given() {
		v.String(0, most, nil) // its "Field required"
		return nil
	}
	return v.OptString(0, most, nil)
}

func (s *Sessions) recordResources(r *http.Request, p crud.Params) (any, error) {
	session, err := p.UUID("session_id")
	if err != nil {
		return nil, err
	}
	body, err := crud.ReadBody(r, "rows")
	if err != nil {
		return nil, err
	}
	var rows []touch
	for _, item := range body.Get("rows").List(1, resourcesMax) {
		o := item.Object("tool", "resource_type", "resource_id", "action", "model_slug", "before_id", "after_id")
		if o == nil {
			continue
		}
		rows = append(rows, touch{
			tool:         o.Get("tool").String(1, toolMax, nil),
			resourceType: o.Get("resource_type").Enum(resourceTypes...),
			resourceID:   nullable(o.Get("resource_id"), resourceIDMax),
			action:       o.Get("action").Enum("created", "modified", "deleted"),
			modelSlug:    nullable(o.Get("model_slug"), resourceIDMax),
			before:       nullable(o.Get("before_id"), resourceIDMax),
			after:        nullable(o.Get("after_id"), resourceIDMax),
		})
		o.Close()
	}
	if err := body.Err(); err != nil {
		return nil, err
	}
	for _, row := range rows {
		if (row.resourceType == "unclassified") != (row.resourceID == nil) {
			return nil, crud.Invalid("an unclassified row has no id, and every other row has one")
		}
	}
	recorded := false
	ctx := r.Context()
	err = pgx.BeginFunc(ctx, s.Pool, func(tx pgx.Tx) error {
		var one int
		err := tx.QueryRow(ctx, `SELECT 1 FROM ai_sessions WHERE id = $1 FOR KEY SHARE`, session).Scan(&one)
		if errors.Is(err, pgx.ErrNoRows) {
			return nil
		}
		if err != nil {
			return err
		}
		for _, row := range rows {
			if _, err := tx.Exec(ctx, `INSERT INTO ai_session_resources (session_id, tool, resource_type,
				resource_id, action, model_slug, before_id, after_id)
				VALUES ($1, $2, $3, $4, $5, $6, $7, $8)`,
				session, row.tool, row.resourceType, row.resourceID, row.action, row.modelSlug,
				row.before, row.after); err != nil {
				return err
			}
		}
		recorded = true
		return nil
	})
	if err != nil {
		return nil, err
	}
	return struct {
		Recorded bool `json:"recorded"`
	}{recorded}, nil
}

type touched struct {
	ResourceType string  `json:"resource_type"`
	ResourceID   *string `json:"resource_id"`
	Action       string  `json:"action"`
	ModelSlug    *string `json:"model_slug"`
	BeforeID     *string `json:"before_id"`
	AfterID      *string `json:"after_id"`
	Tool         string  `json:"tool"`
	At           string  `json:"at"`
}

func (s *Sessions) listResources(r *http.Request, p crud.Params) (any, error) {
	session, err := p.UUID("session_id")
	if err != nil {
		return nil, err
	}
	rows, err := s.Pool.Query(r.Context(), `SELECT resource_type, resource_id, action, model_slug,
		before_id, after_id, tool, `+iso("at", "at")+`
		FROM ai_session_resources WHERE session_id = $1 ORDER BY id`, session)
	if err != nil {
		return nil, err
	}
	out, err := pgx.CollectRows(rows, pgx.RowToStructByPos[touched])
	if err != nil {
		return nil, err
	}
	if out == nil {
		out = []touched{}
	}
	return struct {
		Resources []touched `json:"resources"`
	}{out}, nil
}
