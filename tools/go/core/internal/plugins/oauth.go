package plugins

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"regexp"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/seal"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/wire"
)

const noKEKToOpenSignIn = "no key-encryption key is configured to open the sign-in"

var (
	hashPattern     = regexp.MustCompile(`^[0-9a-f]{64}$`)
	bindingPattern  = regexp.MustCompile(`^([0-9a-f]{64})?$`)
	authMethods     = []string{"none", "client_secret_post", "client_secret_basic"}
	registrations   = []string{"dynamic", "configured"}
	errOpenedShape  = errors.New("an opened sign-in is not its secret's shape")
	flowMetaFields  = []string{"issuer", "authorization_endpoint", "token_endpoint", "iss_supported", "resource", "scope", "client_id", "auth_method", "registration", "redirect_uri"}
	flowSecretNames = []string{"state", "verifier", "client_secret"}
	grantNames      = []string{"access_token", "refresh_token", "client_secret"}
)

// flowMeta is oauthStore.ts FlowMeta, in its field order: the row's meta.
type flowMeta struct {
	Issuer                string  `json:"issuer"`
	AuthorizationEndpoint string  `json:"authorization_endpoint"`
	TokenEndpoint         string  `json:"token_endpoint"`
	IssSupported          bool    `json:"iss_supported"`
	Resource              string  `json:"resource"`
	Scope                 *string `json:"scope"`
	ClientID              string  `json:"client_id"`
	AuthMethod            string  `json:"auth_method"`
	Registration          string  `json:"registration"`
	RedirectURI           string  `json:"redirect_uri"`
}

func readMeta(f *form) flowMeta {
	n := f.model("meta", required, "FlowMeta", flowMetaFields...)
	if n == nil {
		return flowMeta{}
	}
	defer n.done()
	var m flowMeta
	str := func(name string, least, most int) string {
		if s := n.text(name, required, least, most, nil); s != nil {
			return *s
		}
		return ""
	}
	m.Issuer = str("issuer", 1, urlMax)
	m.AuthorizationEndpoint = str("authorization_endpoint", 1, urlMax)
	m.TokenEndpoint = str("token_endpoint", 1, urlMax)
	if b := n.boolean("iss_supported", required); b != nil {
		m.IssSupported = *b
	}
	m.Resource = str("resource", 1, urlMax)
	m.Scope = n.text("scope", nullable, 0, textMax, nil)
	m.ClientID = str("client_id", 1, textMax)
	if s := n.literal("auth_method", required, authMethods...); s != nil {
		m.AuthMethod = *s
	}
	if s := n.literal("registration", required, registrations...); s != nil {
		m.Registration = *s
	}
	m.RedirectURI = str("redirect_uri", 1, urlMax)
	return m
}

// readFlowSecret is FlowSecret: f the model, nil when there is none.
func readFlowSecret(n *form) flowSecret {
	var s flowSecret
	if n == nil {
		return s
	}
	defer n.done()
	if v := n.text("state", required, 1, 512, nil); v != nil {
		s.State = *v
	}
	if v := n.text("verifier", required, 1, 512, nil); v != nil {
		s.Verifier = *v
	}
	s.ClientSecret = n.text("client_secret", optional, 1, tokenMax, nil)
	return s
}

// readGrantSecret is GrantSecret: n the model, nil when there is none.
func readGrantSecret(n *form) grantSecret {
	var s grantSecret
	if n == nil {
		return s
	}
	defer n.done()
	if v := n.text("access_token", required, 1, tokenMax, nil); v != nil {
		s.AccessToken = *v
	}
	s.RefreshToken = n.text("refresh_token", optional, 1, tokenMax, nil)
	s.ClientSecret = n.text("client_secret", optional, 1, tokenMax, nil)
	return s
}

// parsed reads an opened plaintext as model_validate_json does; a text that is not the
// model's shape is an error that names nothing of it.
func parsed(text string, names []string) (*form, func() error) {
	f := &form{issues: &[]httpx.Issue{}, names: names}
	if !f.parse(json.RawMessage(text)) {
		return nil, func() error { return errOpenedShape }
	}
	return f, func() error {
		if len(*f.issues) > 0 {
			return errOpenedShape
		}
		return nil
	}
}

