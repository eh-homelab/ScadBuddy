// Package stores is the agent's small stores: backend/scadbuddy/api/crud_agent_stores.py,
// statement for statement (areas activity-kinds, browser-origins, bridge-tabs, pairings and
// mcp-tokens). Each route runs in one transaction, as Python's _in_tx.
package stores

import (
	"context"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"regexp"
	"strconv"
	"strings"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/seal"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/wire"
)

const (
	tabIDMax       = 200
	queueMax       = 300
	originMax      = 2048
	tabsMax        = 500
	approvedListed = 200
	// tabImageBytesMax is sessions/images.ts IMAGE_BYTES_MAX.
	tabImageBytesMax = 5 * 1024 * 1024 / 4 * 3
	tabImageTTL      = "600000 milliseconds"
	// The pairing policy (bridge/pairings.ts, spec §8.5).
	pairingRequestTTL          = "300000 milliseconds"
	pairingPairedTTL           = "28800000 milliseconds"
	pairingMaxAttempts         = 5
	pairingMaxPendingPrincipal = 3
	pairingMaxPending          = 20
	pairingRequestLock         = "scadbuddy.ai_browser_pairings.request"
	pairingRefused             = "pairing-refused"
	activityKindsMax           = 500
	labelMax                   = 300
	fallbackLabelMax           = 600
	principalIDMax             = 500
	tokenNameMax               = 200
	claimedBeforeMax           = 64
	facetsMax                  = 50
	activityKeyLeast, keyMost  = 7, 200
	facetNameMost              = 100
	bodyBytesMax               = 8 * 1024 * 1024 // api/limits.py MAX_TEXT_BODY_BYTES
)