func parseFlowSecret(text string) (flowSecret, error) {
	f, check := parsed(text, flowSecretNames)
	s := readFlowSecret(f)
	return s, check()
}

func parseGrantSecret(text string) (grantSecret, error) {
	f, check := parsed(text, grantNames)
	s := readGrantSecret(f)
	return s, check()
}

func str(p *string) string {
	if p == nil {
		return ""
	}
	return *p
}

type doneAnswer struct {
	Done bool `json:"done"`
}

type storedAnswer struct {
	Stored bool `json:"stored"`
}

// flowStart is PluginOAuth.start: its state and verifier sealed, the expired flows dropped
// first.
func (s *Plugins) flowStart(r *http.Request, _ crud.Params) (any, error) {
	f, err := readForm(r, "state_hash", "plugin", "plugin_url", "ticket_hash", "meta", "secret",
		"created_by", "ttl_ms")
	if err != nil {
		return nil, err
	}
	stateHash := str(f.text("state_hash", required, 0, 1<<30, hashPattern))
	plugin := str(f.text("plugin", required, 0, 1<<30, namePattern))
	pluginURL := str(f.text("plugin_url", required, 1, urlMax, nil))
	ticketHash := str(f.text("ticket_hash", required, 0, 1<<30, hashPattern))
	meta := readMeta(f)
	secret := readFlowSecret(f.model("secret", required, "FlowSecret", flowSecretNames...))
	createdBy := str(f.text("created_by", required, 1, byMax, nil))
	ttl := f.integer("ttl_ms", 0, 3_600_000)
	if err := f.err(); err != nil {
		return nil, err
	}
	kek, err := s.needKEK()
	if err != nil {
		return nil, err
	}
	key := flowKey{meta.Issuer, meta.TokenEndpoint, meta.ClientID, meta.RedirectURI}
	sealed := seal.SealSecret(kek, secret.text(), flowAAD(stateHash, plugin, pluginURL, key))
	metaJSON, err := json.Marshal(meta)
	if err != nil {
		return nil, err
	}
	ctx := r.Context()
	err = s.inTx(ctx, func(tx pgx.Tx) error {
		if _, err := tx.Exec(ctx, `DELETE FROM ai_plugin_oauth_flows WHERE expires_at < now()`); err != nil {
			return err
		}
		_, err := tx.Exec(ctx, `INSERT INTO ai_plugin_oauth_flows (state_hash, plugin, plugin_url,
			ticket_hash, meta, secret_sealed, dek_sealed, kek_id, created_by, expires_at)
			VALUES ($1, $2, $3, $4, $5::text::jsonb, $6, $7, $8, $9, now() + make_interval(secs => $10))`,
			stateHash, plugin, pluginURL, ticketHash, string(metaJSON), sealed.SecretSealed,
			sealed.DEKSealed, sealed.KEKID, createdBy, float64(ttl)/1000)
		var pg *pgconn.PgError
		if errors.As(err, &pg) && pg.Code == "23503" {
			return httpx.WithReason(http.StatusConflict,
				fmt.Sprintf("plugin %s was removed during the sign-in", plugin), pluginGone)
		}
		return err
	})
	if err != nil {
		return nil, err
	}
	return doneAnswer{true}, nil
}

// flowOpened is a sign-in under way, its secret opened; seal_error says why it is not.
type flowOpened struct {
	Plugin    string          `json:"plugin"`
	PluginURL string          `json:"plugin_url"`
	Meta      json.RawMessage `json:"meta"`
	Secret    *flowSecret     `json:"secret"`
	SealError *string         `json:"seal_error"`
}

// flowRow is a ai_plugin_oauth_flows row as the routes return it.
type flowRow struct {
	stateHash, plugin, pluginURL, meta string
	envelope                           seal.Envelope
}

const flowReturning = `state_hash, plugin, plugin_url, meta::text, secret_sealed, dek_sealed, kek_id`

func (row *flowRow) targets(more ...any) []any {
	return append([]any{&row.stateHash, &row.plugin, &row.pluginURL, &row.meta,
		&row.envelope.SecretSealed, &row.envelope.DEKSealed, &row.envelope.KEKID}, more...)
}

func (s *Plugins) openFlow(row *flowRow) (flowOpened, error) {
	var meta bytes.Buffer
	if err := json.Compact(&meta, []byte(row.meta)); err != nil {
		return flowOpened{}, err
	}
	out := flowOpened{Plugin: row.plugin, PluginURL: row.pluginURL, Meta: meta.Bytes()}
	if s.kek() == nil {
		out.SealError = some(noKEKToOpenSignIn)
		return out, nil
	}
	var key flowKey
	if err := json.Unmarshal(meta.Bytes(), &key); err != nil {
		return flowOpened{}, err
	}
	text, err := s.open(row.envelope, flowAAD(row.stateHash, row.plugin, row.pluginURL, key))
	if err != nil {
		out.SealError = some(err.Error())
		return out, nil
	}
	secret, err := parseFlowSecret(text)
	if err != nil {
		return flowOpened{}, err
	}
	out.Secret = &secret
	return out, nil
}

type flowAnswer struct {
	Flow *flowOpened `json:"flow"`
}

// flowAuthorize spends a ticket once (PluginOAuth.authorize): binds its flow to the
// browser's cookie and answers the flow opened. Without a KEK nothing is spent.
func (s *Plugins) flowAuthorize(r *http.Request, _ crud.Params) (any, error) {
	f, err := readForm(r, "ticket_hash", "binding_hash")
	if err != nil {
		return nil, err
	}
	ticket := str(f.text("ticket_hash", required, 0, 1<<30, hashPattern))
	binding := str(f.text("binding_hash", required, 0, 1<<30, hashPattern))
	if err := f.err(); err != nil {
		return nil, err
	}
	if _, err := s.needKEK(); err != nil {
		return nil, err
	}
	var row *flowRow
	ctx := r.Context()
	err = s.inTx(ctx, func(tx pgx.Tx) error {
		found := &flowRow{}
		err := tx.QueryRow(ctx, `UPDATE ai_plugin_oauth_flows SET ticket_hash = NULL,
			binding_hash = $1 WHERE ticket_hash = $2 AND binding_hash IS NULL AND expires_at > now()
			RETURNING `+flowReturning, binding, ticket).Scan(found.targets()...)
		if errors.Is(err, pgx.ErrNoRows) {
			return nil
		}
		row = found
		return err
	})
	if err != nil || row == nil {
		return flowAnswer{}, err
	}
	opened, err := s.openFlow(row)
	if err != nil {
		return nil, err
	}
	return flowAnswer{&opened}, nil
}

type flowTaken struct {
	flowOpened
	CreatedBy string `json:"created_by"`
	Expired   bool   `json:"expired"`
}

type takeAnswer struct {
	Flow *flowTaken `json:"flow"`
	Held bool       `json:"held"`
}

// flowTake is the callback's flow (PluginOAuth.complete), deleted before anything else: a
// state is good once, and only with the browser's binding.
func (s *Plugins) flowTake(r *http.Request, _ crud.Params) (any, error) {
	f, err := readForm(r, "state_hash", "binding_hash")
	if err != nil {
		return nil, err
	}
	state := str(f.text("state_hash", required, 0, 1<<30, hashPattern))
	binding := str(f.text("binding_hash", required, 0, 1<<30, bindingPattern))
	if err := f.err(); err != nil {
		return nil, err
	}
	var row *flowRow
	var createdBy string
	var expired, held bool
	ctx := r.Context()
	err = s.inTx(ctx, func(tx pgx.Tx) error {
		found := &flowRow{}
		err := tx.QueryRow(ctx, `DELETE FROM ai_plugin_oauth_flows WHERE state_hash = $1
			AND binding_hash = $2 RETURNING `+flowReturning+`, created_by, expires_at <= now()`,
			state, binding).Scan(found.targets(&createdBy, &expired)...)
		if err == nil {
			row = found
			return nil
		}
		if !errors.Is(err, pgx.ErrNoRows) {
			return err
		}
		var one int
		err = tx.QueryRow(ctx, `SELECT 1 FROM ai_plugin_oauth_flows WHERE state_hash = $1
			AND expires_at > now()`, state).Scan(&one)
		held = err == nil
		if errors.Is(err, pgx.ErrNoRows) {
			return nil
		}
		return err
	})
	if err != nil {
		return nil, err
	}
	if row == nil {
		return takeAnswer{Held: held}, nil
	}
	opened, err := s.openFlow(row)
	if err != nil {
		return nil, err
	}
	return takeAnswer{Flow: &flowTaken{opened, createdBy, expired}}, nil
}