var (
	originPattern  = regexp.MustCompile(`^https?://[^/?#@\s]+$`)
	tabImageName   = regexp.MustCompile(`^[0-9a-f]{64}\.(png|jpg|gif|webp)$`)
	sha256Hex      = regexp.MustCompile(`^[0-9a-f]{64}$`)
	activityKey    = regexp.MustCompile(`^agent\.[A-Za-z0-9_.-]+$`)
	tabImageTypes  = map[string]string{"png": "image/png", "jpg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}
	scopes         = []string{"once", "session"}
	principalKinds = []string{"bearer", "oidc", "anonymous", "flow"}
	tiers          = []string{"read", "write", "outward"}
	facetTypes     = []string{"keyword", "number", "bool", "time"}
	okAnswer       = struct {
		Ok bool `json:"ok"`
	}{true}
)

// Stores is the areas' routes.
type Stores struct{ Pool *pgxpool.Pool }

type done struct {
	Done bool `json:"done"`
}

// Areas are the package's routes by area name.
func Areas(pool *pgxpool.Pool, _ *seal.Keys) map[string][]crud.Route {
	s := &Stores{Pool: pool}
	return map[string][]crud.Route{
		"activity-kinds":       s.ActivityKinds(),
		"browser-origins:read": s.BrowserOriginReads(),
		"browser-origins":      s.BrowserOrigins(),
		"bridge-tabs:read":     s.BridgeTabReads(),
		"bridge-tabs":          s.BridgeTabs(),
		"pairings:read":        s.PairingReads(),
		"pairings":             s.Pairings(),
		"mcp-tokens:read":      s.TokenReads(),
		"mcp-tokens":           s.Tokens(),
	}
}

// ActivityKinds is area activity-kinds.
func (s *Stores) ActivityKinds() []crud.Route {
	return []crud.Route{{Method: "POST", Path: "/activity-kinds", Handle: s.activityKinds, MaxBody: bodyBytesMax}}
}

// BrowserOriginReads are area browser-origins' GETs.
func (s *Stores) BrowserOriginReads() []crud.Route {
	return []crud.Route{
		{Method: "GET", Path: "/browser-origins", Handle: s.browserOrigins},
		{Method: "GET", Path: "/browser-origins/approved", Handle: s.approved},
	}
}

// BrowserOrigins is area browser-origins.
func (s *Stores) BrowserOrigins() []crud.Route {
	return append(s.BrowserOriginReads(),
		crud.Route{Method: "POST", Path: "/browser-origins/approve", Handle: s.approve, MaxBody: bodyBytesMax},
		crud.Route{Method: "POST", Path: "/browser-origins/revoke", Handle: s.revokeOrigin, MaxBody: bodyBytesMax},
	)
}

// BridgeTabReads are area bridge-tabs' GETs.
func (s *Stores) BridgeTabReads() []crud.Route {
	return []crud.Route{
		{Method: "GET", Path: "/session-tabs", Handle: s.sessionTab},
		{Method: "GET", Path: "/tab-owners", Handle: s.tabOwner},
	}
}

// BridgeTabs is area bridge-tabs.
func (s *Stores) BridgeTabs() []crud.Route {
	return append(s.BridgeTabReads(),
		crud.Route{Method: "POST", Path: "/session-tabs", Handle: s.sessionTabWrite, MaxBody: bodyBytesMax},
		crud.Route{Method: "POST", Path: "/tab-owners/claim", Handle: s.tabClaim, MaxBody: bodyBytesMax},
		crud.Route{Method: "POST", Path: "/tab-owners/refresh", Handle: s.tabRefresh, MaxBody: bodyBytesMax},
		crud.Route{Method: "POST", Path: "/tab-owners/release", Handle: s.tabRelease, MaxBody: bodyBytesMax},
		crud.Route{Method: "POST", Path: "/tab-owners/release-all", Handle: s.tabReleaseAll, MaxBody: bodyBytesMax},
		crud.Route{Method: "PUT", Path: "/tab-images/{name}", Handle: s.tabImagePut, MaxBody: bodyBytesMax, Status: http.StatusNoContent},
		crud.Route{Method: "POST", Path: "/tab-images/{name}/take", Handle: s.tabImageTake},
	)
}

// PairingReads are area pairings' GETs.
func (s *Stores) PairingReads() []crud.Route {
	return []crud.Route{
		{Method: "GET", Path: "/pairings/paired-tab", Handle: s.pairedTab},
		{Method: "GET", Path: "/pairings/pending", Handle: s.pending},
	}
}

// Pairings is area pairings.
func (s *Stores) Pairings() []crud.Route {
	return append(s.PairingReads(),
		crud.Route{Method: "POST", Path: "/pairings", Handle: s.pairingRequest, MaxBody: bodyBytesMax, Status: http.StatusCreated},
		crud.Route{Method: "POST", Path: "/pairings/paired-with", Handle: s.pairedWith, MaxBody: bodyBytesMax},
		crud.Route{Method: "POST", Path: "/pairings/{pairing_id}/accept", Handle: s.accept, MaxBody: bodyBytesMax},
		crud.Route{Method: "POST", Path: "/pairings/{pairing_id}/deny", Handle: s.deny},
		crud.Route{Method: "POST", Path: "/pairings/{pairing_id}/end", Handle: s.end, MaxBody: bodyBytesMax},
	)
}

// TokenReads are area mcp-tokens' GETs.
func (s *Stores) TokenReads() []crud.Route {
	return []crud.Route{
		{Method: "GET", Path: "/mcp-tokens", Handle: s.tokens},
		{Method: "GET", Path: "/mcp-tokens/{token_id}/live", Handle: s.tokenLive},
	}
}

// Tokens is area mcp-tokens.
func (s *Stores) Tokens() []crud.Route {
	return append(s.TokenReads(),
		crud.Route{Method: "POST", Path: "/mcp-tokens/verify", Handle: s.verify, MaxBody: bodyBytesMax},
		crud.Route{Method: "POST", Path: "/mcp-tokens", Handle: s.mint, MaxBody: bodyBytesMax, Status: http.StatusCreated},
		crud.Route{Method: "POST", Path: "/mcp-tokens/{token_id}/revoke", Handle: s.revokeToken},
	)
}

// inTx runs fn in one transaction.
func (s *Stores) inTx(ctx context.Context, fn func(pgx.Tx) error) error {
	return pgx.BeginFunc(ctx, s.Pool, fn)
}

// returned says whether a statement's RETURNING gave a row.
func returned(ctx context.Context, tx pgx.Tx, sql string, args ...any) (bool, error) {
	var one any
	err := tx.QueryRow(ctx, sql, args...).Scan(&one)
	if errors.Is(err, pgx.ErrNoRows) {
		return false, nil
	}
	return err == nil, err
}

func pgCode(err error) string {
	var pg *pgconn.PgError
	if errors.As(err, &pg) {
		return pg.Code
	}
	return ""
}

// -- activity kinds -------------------------------------------------------------------------

type facet struct {
	Name  string `json:"name"`
	Type  string `json:"type"`
	Label string `json:"label"`
}

func (s *Stores) activityKinds(r *http.Request, _ crud.Params) (any, error) {
	body, err := crud.ReadBody(r, "kinds")
	if err != nil {
		return nil, err
	}
	type kind struct {
		key, label string
		facets     []facet
	}
	var kinds []kind
	for _, k := range body.Objects("kinds", false, activityKindsMax, "key", "label", "facets") {
		one := kind{
			key:    k.Match("key", activityKeyLeast, keyMost, activityKey),
			label:  k.Text("label", 0, labelMax),
			facets: []facet{},
		}
		for _, f := range k.Objects("facets", true, facetsMax, "name", "type", "label") {
			one.facets = append(one.facets, facet{
				Name: f.Text("name", 1, facetNameMost), Type: f.Choice("type", facetTypes...), Label: f.Text("label", 0, labelMax),
			})
			f.Done()
		}
		k.Done()
		kinds = append(kinds, one)
	}
	if err := body.Err(); err != nil {
		return nil, err
	}
	keys := make([]string, len(kinds))
	err = s.inTx(r.Context(), func(tx pgx.Tx) error {
		for i, k := range kinds {
			keys[i] = k.key
			facets, err := json.Marshal(k.facets)
			if err != nil {
				return err
			}
			if _, err := tx.Exec(r.Context(),
				`INSERT INTO ai_activity_kinds (key, label, facets) VALUES ($1, $2, $3::jsonb)
				 ON CONFLICT (key) DO UPDATE SET label = EXCLUDED.label,
				 facets = EXCLUDED.facets, retired_at = NULL, updated_at = now()`,
				k.key, k.label, string(facets)); err != nil {
				return err
			}
		}
		_, err := tx.Exec(r.Context(),
			`UPDATE ai_activity_kinds SET retired_at = now() WHERE retired_at IS NULL AND key <> ALL($1)`, keys)
		return err
	})
	if err != nil {
		return nil, err
	}
	return okAnswer, nil
}

// -- browser origins ------------------------------------------------------------------------

type approvedOrigin struct {
	Origin string `json:"origin"`
	Scope  string `json:"scope"`
}

func (s *Stores) browserOrigins(r *http.Request, _ crud.Params) (any, error) {
	session, err := crud.QueryUUID(r, "session_id", false)
	if err != nil {
		return nil, err
	}
	rows, err := s.Pool.Query(r.Context(),
		`SELECT origin, scope FROM ai_browser_session_origins WHERE session_id = $1::text::uuid`, *session)
	if err != nil {
		return nil, err
	}
	origins, err := pgx.CollectRows(rows, pgx.RowToStructByPos[approvedOrigin])
	if err != nil {
		return nil, err
	}
	return struct {
		Origins []approvedOrigin `json:"origins"`
	}{nonNil(origins)}, nil
}

func (s *Stores) approve(r *http.Request, _ crud.Params) (any, error) {
	body, err := crud.ReadBody(r, "session_id", "origin", "scope")
	if err != nil {
		return nil, err
	}
	session := body.UUID("session_id")
	origin := body.Match("origin", 8, originMax, originPattern)
	scope := body.Choice("scope", scopes...)
	if err := body.Err(); err != nil {
		return nil, err
	}
	var written bool
	err = s.inTx(r.Context(), func(tx pgx.Tx) (err error) {
		written, err = returned(r.Context(), tx,
			`INSERT INTO ai_browser_session_origins (session_id, origin, scope)
			 SELECT id, $1, $2 FROM ai_sessions WHERE id = $3::text::uuid
			 ON CONFLICT (session_id, origin) DO UPDATE
			 SET scope = CASE WHEN ai_browser_session_origins.scope = 'session'
			 THEN 'session' ELSE EXCLUDED.scope END, approved_at = now()
			 RETURNING origin`, origin, scope, session)
		return err
	})
	if err != nil {
		return nil, err
	}
	return done{written}, nil
}

func (s *Stores) revokeOrigin(r *http.Request, _ crud.Params) (any, error) {
	body, err := crud.ReadBody(r, "session_id", "origin")
	if err != nil {
		return nil, err
	}
	session := body.UUID("session_id")
	origin := body.Match("origin", 8, originMax, originPattern)
	if err := body.Err(); err != nil {
		return nil, err
	}
	var gone bool
	err = s.inTx(r.Context(), func(tx pgx.Tx) (err error) {
		gone, err = returned(r.Context(), tx,
			`DELETE FROM ai_browser_session_origins WHERE session_id = $1::text::uuid AND origin = $2
			 RETURNING origin`, session, origin)
		return err
	})
	if err != nil {
		return nil, err
	}
	return done{gone}, nil
}

type approvedRow struct {
	SessionID  string    `json:"session_id"`
	Title      string    `json:"title"`
	Origin     string    `json:"origin"`
	Scope      string    `json:"scope"`
	ApprovedAt wire.Time `json:"approved_at"`
}

func (s *Stores) approved(r *http.Request, _ crud.Params) (any, error) {
	session, err := crud.QueryUUID(r, "session_id", true)
	if err != nil {
		return nil, err
	}
	rows, err := s.Pool.Query(r.Context(),
		`SELECT o.session_id::text AS session_id, s.title, o.origin, o.scope, o.approved_at
		 FROM ai_browser_session_origins o JOIN ai_sessions s ON s.id = o.session_id
		 WHERE ($1::text::uuid IS NULL OR o.session_id = $1::text::uuid)
		 ORDER BY o.approved_at DESC, o.origin LIMIT $2`, session, approvedListed)
	if err != nil {
		return nil, err
	}
	list, err := pgx.CollectRows(rows, pgx.RowToStructByPos[approvedRow])
	if err != nil {
		return nil, err
	}
	return struct {
		Approved []approvedRow `json:"approved"`
	}{nonNil(list)}, nil
}

// -- session tabs, tab owners, tab images (area bridge-tabs) ---------------------------------

func (s *Stores) sessionTabWrite(r *http.Request, _ crud.Params) (any, error) {
	body, err := crud.ReadBody(r, "session_id", "tab_id", "conditional", "seen")
	if err != nil {
		return nil, err
	}
	session := body.UUID("session_id")
	tab := body.Text("tab_id", 1, tabIDMax)
	conditional := body.Bool("conditional", false)
	seen := body.OptText("seen", 1, tabIDMax, nil)
	if err := body.Err(); err != nil {
		return nil, err
	}
	sql := `INSERT INTO ai_session_tabs (session_id, tab_id) VALUES ($1::text::uuid, $2)
		ON CONFLICT (session_id) DO UPDATE SET tab_id = EXCLUDED.tab_id,
		paired_at = now() RETURNING 1`
	args := []any{session, tab}
	switch {
	case !conditional:
	case seen == nil:
		sql = `INSERT INTO ai_session_tabs (session_id, tab_id) VALUES ($1::text::uuid, $2)
			ON CONFLICT (session_id) DO NOTHING RETURNING 1`
	default:
		sql = `INSERT INTO ai_session_tabs (session_id, tab_id) VALUES ($1::text::uuid, $2)
			ON CONFLICT (session_id) DO UPDATE SET tab_id = EXCLUDED.tab_id,
			paired_at = now() WHERE ai_session_tabs.tab_id = $3 RETURNING 1`
		args = append(args, *seen)
	}
	var written bool
	err = s.inTx(r.Context(), func(tx pgx.Tx) (err error) {
		written, err = returned(r.Context(), tx, sql, args...)
		return err
	})
	if pgCode(err) == "23503" {
		return nil, &httpx.Problem{Status: http.StatusNotFound, Detail: "no such session"}
	}
	if err != nil {
		return nil, err
	}
	return struct {
		Written bool `json:"written"`
	}{written}, nil
}

func (s *Stores) sessionTab(r *http.Request, _ crud.Params) (any, error) {
	session, err := crud.QueryUUID(r, "session_id", false)
	if err != nil {
		return nil, err
	}
	var tab *string
	err = s.Pool.QueryRow(r.Context(),
		`SELECT tab_id FROM ai_session_tabs WHERE session_id = $1::text::uuid`, *session).Scan(&tab)
	if err != nil && !errors.Is(err, pgx.ErrNoRows) {
		return nil, err
	}
	return struct {
		TabID *string `json:"tab_id"`
	}{tab}, nil
}

func (s *Stores) tabClaim(r *http.Request, _ crud.Params) (any, error) {
	body, err := crud.ReadBody(r, "tab_id", "queue")
	if err != nil {
		return nil, err
	}
	tab, queue := body.Text("tab_id", 1, tabIDMax), body.Text("queue", 1, queueMax)
	if err := body.Err(); err != nil {
		return nil, err
	}
	err = s.inTx(r.Context(), func(tx pgx.Tx) error {
		_, err := tx.Exec(r.Context(), `INSERT INTO ai_tab_owners (tab_id, queue) VALUES ($1, $2)
			ON CONFLICT (tab_id) DO UPDATE SET queue = EXCLUDED.queue, claimed_at = now()`, tab, queue)
		return err
	})
	if err != nil {
		return nil, err
	}
	return okAnswer, nil
}

func (s *Stores) tabRefresh(r *http.Request, _ crud.Params) (any, error) {
	body, err := crud.ReadBody(r, "tab_ids", "queue")
	if err != nil {
		return nil, err
	}
	tabs := body.Strings("tab_ids", false, 0, tabsMax, 1, tabIDMax)
	queue := body.Text("queue", 1, queueMax)
	if err := body.Err(); err != nil {
		return nil, err
	}
	if len(tabs) == 0 {
		return okAnswer, nil
	}
	err = s.inTx(r.Context(), func(tx pgx.Tx) error {
		_, err := tx.Exec(r.Context(), `INSERT INTO ai_tab_owners (tab_id, queue)
			SELECT tab_id, $1 FROM unnest($2::text[]) AS t (tab_id)
			ON CONFLICT (tab_id) DO UPDATE SET claimed_at = now()
			WHERE ai_tab_owners.queue = EXCLUDED.queue`, queue, tabs)
		return err
	})
	if err != nil {
		return nil, err
	}
	return okAnswer, nil
}

func (s *Stores) tabRelease(r *http.Request, _ crud.Params) (any, error) {
	body, err := crud.ReadBody(r, "tab_id", "queue", "claimed_before")
	if err != nil {
		return nil, err
	}
	tab, queue := body.Text("tab_id", 1, tabIDMax), body.Text("queue", 1, queueMax)
	before := body.OptText("claimed_before", 0, claimedBeforeMax, nil)
	if err := body.Err(); err != nil {
		return nil, err
	}
	err = s.inTx(r.Context(), func(tx pgx.Tx) error {
		if before == nil {
			_, err := tx.Exec(r.Context(),
				`DELETE FROM ai_tab_owners WHERE tab_id = $1 AND queue = $2`, tab, queue)
			return err
		}
		_, err := tx.Exec(r.Context(), `DELETE FROM ai_tab_owners WHERE tab_id = $1 AND queue = $2
			AND claimed_at <= $3::text::timestamptz`, tab, queue, *before)
		return err
	})
	// psycopg's DataError is SQLSTATE class 22.
	if strings.HasPrefix(pgCode(err), "22") {
		return nil, &httpx.Problem{Status: http.StatusUnprocessableEntity, Detail: "claimed_before is not a timestamp"}
	}
	if err != nil {
		return nil, err
	}
	return okAnswer, nil
}

func (s *Stores) tabReleaseAll(r *http.Request, _ crud.Params) (any, error) {
	body, err := crud.ReadBody(r, "queue")
	if err != nil {
		return nil, err
	}
	queue := body.Text("queue", 1, queueMax)
	if err := body.Err(); err != nil {
		return nil, err
	}
	err = s.inTx(r.Context(), func(tx pgx.Tx) error {
		_, err := tx.Exec(r.Context(), `DELETE FROM ai_tab_owners WHERE queue = $1`, queue)
		return err
	})
	if err != nil {
		return nil, err
	}
	return okAnswer, nil
}

type tabOwner struct {
	Queue string `json:"queue"`
	// ClaimedAt is Postgres's own text, to the microsecond.
	ClaimedAt string `json:"claimed_at"`
}

func (s *Stores) tabOwner(r *http.Request, _ crud.Params) (any, error) {
	tab, err := crud.QueryText(r, "tab_id", tabIDMax)
	if err != nil {
		return nil, err
	}
	rows, err := s.Pool.Query(r.Context(),
		`SELECT queue, claimed_at::text AS claimed_at FROM ai_tab_owners WHERE tab_id = $1`, tab)
	if err != nil {
		return nil, err
	}
	owner, err := pgx.CollectExactlyOneRow(rows, pgx.RowToAddrOfStructByPos[tabOwner])
	if err != nil && !errors.Is(err, pgx.ErrNoRows) {
		return nil, err
	}
	return struct {
		Owner *tabOwner `json:"owner"`
	}{owner}, nil
}

func (s *Stores) tabImagePut(r *http.Request, p crud.Params) (any, error) {
	name, err := p.Match("name", tabImageName)
	if err != nil {
		return nil, err
	}
	ext := name[strings.LastIndexByte(name, '.')+1:]
	mediaType := tabImageTypes[ext]
	if got, _, _ := strings.Cut(r.Header.Get("Content-Type"), ";"); strings.TrimSpace(got) != mediaType {
		return nil, &httpx.Problem{Status: http.StatusUnprocessableEntity,
			Detail: fmt.Sprintf("a .%s image is sent as %s", ext, mediaType)}
	}
	tooLarge := &httpx.Problem{Status: http.StatusRequestEntityTooLarge,
		Detail: "the image is larger than a tab image may be"}
	if declared := r.Header.Get("Content-Length"); declared != "" {
		if n, err := strconv.ParseUint(declared, 10, 64); err == nil && n > tabImageBytesMax {
			return nil, tooLarge
		}
	}
	data, err := io.ReadAll(io.LimitReader(r.Body, tabImageBytesMax+1))
	if err != nil {
		return nil, err
	}
	if len(data) > tabImageBytesMax {
		return nil, tooLarge
	}
	sum := sha256.Sum256(data)
	if len(data) == 0 || hex.EncodeToString(sum[:]) != name[:64] {
		return nil, &httpx.Problem{Status: http.StatusUnprocessableEntity,
			Detail: "an image's name is its bytes' sha256"}
	}
	err = s.inTx(r.Context(), func(tx pgx.Tx) error {
		if _, err := tx.Exec(r.Context(),
			`DELETE FROM ai_tab_images WHERE stored_at < now() - $1::interval`, tabImageTTL); err != nil {
			return err
		}
		_, err := tx.Exec(r.Context(), `INSERT INTO ai_tab_images (name, media_type, bytes)
			VALUES ($1, $2, $3) ON CONFLICT (name) DO UPDATE SET stored_at = now()`, name, mediaType, data)
		return err
	})
	if err != nil {
		return nil, err
	}
	return nil, nil
}

func (s *Stores) tabImageTake(r *http.Request, p crud.Params) (any, error) {
	name, err := p.Match("name", tabImageName)
	if err != nil {
		return nil, err
	}
	var reply *crud.Reply
	err = s.inTx(r.Context(), func(tx pgx.Tx) error {
		rows, err := tx.Query(r.Context(),
			`DELETE FROM ai_tab_images WHERE name = $1 OR stored_at < now() - $2::interval
			 RETURNING media_type, bytes, (name = $1 AND stored_at >= now() - $2::interval) AS fresh`,
			name, tabImageTTL)
		if err != nil {
			return err
		}
		defer rows.Close()
		for rows.Next() {
			var mediaType string
			var data []byte
			var fresh bool
			if err := rows.Scan(&mediaType, &data, &fresh); err != nil {
				return err
			}
			if fresh && reply == nil {
				reply = &crud.Reply{Status: http.StatusOK, MediaType: mediaType, Body: data}
			}
		}
		return rows.Err()
	})
	if err != nil {
		return nil, err
	}
	if reply == nil {
		return &crud.Reply{Status: http.StatusNoContent}, nil
	}
	return reply, nil
}

// -- pairings -------------------------------------------------------------------------------

type pairing struct {
	ID        string    `json:"id"`
	Label     string    `json:"label"`
	ExpiresAt wire.Time `json:"expires_at"`
}

type pairedTab struct {
	ID        string    `json:"id"`
	Label     string    `json:"label"`
	ExpiresAt wire.Time `json:"expires_at"`
	TabID     string    `json:"tab_id"`
}

func (s *Stores) pairingRequest(r *http.Request, _ crud.Params) (any, error) {
	body, err := crud.ReadBody(r, "principal_kind", "principal_id", "fallback_label", "code_hash")
	if err != nil {
		return nil, err
	}
	kind := body.Choice("principal_kind", principalKinds...)
	principal := body.Text("principal_id", 1, principalIDMax)
	fallback := body.Text("fallback_label", 1, fallbackLabelMax)
	code := body.Match("code_hash", 0, 1<<30, sha256Hex)
	if err := body.Err(); err != nil {
		return nil, err
	}
	ctx := r.Context()
	// Rows past their use a day ago go, so the table stays small without a sweeper.
	err = s.inTx(ctx, func(tx pgx.Tx) error {
		_, err := tx.Exec(ctx, `DELETE FROM ai_browser_pairings WHERE expires_at < now() - interval '1 day'
			OR (status IN ('denied', 'ended') AND COALESCE(ended_at, created_at) < now() - interval '1 day')`)
		return err
	})
	if err != nil {
		return nil, err
	}
	var out pairing
	err = s.inTx(ctx, func(tx pgx.Tx) error {
		label, err := tokenLabel(ctx, tx, kind, principal, fallback)
		if err != nil {
			return err
		}
		// Count and insert under one transaction-scoped advisory lock, so two concurrent
		// requests cannot both read the counts below a cap and both insert (#731 review).
		if _, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock(hashtextextended($1, 0))`,
			pairingRequestLock); err != nil {
			return err
		}
		var mine, every int
		if err := tx.QueryRow(ctx, `SELECT count(*) FILTER (WHERE principal_kind = $1 AND principal_id = $2)::int
			AS mine, count(*)::int AS all FROM ai_browser_pairings
			WHERE status = 'pending' AND expires_at > now()`, kind, principal).Scan(&mine, &every); err != nil {
			return err
		}
		if mine >= pairingMaxPendingPrincipal {
			return httpx.WithReason(http.StatusConflict, fmt.Sprintf("this caller already has %d pairing requests"+
				" waiting; ask the user to answer one, or wait for them to expire", pairingMaxPendingPrincipal),
				pairingRefused)
		}
		if every >= pairingMaxPending {
			return httpx.WithReason(http.StatusConflict,
				"too many pairing requests are waiting for the user; try again in a few minutes", pairingRefused)
		}
		return tx.QueryRow(ctx, `INSERT INTO ai_browser_pairings
			(id, principal_kind, principal_id, principal_label, code_hash, status, expires_at)
			VALUES ($1::text::uuid, $2, $3, $4, $5, 'pending', now() + $6::interval)
			RETURNING id::text, principal_label, expires_at`,
			wire.UUID4(), kind, principal, label, code, pairingRequestTTL).Scan(&out.ID, &out.Label, &out.ExpiresAt)
	})
	if err != nil {
		return nil, err
	}
	return out, nil
}

// tokenLabel names a bearer principal after its token (token:<uuid>).
func tokenLabel(ctx context.Context, tx pgx.Tx, kind, principal, fallback string) (string, error) {
	rest, ok := strings.CutPrefix(principal, "token:")
	if kind != "bearer" || !ok {
		return fallback, nil
	}
	id, ok := wire.PyUUID(rest)
	if !ok {
		return fallback, nil
	}
	var name string
	err := tx.QueryRow(ctx, `SELECT name FROM ai_mcp_tokens WHERE id = $1::text::uuid`, id).Scan(&name)
	if errors.Is(err, pgx.ErrNoRows) {
		return fallback, nil
	}
	if err != nil {
		return "", err
	}
	return "MCP token “" + name + "”", nil
}

type accepted struct {
	Result        string   `json:"result"`
	Pairing       *pairing `json:"pairing"`
	PrincipalKind *string  `json:"principal_kind"`
	PrincipalID   *string  `json:"principal_id"`
	AttemptsLeft  *int     `json:"attempts_left"`
}

func (s *Stores) accept(r *http.Request, p crud.Params) (any, error) {
	id, err := p.UUID("pairing_id")
	if err != nil {
		return nil, err
	}
	body, err := crud.ReadBody(r, "code_hash", "tab_id")
	if err != nil {
		return nil, err
	}
	code := body.Match("code_hash", 0, 1<<30, sha256Hex)
	tab := body.Text("tab_id", 1, tabIDMax)
	if err := body.Err(); err != nil {
		return nil, err
	}
	ctx := r.Context()
	var out accepted
	err = s.inTx(ctx, func(tx pgx.Tx) error {
		var kind, principal, stored string
		var attempts int
		err := tx.QueryRow(ctx, `SELECT principal_kind, principal_id, code_hash, attempts
			FROM ai_browser_pairings
			WHERE id = $1::text::uuid AND status = 'pending' AND expires_at > now() FOR UPDATE`,
			id).Scan(&kind, &principal, &stored, &attempts)
		if errors.Is(err, pgx.ErrNoRows) {
			out = accepted{Result: "gone"}
			return nil
		}
		if err != nil {
			return err
		}
		if subtle.ConstantTimeCompare([]byte(code), []byte(stored)) != 1 {
			attempts++
			denied := attempts >= pairingMaxAttempts
			if _, err := tx.Exec(ctx, `UPDATE ai_browser_pairings SET attempts = $1,
				status = CASE WHEN $2 THEN 'denied' ELSE status END,
				ended_at = CASE WHEN $2 THEN now() ELSE ended_at END WHERE id = $3::text::uuid`,
				attempts, denied, id); err != nil {
				return err
			}
			if denied {
				out = accepted{Result: "gone"}
			} else {
				left := pairingMaxAttempts - attempts
				out = accepted{Result: "wrong_code", AttemptsLeft: &left}
			}
			return nil
		}
		// A newer pairing replaces the principal's old one (one tab per principal).
		if _, err := tx.Exec(ctx, `UPDATE ai_browser_pairings SET status = 'ended', ended_at = now()
			WHERE principal_kind = $1 AND principal_id = $2 AND status = 'paired'`, kind, principal); err != nil {
			return err
		}
		paired := &pairing{}
		if err := tx.QueryRow(ctx, `UPDATE ai_browser_pairings SET status = 'paired', tab_id = $1,
			paired_at = now(), expires_at = now() + $2::interval WHERE id = $3::text::uuid
			RETURNING id::text, principal_label, expires_at`, tab, pairingPairedTTL, id).Scan(
			&paired.ID, &paired.Label, &paired.ExpiresAt); err != nil {
			return err
		}
		out = accepted{Result: "ok", Pairing: paired, PrincipalKind: &kind, PrincipalID: &principal}
		return nil
	})
	if err != nil {
		return nil, err
	}
	return out, nil
}

func (s *Stores) deny(r *http.Request, p crud.Params) (any, error) {
	id, err := p.UUID("pairing_id")
	if err != nil {
		return nil, err
	}
	var denied bool
	err = s.inTx(r.Context(), func(tx pgx.Tx) (err error) {
		denied, err = returned(r.Context(), tx, `UPDATE ai_browser_pairings SET status = 'denied',
			ended_at = now() WHERE id = $1::text::uuid AND status = 'pending' RETURNING id`, id)
		return err
	})
	if err != nil {
		return nil, err
	}
	return done{denied}, nil
}

func (s *Stores) end(r *http.Request, p crud.Params) (any, error) {
	id, err := p.UUID("pairing_id")
	if err != nil {
		return nil, err
	}
	body, err := crud.ReadBody(r, "tab_id")
	if err != nil {
		return nil, err
	}
	tab := body.Text("tab_id", 1, tabIDMax)
	if err := body.Err(); err != nil {
		return nil, err
	}
	var ended bool
	err = s.inTx(r.Context(), func(tx pgx.Tx) (err error) {
		ended, err = returned(r.Context(), tx, `UPDATE ai_browser_pairings SET status = 'ended',
			ended_at = now() WHERE id = $1::text::uuid AND status = 'paired' AND tab_id = $2 RETURNING id`, id, tab)
		return err
	})
	if err != nil {
		return nil, err
	}
	return done{ended}, nil
}

func (s *Stores) pairedTab(r *http.Request, _ crud.Params) (any, error) {
	kind, err := crud.QueryChoice(r, "principal_kind", principalKinds...)
	if err != nil {
		return nil, err
	}
	principal, err := crud.QueryText(r, "principal_id", principalIDMax)
	if err != nil {
		return nil, err
	}
	rows, err := s.Pool.Query(r.Context(),
		`SELECT id::text, principal_label, expires_at, tab_id FROM ai_browser_pairings
		 WHERE principal_kind = $1 AND principal_id = $2
		 AND status = 'paired' AND expires_at > now()`, kind, principal)
	if err != nil {
		return nil, err
	}
	// Python's fetchone: the first row, if any.
	found, err := pgx.CollectRows(rows, pgx.RowToAddrOfStructByPos[pairedTab])
	if err != nil {
		return nil, err
	}
	var one *pairedTab
	if len(found) > 0 {
		one = found[0]
	}
	return struct {
		Pairing *pairedTab `json:"pairing"`
	}{one}, nil
}

func (s *Stores) pending(r *http.Request, _ crud.Params) (any, error) {
	rows, err := s.Pool.Query(r.Context(),
		`SELECT id::text, principal_label, expires_at FROM ai_browser_pairings
		 WHERE status = 'pending' AND expires_at > now() ORDER BY created_at, id`)
	if err != nil {
		return nil, err
	}
	list, err := pgx.CollectRows(rows, pgx.RowToStructByPos[pairing])
	if err != nil {
		return nil, err
	}
	return struct {
		Pairings []pairing `json:"pairings"`
	}{nonNil(list)}, nil
}

func (s *Stores) pairedWith(r *http.Request, _ crud.Params) (any, error) {
	body, err := crud.ReadBody(r, "tab_ids")
	if err != nil {
		return nil, err
	}
	tabs := body.Strings("tab_ids", false, 0, tabsMax, 1, tabIDMax)
	if err := body.Err(); err != nil {
		return nil, err
	}
	type answer struct {
		// Pairings is each live pairing with its tab, oldest pairing first.
		Pairings []pairedTab `json:"pairings"`
	}
	if len(tabs) == 0 {
		return answer{[]pairedTab{}}, nil
	}
	rows, err := s.Pool.Query(r.Context(),
		`SELECT id::text, principal_label, expires_at, tab_id FROM ai_browser_pairings
		 WHERE status = 'paired' AND expires_at > now() AND tab_id = ANY($1)
		 ORDER BY paired_at, id`, tabs)
	if err != nil {
		return nil, err
	}
	list, err := pgx.CollectRows(rows, pgx.RowToStructByPos[pairedTab])
	if err != nil {
		return nil, err
	}
	return answer{nonNil(list)}, nil
}

// -- MCP tokens -----------------------------------------------------------------------------

type verifiedToken struct {
	ID   string `json:"id"`
	Tier string `json:"tier"`
}

// verify finds, checks and stamps the token in one statement, by its hash's unique index: a
// token revoked or expired by the time the row lock is taken does not verify, and
// last_used_at only moves forward.
func (s *Stores) verify(r *http.Request, _ crud.Params) (any, error) {
	body, err := crud.ReadBody(r, "token_hash")
	if err != nil {
		return nil, err
	}
	hash := body.Match("token_hash", 0, 1<<30, sha256Hex)
	if err := body.Err(); err != nil {
		return nil, err
	}
	var token *verifiedToken
	err = s.inTx(r.Context(), func(tx pgx.Tx) error {
		found := &verifiedToken{}
		err := tx.QueryRow(r.Context(), `UPDATE ai_mcp_tokens SET last_used_at = GREATEST(last_used_at, now())
			WHERE token_hash = $1 AND revoked_at IS NULL
			AND (expires_at IS NULL OR expires_at > now()) RETURNING id::text, tier`, hash).Scan(&found.ID, &found.Tier)
		if errors.Is(err, pgx.ErrNoRows) {
			return nil
		}
		token = found
		return err
	})
	if err != nil {
		return nil, err
	}
	return struct {
		Token *verifiedToken `json:"token"`
	}{token}, nil
}

type tokenRecord struct {
	ID            string     `json:"id"`
	Name          string     `json:"name"`
	Tier          string     `json:"tier"`
	CreatedAt     wire.Time  `json:"created_at"`
	ExpiresAt     *wire.Time `json:"expires_at"`
	RevokedAt     *wire.Time `json:"revoked_at"`
	LastUsedAt    *wire.Time `json:"last_used_at"`
	ApprovalGrant bool       `json:"approval_grant"`
}

const tokenColumns = `id::text AS id, name, tier, created_at, expires_at, revoked_at, last_used_at, approval_grant`

func (s *Stores) mint(r *http.Request, _ crud.Params) (any, error) {
	body, err := crud.ReadBody(r, "name", "tier", "token_hash", "expires_at", "approval_grant")
	if err != nil {
		return nil, err
	}
	name := body.Text("name", 1, tokenNameMax)
	tier := body.Choice("tier", tiers...)
	hash := body.Match("token_hash", 0, 1<<30, sha256Hex)
	// Postgres reads the time as psycopg's datetime would land: a naive one in the
	// session's TimeZone.
	expires := body.OptText("expires_at", 0, 1<<30, nil)
	grant := body.Bool("approval_grant", false)
	if err := body.Err(); err != nil {
		return nil, err
	}
	if grant && tier != "outward" {
		return nil, &httpx.Problem{Status: http.StatusUnprocessableEntity, Detail: "an approval grant needs an" +
			" outward token: deciding an outward action is at least as much as taking one"}
	}
	var record tokenRecord
	err = s.inTx(r.Context(), func(tx pgx.Tx) error {
		rows, err := tx.Query(r.Context(), `INSERT INTO ai_mcp_tokens (id, name, tier, token_hash, expires_at,
			approval_grant) VALUES ($1::text::uuid, $2, $3, $4, $5::text::timestamptz, $6)
			RETURNING `+tokenColumns, wire.UUID4(), name, tier, hash, expires, grant)
		if err != nil {
			return err
		}
		record, err = pgx.CollectExactlyOneRow(rows, pgx.RowToStructByPos[tokenRecord])
		return err
	})
	switch code := pgCode(err); {
	case code == "23505":
		return nil, &httpx.Problem{Status: http.StatusConflict, Detail: "a token with this hash exists"}
	case strings.HasPrefix(code, "22"):
		return nil, httpx.Invalid(httpx.Issue{Loc: []string{"body", "expires_at"},
			Msg: "Input should be a valid datetime"})
	case err != nil:
		return nil, err
	}
	return struct {
		Token tokenRecord `json:"token"`
	}{record}, nil
}

func (s *Stores) revokeToken(r *http.Request, p crud.Params) (any, error) {
	id, err := p.UUID("token_id")
	if err != nil {
		return nil, err
	}
	var revoked bool
	err = s.inTx(r.Context(), func(tx pgx.Tx) (err error) {
		revoked, err = returned(r.Context(), tx, `UPDATE ai_mcp_tokens SET revoked_at = now()
			WHERE id = $1::text::uuid AND revoked_at IS NULL RETURNING id`, id)
		return err
	})
	if err != nil {
		return nil, err
	}
	return done{revoked}, nil
}

func (s *Stores) tokens(r *http.Request, _ crud.Params) (any, error) {
	rows, err := s.Pool.Query(r.Context(), `SELECT `+tokenColumns+` FROM ai_mcp_tokens ORDER BY created_at, id`)
	if err != nil {
		return nil, err
	}
	list, err := pgx.CollectRows(rows, pgx.RowToStructByPos[tokenRecord])
	if err != nil {
		return nil, err
	}
	return struct {
		Tokens []tokenRecord `json:"tokens"`
	}{nonNil(list)}, nil
}

func (s *Stores) tokenLive(r *http.Request, p crud.Params) (any, error) {
	id, err := p.UUID("token_id")
	if err != nil {
		return nil, err
	}
	var out struct {
		// Tier is the live (unrevoked, unexpired) token's tier; null for none.
		Tier *string `json:"tier"`
		// ApprovalGrant is whether it holds the approval grant (an outward token's only).
		ApprovalGrant bool `json:"approval_grant"`
	}
	var grant *bool
	err = s.Pool.QueryRow(r.Context(), `SELECT tier, approval_grant AND tier = 'outward' AS grant
		FROM ai_mcp_tokens WHERE id = $1::text::uuid AND revoked_at IS NULL
		AND (expires_at IS NULL OR expires_at > now())`, id).Scan(&out.Tier, &grant)
	if err != nil && !errors.Is(err, pgx.ErrNoRows) {
		return nil, err
	}
	out.ApprovalGrant = grant != nil && *grant
	return out, nil
}

func nonNil[T any](s []T) []T {
	if s == nil {
		return []T{}
	}
	return s
}