// grantStore keeps a completed sign-in (PluginOAuth.complete), sealed, only while the plugin
// still stands at the URL it was signed in for.
func (s *Plugins) grantStore(r *http.Request, _ crud.Params) (any, error) {
	f, err := readForm(r, "plugin", "plugin_url", "resource", "issuer", "token_endpoint",
		"client_id", "auth_method", "registration", "scope", "secret", "has_refresh",
		"access_expires_at", "connected_by")
	if err != nil {
		return nil, err
	}
	key := grantKey{
		Plugin:        str(f.text("plugin", required, 0, 1<<30, namePattern)),
		PluginURL:     str(f.text("plugin_url", required, 1, urlMax, nil)),
		Resource:      str(f.text("resource", required, 1, urlMax, nil)),
		Issuer:        str(f.text("issuer", required, 1, urlMax, nil)),
		TokenEndpoint: str(f.text("token_endpoint", required, 1, urlMax, nil)),
		ClientID:      str(f.text("client_id", required, 1, textMax, nil)),
	}
	authMethod := str(f.literal("auth_method", required, authMethods...))
	registration := str(f.literal("registration", required, registrations...))
	scope := f.text("scope", nullable, 0, textMax, nil)
	secret := readGrantSecret(f.model("secret", required, "GrantSecret", grantNames...))
	hasRefresh := f.boolean("has_refresh", required)
	expires := f.datetime("access_expires_at", nullable)
	connectedBy := str(f.text("connected_by", required, 1, byMax, nil))
	if err := f.err(); err != nil {
		return nil, err
	}
	kek, err := s.needKEK()
	if err != nil {
		return nil, err
	}
	sealed := seal.SealSecret(kek, secret.text(), grantAAD(key))
	stored := false
	ctx := r.Context()
	err = s.inTx(ctx, func(tx pgx.Tx) error {
		var name string
		err := tx.QueryRow(ctx, `SELECT name FROM ai_plugins WHERE name = $1 AND url = $2
			FOR UPDATE`, key.Plugin, key.PluginURL).Scan(&name)
		if errors.Is(err, pgx.ErrNoRows) {
			return nil
		}
		if err != nil {
			return err
		}
		if _, err := tx.Exec(ctx, `INSERT INTO ai_plugin_oauth (plugin, plugin_url, resource,
			issuer, token_endpoint, client_id, auth_method, registration, scope, secret_sealed,
			dek_sealed, kek_id, has_refresh, access_expires_at, status, last_error, grant_id,
			connected_by, connected_at, refreshed_at) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9,
			$10, $11, $12, $13, $14, 'connected', NULL, $15::uuid, $16, now(), NULL)
			ON CONFLICT (plugin) DO UPDATE SET plugin_url = EXCLUDED.plugin_url,
			resource = EXCLUDED.resource, issuer = EXCLUDED.issuer,
			token_endpoint = EXCLUDED.token_endpoint, client_id = EXCLUDED.client_id,
			auth_method = EXCLUDED.auth_method, registration = EXCLUDED.registration,
			scope = EXCLUDED.scope, secret_sealed = EXCLUDED.secret_sealed,
			dek_sealed = EXCLUDED.dek_sealed, kek_id = EXCLUDED.kek_id,
			has_refresh = EXCLUDED.has_refresh, access_expires_at = EXCLUDED.access_expires_at,
			status = 'connected', last_error = NULL, grant_id = EXCLUDED.grant_id,
			connected_by = EXCLUDED.connected_by, connected_at = EXCLUDED.connected_at,
			refreshed_at = NULL`,
			key.Plugin, key.PluginURL, key.Resource, key.Issuer, key.TokenEndpoint, key.ClientID,
			authMethod, registration, scope, sealed.SecretSealed, sealed.DEKSealed, sealed.KEKID,
			hasRefresh != nil && *hasRefresh, expires, wire.UUID4(), connectedBy); err != nil {
			return err
		}
		if _, err := tx.Exec(ctx, `UPDATE ai_plugins SET updated_at = now() WHERE name = $1`,
			key.Plugin); err != nil {
			return err
		}
		stored = true
		return wire.NotifyWarmEvict(ctx, tx)
	})
	if err != nil {
		return nil, err
	}
	return storedAnswer{stored}, nil
}

// openedGrant is a sign-in's row for a token request, its secret opened.
type openedGrant struct {
	Plugin          string      `json:"plugin"`
	PluginURL       string      `json:"plugin_url"`
	Resource        string      `json:"resource"`
	Issuer          string      `json:"issuer"`
	TokenEndpoint   string      `json:"token_endpoint"`
	ClientID        string      `json:"client_id"`
	AuthMethod      string      `json:"auth_method"`
	GrantID         string      `json:"grant_id"`
	AccessExpiresAt *wire.Time  `json:"access_expires_at"`
	Secret          grantSecret `json:"secret"`
}

type grantOpenAnswer struct {
	Grant *openedGrant `json:"grant"`
	// Why there is none (PluginOAuth's GrantError text): sign in again.
	Fail *string `json:"fail"`
}

// grantOpen is PluginOAuth.fetchToken's checks, then the row opened.
func (s *Plugins) grantOpen(r *http.Request, p crud.Params) (any, error) {
	plugin, err := p.Match("plugin", namePattern)
	if err != nil {
		return nil, err
	}
	f, err := readForm(r, "plugin_url", "grant")
	if err != nil {
		return nil, err
	}
	pluginURL := str(f.text("plugin_url", required, 1, urlMax, nil))
	grant := f.uuid("grant")
	if err := f.err(); err != nil {
		return nil, err
	}
	var g openedGrant
	var status string
	var lastError *string
	var e seal.Envelope
	found := false
	ctx := r.Context()
	err = s.inTx(ctx, func(tx pgx.Tx) error {
		err := tx.QueryRow(ctx, `SELECT plugin, plugin_url, resource, issuer, token_endpoint,
			client_id, auth_method, grant_id::text, access_expires_at, status, last_error,
			secret_sealed, dek_sealed, kek_id FROM ai_plugin_oauth WHERE plugin = $1`, plugin).Scan(
			&g.Plugin, &g.PluginURL, &g.Resource, &g.Issuer, &g.TokenEndpoint, &g.ClientID,
			&g.AuthMethod, &g.GrantID, &g.AccessExpiresAt, &status, &lastError,
			&e.SecretSealed, &e.DEKSealed, &e.KEKID)
		if errors.Is(err, pgx.ErrNoRows) {
			return nil
		}
		found = err == nil
		return err
	})
	if err != nil {
		return nil, err
	}
	fail := func(reason string) (any, error) { return grantOpenAnswer{Fail: &reason}, nil }
	switch {
	case s.kek() == nil:
		return fail(noKEKToOpenSignIn)
	case !found:
		return fail("it is not signed in")
	case status == "error":
		if lastError == nil || *lastError == "" {
			return fail("its sign-in failed")
		}
		return fail(*lastError)
	case g.PluginURL != pluginURL:
		return fail("it was signed in at another URL")
	case g.GrantID != grant:
		return fail("it was signed in again or out since this run loaded it")
	}
	text, err := s.open(e, grantAAD(grantKey{g.Plugin, g.PluginURL, g.Resource, g.Issuer,
		g.TokenEndpoint, g.ClientID}))
	if err != nil {
		return fail(fmt.Sprintf("its sign-in cannot be opened (%s)", err.Error()))
	}
	if g.Secret, err = parseGrantSecret(text); err != nil {
		return nil, err
	}
	return grantOpenAnswer{Grant: &g}, nil
}

// grantRefreshed keeps a refreshed sign-in only while the caller holds the refresh lease
// plugin-oauth:<plugin> (else 409 lease-lost: another replica may spend the refresh token)
// and the grant is still the one it refreshed, and connected.
func (s *Plugins) grantRefreshed(r *http.Request, p crud.Params) (any, error) {
	plugin, err := p.Match("plugin", namePattern)
	if err != nil {
		return nil, err
	}
	f, err := readForm(r, "lease_token", "grant", "secret", "has_refresh", "access_expires_at", "scope")
	if err != nil {
		return nil, err
	}
	leaseToken := f.uuid("lease_token")
	grant := f.uuid("grant")
	secret := readGrantSecret(f.model("secret", required, "GrantSecret", grantNames...))
	hasRefresh := f.boolean("has_refresh", required)
	expires := f.datetime("access_expires_at", nullable)
	scope := f.text("scope", nullable, 0, textMax, nil)
	if err := f.err(); err != nil {
		return nil, err
	}
	kek, err := s.needKEK()
	if err != nil {
		return nil, err
	}
	stored := false
	ctx := r.Context()
	err = s.inTx(ctx, func(tx pgx.Tx) error {
		held, err := heldIn(ctx, tx, "plugin-oauth:"+plugin, leaseToken)
		if err != nil {
			return err
		}
		if !held {
			return httpx.WithReason(http.StatusConflict,
				"The refresh lease is not held by this token.", leaseLost)
		}
		var k grantKey
		err = tx.QueryRow(ctx, `SELECT plugin, plugin_url, resource, issuer, token_endpoint,
			client_id FROM ai_plugin_oauth WHERE plugin = $1 AND grant_id = $2::uuid
			AND status = 'connected' FOR UPDATE`, plugin, grant).Scan(
			&k.Plugin, &k.PluginURL, &k.Resource, &k.Issuer, &k.TokenEndpoint, &k.ClientID)
		if errors.Is(err, pgx.ErrNoRows) {
			return nil
		}
		if err != nil {
			return err
		}
		sealed := seal.SealSecret(kek, secret.text(), grantAAD(k))
		if _, err := tx.Exec(ctx, `UPDATE ai_plugin_oauth SET secret_sealed = $1, dek_sealed = $2,
			kek_id = $3, has_refresh = $4, access_expires_at = $5, refreshed_at = now(),
			scope = coalesce($6, scope) WHERE plugin = $7`, sealed.SecretSealed, sealed.DEKSealed,
			sealed.KEKID, hasRefresh != nil && *hasRefresh, expires, scope, plugin); err != nil {
			return err
		}
		stored = true
		return nil
	})
	if err != nil {
		return nil, err
	}
	return storedAnswer{stored}, nil
}

// heldIn is leases.py held_in: whether token holds key, the row locked FOR SHARE.
func heldIn(ctx context.Context, tx pgx.Tx, key, token string) (bool, error) {
	var one int
	err := tx.QueryRow(ctx, `SELECT 1 FROM agent_leases WHERE key = $1 AND token = $2
		AND expires_at > now() FOR SHARE`, key, token).Scan(&one)
	if errors.Is(err, pgx.ErrNoRows) {
		return false, nil
	}
	return err == nil, err
}

type storedSecrets struct {
	Secrets []grantSecret `json:"secrets"`
}

// storedSecrets is every stored sign-in's secrets, opened, for the redaction list
// (PluginOAuth.storedSecrets). A row the key does not open gives nothing.
func (s *Plugins) storedSecrets(r *http.Request, _ crud.Params) (any, error) {
	out := storedSecrets{Secrets: []grantSecret{}}
	if s.kek() == nil {
		return out, nil
	}
	type grantRow struct {
		key grantKey
		e   seal.Envelope
	}
	var rows []grantRow
	ctx := r.Context()
	err := s.inTx(ctx, func(tx pgx.Tx) error {
		found, err := tx.Query(ctx, `SELECT plugin, plugin_url, resource, issuer, token_endpoint,
			client_id, secret_sealed, dek_sealed, kek_id FROM ai_plugin_oauth`)
		if err != nil {
			return err
		}
		defer found.Close()
		for found.Next() {
			var g grantRow
			if err := found.Scan(&g.key.Plugin, &g.key.PluginURL, &g.key.Resource, &g.key.Issuer,
				&g.key.TokenEndpoint, &g.key.ClientID, &g.e.SecretSealed, &g.e.DEKSealed,
				&g.e.KEKID); err != nil {
				return err
			}
			rows = append(rows, g)
		}
		return found.Err()
	})
	if err != nil {
		return nil, err
	}
	for _, g := range rows {
		text, err := s.open(g.e, grantAAD(g.key))
		if err != nil {
			continue
		}
		secret, err := parseGrantSecret(text)
		if err != nil {
			return nil, err
		}
		out.Secrets = append(out.Secrets, secret)
	}
	return out, nil
}
