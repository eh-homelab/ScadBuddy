package secrets

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"regexp"
	"slices"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"

	"github.com/jackc/pgx/v5"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/seal"
)

// The agent's Claude credentials, ai_credentials: agent_data/credentials.py and the
// credentials routes of api/crud_agent_secrets.py. The AAD binds a sealed secret to its row
// and to the columns that say where it is sent. Every structural write takes the table's lock
// (SHARE ROW EXCLUSIVE: readers are not blocked); a health write locks the row.

const (
	maxCredentials    = 100
	nameMax           = 60
	credentialIDMax   = 64
	redactAuditWindow = "10 minutes"

	tooManyMessage    = "at most 100 Claude credentials can be stored; delete one first"
	staleOrderMessage = "the order must name every stored credential exactly once; the list changed since it" +
		" was read, so reload it"
	legacyFormatMessage = "the stored credential was saved in an older format that did not bind its kind and" +
		" base URL; save it again"
	wideningMessage = "to share a fleet credential with the agent, enter its secret again"
)

var kinds = []string{"anthropic_api_key", "claude_login", "claude_oauth_token", "gateway"}

// scopes say who may use a credential (#7983): ScadBuddy's agent, the fleet's login rotator, or both.
var scopes = []string{"agent", "fleet", "both"}

// claudeLogin is the kind whose secret is a whole Claude Code claudeAiOauth object. Only the
// fleet uses one; its account is stored in clear, and only it holds a machine lease.
const claudeLogin = "claude_login"

const (
	accountFieldMax = 200
	emailMax        = 320
)

// refused is a request the credential store refuses (CredentialRefusedError).
func refused(status int, detail string) error {
	return httpx.WithReason(status, detail, credentialRefused)
}

// credentialAAD is credentials.ts credentialAad: JSON as JSON.stringify writes it.
func credentialAAD(id, kind string, baseURL *string) string {
	base := "null"
	if baseURL != nil {
		base = seal.JSONString(*baseURL)
	}
	return "ai_credentials:" + id + `:{"kind":` + seal.JSONString(kind) + `,"base_url":` + base + "}"
}

// last4 is secrets.ts last4: empty for a secret under 12 characters.
func last4(secret string) string {
	runes := []rune(secret)
	if len(runes) < 12 {
		return ""
	}
	return string(runes[len(runes)-4:])
}

// pyIsSpace is Python's str.isspace for one character.
func pyIsSpace(r rune) bool { return unicode.IsSpace(r) || (r >= 0x1c && r <= 0x1f) }

func pyStrip(s string) string { return strings.TrimFunc(s, pyIsSpace) }

func checkName(raw *string) (*string, error) {
	if raw == nil {
		return nil, nil
	}
	name := pyStrip(*raw)
	if name == "" {
		return nil, nil
	}
	if name != *raw {
		return nil, refused(400, "name must be trimmed: no leading or trailing space")
	}
	if utf8.RuneCountInString(name) > nameMax {
		return nil, refused(400, fmt.Sprintf("name must be at most %d characters", nameMax))
	}
	for _, r := range name {
		if unicode.In(r, unicode.Cc, unicode.Cf, unicode.Zl, unicode.Zp) {
			return nil, refused(400,
				"name must be one line of plain text, with no control or formatting characters")
		}
	}
	return &name, nil
}

var schemePattern = regexp.MustCompile(`^[A-Za-z][A-Za-z0-9+.-]*:`)

// checkBaseURL is the base URL as the agent normalised it, refused otherwise (never
// rewritten: the AAD binds it as given). It reads the URL as Python's urlsplit does.
func checkBaseURL(raw string) (string, error) {
	scheme, rest := "", raw
	if m := schemePattern.FindString(raw); m != "" {
		scheme, rest = strings.ToLower(m[:len(m)-1]), raw[len(m):]
	}
	netloc := ""
	if strings.HasPrefix(rest, "//") {
		netloc = rest[2:]
		if i := strings.IndexAny(netloc, "/?#"); i >= 0 {
			netloc = netloc[:i]
		}
	}
	if strings.Contains(netloc, "[") != strings.Contains(netloc, "]") {
		return "", refused(400, "base_url is not a valid URL")
	}
	if (scheme != "http" && scheme != "https") || netloc == "" {
		return "", refused(400, "base_url must be an http(s) URL")
	}
	if strings.Contains(netloc, "@") {
		return "", refused(400, "base_url must not carry credentials; put the token in secret")
	}
	if strings.ContainsAny(raw, "?#") {
		return "", refused(400, "base_url must not have a query or fragment")
	}
	if strings.HasSuffix(raw, "/") || raw != pyStrip(raw) {
		return "", refused(400, "base_url must be normalised: no trailing slash")
	}
	return raw, nil
}

const columns = `id, priority, kind, name, base_url, last4, updated_at, kek_id,
	get_byte(secret_sealed, 0) AS seal_version,
	CASE WHEN status = 'cooling_down' AND cooldown_until <= now() THEN 'active' ELSE status END AS status,
	CASE WHEN status = 'cooling_down' AND cooldown_until > now() THEN cooldown_until END AS cooldown_until,
	last_error, last_error_at, last_used_at, epoch, scope, account_uuid, organization_uuid,
	subscription_type, rate_limit_tier, email, leased_to, leased_at, lease_epoch`

type row struct {
	ID            string
	Priority      int
	Kind          string
	Name          *string
	BaseURL       *string
	Last4         string
	UpdatedAt     time.Time
	KEKID         string
	SealVersion   int
	Status        string
	CooldownUntil *time.Time
	LastError     *string
	LastErrorAt   *time.Time
	LastUsedAt    *time.Time
	Epoch         int
	Scope         string
	account
	Email        *string
	LeasedTo     *string
	LeasedAt     *time.Time
	LeaseEpoch   int64
	SecretSealed []byte
	DEKSealed    []byte
}

// account is a claude_login's account, in clear: never secret.
type account struct {
	AccountUUID      *string
	OrganizationUUID *string
	SubscriptionType *string
	RateLimitTier    *string
}

type querier interface {
	Query(ctx context.Context, sql string, args ...any) (pgx.Rows, error)
}

// agentsOwn is the agent's rows only (#7983): never a fleet row. The reveal is the agent's, so
// every read of sealed bytes takes it, in the SQL: a fleet row reads as no row.
const agentsOwn = "scope IN ('agent', 'both') AND kind <> 'claude_login'"

// rows are the credentials as of the database's clock: an expired cooldown reads as active.
// With sealed, the sealed bytes too, of the agent's own rows only.
func rows(ctx context.Context, q querier, id *string, sealed bool) ([]row, error) {
	cols, where, args := columns, []string{}, []any{}
	if sealed {
		cols += ", secret_sealed, dek_sealed"
		where = append(where, agentsOwn)
	} else {
		cols += ", NULL::bytea, NULL::bytea"
	}
	if id != nil {
		where, args = append(where, "id = $1"), []any{*id}
	}
	filter := ""
	if len(where) > 0 {
		filter = " WHERE " + strings.Join(where, " AND ")
	}
	found, err := q.Query(ctx, "SELECT "+cols+" FROM ai_credentials"+filter+" ORDER BY priority", args...)
	if err != nil {
		return nil, err
	}
	return pgx.CollectRows(found, pgx.RowToStructByPos[row])
}

func first(ctx context.Context, q querier, id *string) (*row, error) {
	found, err := rows(ctx, q, id, false)
	if err != nil || len(found) == 0 {
		return nil, err
	}
	return &found[0], nil
}

// iso is Python's datetime.isoformat, in UTC.
func iso(t *time.Time) *string {
	if t == nil {
		return nil
	}
	s := t.UTC().Format("2006-01-02T15:04:05.999999")
	if t.Nanosecond()/1000 != 0 {
		// isoformat writes all six digits of a fraction.
		whole, frac, _ := strings.Cut(s, ".")
		s = whole + "." + (frac + "00000")[:6]
	}
	s += "+00:00"
	return &s
}

// stored is the agent's StoredCredential: never a secret.
type stored struct {
	ID            string  `json:"id"`
	Priority      int     `json:"priority"`
	Kind          string  `json:"kind"`
	Name          *string `json:"name"`
	BaseURL       *string `json:"base_url"`
	Last4         string  `json:"last4"`
	UpdatedAt     string  `json:"updated_at"`
	Status        string  `json:"status"`
	CooldownUntil *string `json:"cooldown_until"`
	LastError     *string `json:"last_error"`
	LastErrorAt   *string `json:"last_error_at"`
	LastUsedAt    *string `json:"last_used_at"`
	Epoch         int     `json:"epoch"`
	KEKID         string  `json:"kek_id"`
	LegacyFormat  bool    `json:"legacy_format"`
	// #7983: who may use it, a claude_login's account and its machine lease.
	Scope            string  `json:"scope"`
	AccountUUID      *string `json:"account_uuid"`
	OrganizationUUID *string `json:"organization_uuid"`
	SubscriptionType *string `json:"subscription_type"`
	RateLimitTier    *string `json:"rate_limit_tier"`
	Email            *string `json:"email"`
	LeasedTo         *string `json:"leased_to"`
	LeasedAt         *string `json:"leased_at"`
	LeaseEpoch       int64   `json:"lease_epoch"`
}

func (r *row) stored() stored {
	return stored{
		ID: r.ID, Priority: r.Priority, Kind: r.Kind, Name: r.Name, BaseURL: r.BaseURL,
		Last4: r.Last4, UpdatedAt: *iso(&r.UpdatedAt), Status: r.Status,
		CooldownUntil: iso(r.CooldownUntil), LastError: r.LastError, LastErrorAt: iso(r.LastErrorAt),
		LastUsedAt: iso(r.LastUsedAt), Epoch: r.Epoch, KEKID: r.KEKID, LegacyFormat: r.SealVersion == seal.V1,
		Scope: r.Scope, AccountUUID: r.AccountUUID, OrganizationUUID: r.OrganizationUUID,
		SubscriptionType: r.SubscriptionType, RateLimitTier: r.RateLimitTier, Email: r.Email,
		LeasedTo: r.LeasedTo, LeasedAt: iso(r.LeasedAt), LeaseEpoch: r.LeaseEpoch,
	}
}

// label is credentials.ts credentialLabel: which credential, never its secret or name.
func (r *row) label() string {
	what := "API key"
	switch r.Kind {
	case "gateway":
		what = "gateway "
		if r.BaseURL != nil {
			what += *r.BaseURL
		}
	case "claude_oauth_token":
		what = "OAuth token"
	case claudeLogin:
		what = "Claude login"
	}
	tail := ""
	if r.Last4 != "" {
		tail = " …" + r.Last4
	}
	return fmt.Sprintf("credential %d (%s%s)", r.Priority+1, what, tail)
}

type credentialList struct {
	Credentials []stored `json:"credentials"`
}

type oneCredential struct {
	Credential *stored `json:"credential"`
}

func listOf(found []row) credentialList {
	out := credentialList{Credentials: []stored{}}
	for i := range found {
		out.Credentials = append(out.Credentials, found[i].stored())
	}
	return out
}

func one(r *row) oneCredential {
	if r == nil {
		return oneCredential{}
	}
	s := r.stored()
	return oneCredential{&s}
}

func lock(ctx context.Context, tx pgx.Tx) error {
	_, err := tx.Exec(ctx, `LOCK TABLE ai_credentials IN SHARE ROW EXCLUSIVE MODE`)
	return err
}

func evict(ctx context.Context, tx pgx.Tx) error {
	_, err := tx.Exec(ctx, `SELECT pg_notify($1, '*')`, warmEvictChannel)
	return err
}

func (s *Store) key(_ *http.Request, _ crud.Params) (any, error) {
	return struct {
		OK     bool    `json:"ok"`
		KEKID  *string `json:"kek_id"`
		Reason *string `json:"reason"`
	}{true, &s.Keys.Current.ID, nil}, nil
}

func (s *Store) list(r *http.Request, _ crud.Params) (any, error) {
	found, err := rows(r.Context(), s.Pool, nil, false)
	if err != nil {
		return nil, err
	}
	return listOf(found), nil
}

func (s *Store) get(r *http.Request, _ crud.Params) (any, error) {
	var id *string
	if values := r.URL.Query()["id"]; len(values) > 0 {
		v := values[len(values)-1]
		if n := utf8.RuneCountInString(v); n < 1 || n > credentialIDMax {
			return nil, httpx.Invalid(httpx.Issue{Loc: []string{"query", "id"},
				Msg: "String should have between 1 and 64 characters"})
		}
		id = &v
	}
	found, err := first(r.Context(), s.Pool, id)
	if err != nil {
		return nil, err
	}
	return one(found), nil
}

// update is credentials.py Update, read from the body's object at f.
type update struct {
	kind            string
	baseURL, secret *string
	name            *string
	// scope nil keeps the stored one (a new row: agent, or fleet for a claude_login).
	scope *string
	// email nil keeps the stored one; "" clears it. A claude_login's only.
	email *string
}

func readUpdate(f *form) update {
	u := update{
		kind:    f.choice("kind", nil, kinds...),
		baseURL: f.text("base_url", 0, 2048, nil),
		secret:  f.text("secret", 1, 4096, nil),
		name:    f.text("name", 0, 1024, nil),
		email:   f.text("email", 0, 1024, nil),
	}
	if f.raw("scope") != nil {
		scope := f.choice("scope", nil, scopes...)
		u.scope = &scope
	}
	return u
}

var updateFields = []string{"kind", "base_url", "secret", "name", "scope", "email"}

type plan struct {
	baseURL *string
	// envelope nil keeps the stored secret.
	envelope *seal.Envelope
	last4    string
	scope    string
	account  account
	email    *string
}

// changes says whether a plan that keeps the secret still writes the scope or the email.
func (p *plan) changes(current *row) bool {
	return p.scope != current.Scope || !equal(p.email, current.Email)
}

// planScope is the row's scope after u: the one asked, else the stored one, else agent. A
// claude_login is the fleet's only: its scope is fleet, and any other scope asked is a 422.
func planScope(u update, current *row) (string, error) {
	if u.kind == claudeLogin {
		if u.scope != nil && *u.scope != "fleet" {
			return "", refused(422, `kind "claude_login" is the fleet's only: its scope must be "fleet"`)
		}
		return "fleet", nil
	}
	scope := "agent"
	if current != nil {
		scope = current.Scope
	}
	if u.scope != nil {
		scope = *u.scope
	}
	return scope, nil
}

// planEmail is a claude_login's email after u: the one asked ("" clears it), else the stored one.
func planEmail(u update, current *row) (*string, error) {
	if u.kind != claudeLogin {
		if u.email != nil && *u.email != "" {
			return nil, refused(400, `email applies to kind "claude_login" only`)
		}
		return nil, nil
	}
	if u.email == nil {
		if current != nil && current.Kind == claudeLogin {
			return current.Email, nil
		}
		return nil, nil
	}
	email := *u.email
	if email == "" {
		return nil, nil
	}
	if utf8.RuneCountInString(email) > emailMax || !strings.Contains(email, "@") ||
		strings.IndexFunc(email, func(r rune) bool { return pyIsSpace(r) || unicode.IsControl(r) }) >= 0 {
		return nil, refused(400, emailMessage)
	}
	return &email, nil
}

const (
	emailMessage   = "email must be one address, at most 320 characters, with no space or control character"
	loginMessage   = "a claude_login secret must be a claudeAiOauth JSON object"
	loginNeeds     = "a claude_login secret needs accessToken and refreshToken as text and expiresAt as a number"
	accountMessage = "%s must be text of at most 200 characters"
)

// loginAccount reads a claude_login's secret: a claudeAiOauth object with accessToken and
// refreshToken as text and expiresAt as a number. It answers the account in it and the last
// four characters of its access token.
func loginAccount(secret string) (account, string, error) {
	var o map[string]json.RawMessage
	if json.Unmarshal([]byte(secret), &o) != nil || o == nil {
		return account{}, "", refused(400, loginMessage)
	}
	var access, refresh string
	var expires float64
	for _, need := range []struct {
		name string
		into any
	}{{"accessToken", &access}, {"refreshToken", &refresh}, {"expiresAt", &expires}} {
		raw, ok := o[need.name]
		if !ok || string(raw) == "null" || json.Unmarshal(raw, need.into) != nil {
			return account{}, "", refused(400, loginNeeds)
		}
	}
	if access == "" || refresh == "" {
		return account{}, "", refused(400, loginNeeds)
	}
	var a account
	for _, field := range []struct {
		name string
		into **string
	}{
		{"accountUuid", &a.AccountUUID}, {"organizationUuid", &a.OrganizationUUID},
		{"subscriptionType", &a.SubscriptionType}, {"rateLimitTier", &a.RateLimitTier},
	} {
		raw, ok := o[field.name]
		if !ok || string(raw) == "null" {
			continue
		}
		var v string
		if json.Unmarshal(raw, &v) != nil || utf8.RuneCountInString(v) > accountFieldMax {
			return account{}, "", refused(400, fmt.Sprintf(accountMessage, field.name))
		}
		*field.into = &v
	}
	return a, last4(access), nil
}

// plan is credentials.ts planPut: its envelope nil to keep the stored secret (only when
// neither kind nor base_url changes).
func (s *Store) plan(u update, current *row, id string) (*plan, error) {
	var baseURL *string
	if u.kind == "gateway" {
		if u.baseURL == nil || *u.baseURL == "" {
			return nil, refused(400, `kind "gateway" needs base_url`)
		}
		checked, err := checkBaseURL(*u.baseURL)
		if err != nil {
			return nil, err
		}
		baseURL = &checked
	} else if u.baseURL != nil && *u.baseURL != "" {
		return nil, refused(400, `base_url applies to kind "gateway" only`)
	}
	scope, err := planScope(u, current)
	if err != nil {
		return nil, err
	}
	email, err := planEmail(u, current)
	if err != nil {
		return nil, err
	}
	p := &plan{baseURL: baseURL, scope: scope, email: email}
	if u.secret == nil {
		if current == nil {
			return nil, refused(400, "no credential is stored yet; secret is required")
		}
		if current.Kind != u.kind || !equal(current.BaseURL, baseURL) {
			return nil, refused(409, "changing kind or base_url needs the secret again: the stored one"+
				" is not sent to a new destination")
		}
		if current.SealVersion == seal.V1 {
			return nil, refused(409, legacyFormatMessage)
		}
		// The agent's reveal opens agent and both rows only. A fleet row that the agent's caller
		// widens must carry its secret, so that caller cannot open a secret it never knew.
		if current.Scope == "fleet" && scope != "fleet" {
			return nil, refused(422, wideningMessage)
		}
		return p, nil
	}
	secret := pyStrip(*u.secret)
	if secret == "" {
		return nil, refused(400, "secret is empty")
	}
	if u.kind == claudeLogin {
		// A claudeAiOauth object as Claude Code writes it: its whitespace is JSON's, not the token's.
		if p.account, p.last4, err = loginAccount(secret); err != nil {
			return nil, err
		}
	} else {
		if strings.IndexFunc(secret, pyIsSpace) >= 0 {
			return nil, refused(400, "secret must not contain whitespace")
		}
		p.last4 = last4(secret)
	}
	envelope := seal.SealSecret(s.Keys.Current, secret, credentialAAD(id, u.kind, baseURL))
	p.envelope = &envelope
	return p, nil
}

func equal(a, b *string) bool { return (a == nil) == (b == nil) && (a == nil || *a == *b) }

func (s *Store) insert(ctx context.Context, tx pgx.Tx, u update) (*row, error) {
	// Under the table lock, so racing creates cannot pass the cap together.
	var n int
	if err := tx.QueryRow(ctx, `SELECT count(*) AS n FROM ai_credentials`).Scan(&n); err != nil {
		return nil, err
	}
	if n >= maxCredentials {
		return nil, refused(409, tooManyMessage)
	}
	id := newUUID()
	name, err := checkName(u.name)
	if err != nil {
		return nil, err
	}
	p, err := s.plan(u, nil, id)
	if err != nil {
		return nil, err
	}
	if p.envelope == nil {
		return nil, errors.New("a create kept a secret that does not exist")
	}
	if _, err := tx.Exec(ctx, `INSERT INTO ai_credentials (id, priority, kind, name, base_url, secret_sealed,
		dek_sealed, kek_id, last4, scope, account_uuid, organization_uuid, subscription_type,
		rate_limit_tier, email) VALUES ($1, (SELECT COALESCE(MAX(priority) + 1, 0)
		FROM ai_credentials), $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)`,
		id, u.kind, name, p.baseURL, p.envelope.SecretSealed, p.envelope.DEKSealed, p.envelope.KEKID, p.last4,
		p.scope, p.account.AccountUUID, p.account.OrganizationUUID, p.account.SubscriptionType,
		p.account.RateLimitTier, p.email,
	); err != nil {
		return nil, err
	}
	if err := evict(ctx, tx); err != nil {
		return nil, err
	}
	return first(ctx, tx, &id)
}

func (s *Store) create(r *http.Request, _ crud.Params) (any, error) {
	f, err := readForm(r, updateFields...)
	if err != nil {
		return nil, err
	}
	u := readUpdate(f)
	if err := f.err(); err != nil {
		return nil, err
	}
	if u.secret == nil {
		return nil, refused(400, "secret is required for a new credential")
	}
	var out *row
	err = pgx.BeginFunc(r.Context(), s.Pool, func(tx pgx.Tx) error {
		if err := lock(r.Context(), tx); err != nil {
			return err
		}
		out, err = s.insert(r.Context(), tx, u)
		return err
	})
	if err != nil {
		return nil, err
	}
	return one(out), nil
}

// id is the optional credential id name of f.
func credentialID(f *form, required bool) *string {
	if required {
		return f.needText("id", 1, credentialIDMax, nil, false)
	}
	return f.text("id", 1, credentialIDMax, nil)
}

func (s *Store) save(r *http.Request, _ crud.Params) (any, error) {
	f, err := readForm(r, "id", "update")
	if err != nil {
		return nil, err
	}
	id := credentialID(f, false)
	var u update
	if n := f.nested("update", updateFields...); n != nil {
		u = readUpdate(n)
	} else if _, ok := f.required("update"); ok {
		f.fail([]string{"update"}, "Input should be a valid dictionary or object to extract fields from")
	}
	if err := f.err(); err != nil {
		return nil, err
	}
	var out *row
	err = pgx.BeginFunc(r.Context(), s.Pool, func(tx pgx.Tx) error {
		ctx := r.Context()
		if err := lock(ctx, tx); err != nil {
			return err
		}
		current, err := first(ctx, tx, id)
		if err != nil {
			return err
		}
		if current == nil {
			if id != nil {
				return refused(404, "no credential "+*id)
			}
			out, err = s.insert(ctx, tx, u)
			return err
		}
		p, err := s.plan(u, current, current.ID)
		if err != nil {
			return err
		}
		switch {
		case p.envelope == nil && !p.changes(current):
			out = current
			return nil
		case p.envelope == nil:
			// The scope or the email only: the secret and its epoch stay.
			if _, err := tx.Exec(ctx, `UPDATE ai_credentials SET scope = $1, email = $2 WHERE id = $3`,
				p.scope, p.email, current.ID); err != nil {
				return err
			}
		default:
			// A kind other than claude_login holds no lease.
			if _, err := tx.Exec(ctx, `UPDATE ai_credentials SET kind = $1, base_url = $2, secret_sealed = $3,
				dek_sealed = $4, kek_id = $5, last4 = $6, updated_at = now(),
				status = 'active', cooldown_until = NULL, last_error = NULL,
				last_error_at = NULL, epoch = epoch + 1, scope = $7, account_uuid = $8,
				organization_uuid = $9, subscription_type = $10, rate_limit_tier = $11, email = $12,
				leased_to = CASE WHEN $1 = 'claude_login' THEN leased_to END,
				leased_at = CASE WHEN $1 = 'claude_login' THEN leased_at END WHERE id = $13`,
				u.kind, p.baseURL, p.envelope.SecretSealed, p.envelope.DEKSealed, p.envelope.KEKID, p.last4,
				p.scope, p.account.AccountUUID, p.account.OrganizationUUID, p.account.SubscriptionType,
				p.account.RateLimitTier, p.email, current.ID); err != nil {
				return err
			}
		}
		if err := evict(ctx, tx); err != nil {
			return err
		}
		out, err = first(ctx, tx, &current.ID)
		return err
	})
	if err != nil {
		return nil, err
	}
	return one(out), nil
}

func (s *Store) delete(r *http.Request, _ crud.Params) (any, error) {
	f, err := readForm(r, "id")
	if err != nil {
		return nil, err
	}
	id := credentialID(f, false)
	if err := f.err(); err != nil {
		return nil, err
	}
	deleted := false
	err = pgx.BeginFunc(r.Context(), s.Pool, func(tx pgx.Tx) error {
		ctx := r.Context()
		if err := lock(ctx, tx); err != nil {
			return err
		}
		target := id
		if target == nil {
			current, err := first(ctx, tx, nil)
			if err != nil || current == nil {
				return err
			}
			target = &current.ID
		}
		tag, err := tx.Exec(ctx, `DELETE FROM ai_credentials WHERE id = $1`, *target)
		if err != nil || tag.RowsAffected() == 0 {
			return err
		}
		if _, err := tx.Exec(ctx, `UPDATE ai_credentials c SET priority = n.rn - 1 FROM (SELECT id,
			row_number() OVER (ORDER BY priority) AS rn FROM ai_credentials) n
			WHERE c.id = n.id AND c.priority <> n.rn - 1`); err != nil {
			return err
		}
		deleted = true
		return evict(ctx, tx)
	})
	if err != nil {
		return nil, err
	}
	return struct {
		Deleted bool `json:"deleted"`
	}{deleted}, nil
}

func (s *Store) order(r *http.Request, _ crud.Params) (any, error) {
	f, err := readForm(r, "ids")
	if err != nil {
		return nil, err
	}
	var ids []string
	if raw, ok := f.required("ids"); ok {
		var items []json.RawMessage
		if json.Unmarshal(raw, &items) != nil {
			f.fail([]string{"ids"}, "Input should be a valid list")
		} else if len(items) > maxCredentials {
			f.fail([]string{"ids"}, "List should have at most 100 items after validation")
		} else {
			for i, item := range items {
				if id := f.check([]string{"ids", fmt.Sprint(i)}, item, 1, credentialIDMax, nil); id != nil {
					ids = append(ids, *id)
				}
			}
		}
	}
	if err := f.err(); err != nil {
		return nil, err
	}
	var out []row
	err = pgx.BeginFunc(r.Context(), s.Pool, func(tx pgx.Tx) error {
		ctx := r.Context()
		if err := lock(ctx, tx); err != nil {
			return err
		}
		current, err := rows(ctx, tx, nil, false)
		if err != nil {
			return err
		}
		have := make([]string, 0, len(current))
		for _, c := range current {
			have = append(have, c.ID)
		}
		wanted := slices.Clone(ids)
		slices.Sort(wanted)
		slices.Sort(have)
		if len(slices.Compact(slices.Clone(wanted))) != len(ids) || !slices.Equal(wanted, have) {
			return refused(409, staleOrderMessage)
		}
		if _, err := tx.Exec(ctx, `UPDATE ai_credentials c SET priority = v.ord - 1 FROM unnest($1::text[])
			WITH ORDINALITY AS v(id, ord) WHERE c.id = v.id`, ids); err != nil {
			return err
		}
		if err := evict(ctx, tx); err != nil {
			return err
		}
		out, err = rows(ctx, tx, nil, false)
		return err
	})
	if err != nil {
		return nil, err
	}
	return listOf(out), nil
}

func (s *Store) reset(r *http.Request, _ crud.Params) (any, error) {
	f, err := readForm(r, "id")
	if err != nil {
		return nil, err
	}
	id := credentialID(f, true)
	if err := f.err(); err != nil {
		return nil, err
	}
	var out *row
	err = pgx.BeginFunc(r.Context(), s.Pool, func(tx pgx.Tx) error {
		ctx := r.Context()
		tag, err := tx.Exec(ctx, `UPDATE ai_credentials SET status = 'active', cooldown_until = NULL,
			last_error = NULL, last_error_at = NULL, epoch = epoch + 1 WHERE id = $1`, *id)
		if err != nil || tag.RowsAffected() == 0 {
			return err
		}
		if err := evict(ctx, tx); err != nil {
			return err
		}
		out, err = first(ctx, tx, id)
		return err
	})
	if err != nil {
		return nil, err
	}
	return one(out), nil
}

func (s *Store) rename(r *http.Request, _ crud.Params) (any, error) {
	f, err := readForm(r, "id", "name")
	if err != nil {
		return nil, err
	}
	id := credentialID(f, true)
	name := f.needText("name", 0, 1024, nil, true)
	if err := f.err(); err != nil {
		return nil, err
	}
	var out *row
	err = pgx.BeginFunc(r.Context(), s.Pool, func(tx pgx.Tx) error {
		ctx := r.Context()
		checked, err := checkName(name)
		if err != nil {
			return err
		}
		tag, err := tx.Exec(ctx, `UPDATE ai_credentials SET name = $1 WHERE id = $2`, checked, *id)
		if err != nil || tag.RowsAffected() == 0 {
			return err
		}
		out, err = first(ctx, tx, id)
		return err
	})
	if err != nil {
		return nil, err
	}
	return one(out), nil
}

const before = `SELECT id, status AS raw, CASE WHEN status = 'cooling_down' AND cooldown_until <= now()
	THEN 'active' ELSE status END AS status FROM ai_credentials
	WHERE id = @id AND epoch = @epoch FOR UPDATE`

var record = map[string]string{
	"used": `UPDATE ai_credentials c SET last_used_at = now(),
		status = CASE WHEN b.status = 'active' THEN 'active' ELSE c.status END,
		cooldown_until = CASE WHEN b.status = 'active' THEN NULL ELSE c.cooldown_until END`,
	"disabled": `UPDATE ai_credentials c SET status = 'disabled', cooldown_until = NULL,
		last_error = @reason, last_error_at = now()`,
	"cooling_down": `UPDATE ai_credentials c SET
		status = CASE WHEN c.status = 'disabled' THEN 'disabled' ELSE 'cooling_down' END,
		cooldown_until = CASE WHEN c.status = 'disabled' THEN NULL
		WHEN @replacing::timestamptz IS NOT NULL
		AND c.cooldown_until = @replacing::timestamptz THEN @until::timestamptz
		ELSE GREATEST(@until::timestamptz,
		CASE WHEN b.status = 'cooling_down' THEN c.cooldown_until END) END,
		last_error = @reason, last_error_at = now()`,
	"transient": `UPDATE ai_credentials c SET last_error = @reason, last_error_at = now()`,
}

// datetime reads the optional member name as pydantic reads a datetime; a time with no
// offset is taken as UTC.
func datetime(f *form, name string) *time.Time {
	raw := f.raw(name)
	if raw == nil {
		return nil
	}
	var s string
	if json.Unmarshal(raw, &s) == nil {
		s = strings.Replace(strings.TrimSuffix(s, "z"), " ", "T", 1)
		for _, layout := range []string{time.RFC3339Nano, "2006-01-02T15:04:05.999999999", "2006-01-02"} {
			if t, err := time.Parse(layout, s); err == nil {
				return &t
			}
		}
	}
	f.fail([]string{name}, "Input should be a valid datetime")
	return nil
}

func (s *Store) events(r *http.Request, _ crud.Params) (any, error) {
	f, err := readForm(r, "id", "epoch", "event")
	if err != nil {
		return nil, err
	}
	id := credentialID(f, true)
	epoch := f.integer("epoch", 0, 1<<31-1)
	var kind string
	var reason *string
	var until, replacing *time.Time
	if e := f.nested("event", "kind", "reason", "until", "replacing"); e != nil {
		kind = e.choice("kind", nil, "used", "disabled", "cooling_down", "transient")
		reason = e.text("reason", 0, 2000, nil)
		until, replacing = datetime(e, "until"), datetime(e, "replacing")
	} else if _, ok := f.required("event"); ok {
		f.fail([]string{"event"}, "Input should be a valid dictionary or object to extract fields from")
	}
	if err := f.err(); err != nil {
		return nil, err
	}
	if kind != "used" && reason == nil {
		return nil, httpx.WithReason(422, fmt.Sprintf("a %s event needs a reason", kind), credentialRefused)
	}
	if kind == "cooling_down" && until == nil {
		return nil, httpx.WithReason(422, "a cooling_down event needs until", credentialRefused)
	}
	sql := "WITH b AS (" + before + ") " + record[kind] +
		" FROM b WHERE c.id = b.id RETURNING b.status AS before, b.raw AS raw, c.status AS after"
	var found *struct{ before, raw, after string }
	err = pgx.BeginFunc(r.Context(), s.Pool, func(tx pgx.Tx) error {
		var b, raw, after string
		err := tx.QueryRow(r.Context(), sql, pgx.NamedArgs{
			"id": *id, "epoch": epoch, "reason": reason, "until": until, "replacing": replacing,
		}).Scan(&b, &raw, &after)
		if errors.Is(err, pgx.ErrNoRows) {
			return nil
		}
		found = &struct{ before, raw, after string }{b, raw, after}
		return err
	})
	if err != nil {
		return nil, err
	}
	type transition struct {
		Before    string `json:"before"`
		After     string `json:"after"`
		Recovered bool   `json:"recovered"`
	}
	out := struct {
		Transition *transition `json:"transition"`
	}{}
	if found != nil {
		out.Transition = &transition{found.before, found.after,
			found.raw == "cooling_down" && found.after == "active"}
	}
	return out, nil
}

// opened is the agent's Credential: the secret in plaintext.
type opened struct {
	Kind    string  `json:"kind"`
	BaseURL *string `json:"base_url"`
	Secret  string  `json:"secret"`
}

// open is credentials.ts openCredential; an error on a wrong KEK, an altered row, or a v1 row.
func open(kek *seal.KEK, r *row) (*opened, error) {
	if r.SealVersion == seal.V1 {
		return nil, errors.New(legacyFormatMessage)
	}
	secret, err := seal.OpenSecret(kek, seal.Envelope{
		SecretSealed: r.SecretSealed, DEKSealed: r.DEKSealed, KEKID: r.KEKID,
	}, credentialAAD(r.ID, r.Kind, r.BaseURL))
	if err != nil {
		return nil, err
	}
	if r.Kind == "gateway" {
		if r.BaseURL == nil {
			return nil, errors.New("gateway credential has no base_url")
		}
		return &opened{"gateway", r.BaseURL, secret}, nil
	}
	return &opened{Kind: r.Kind, Secret: secret}, nil
}

var uuidText = regexp.MustCompile(`^(?i)(urn:uuid:)?\{?[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}\}?$`)

// optionalUUID reads the member name as pydantic reads a UUID: in its canonical text.
func optionalUUID(f *form, name string) *string {
	raw := f.raw(name)
	if raw == nil {
		return nil
	}
	var s string
	if json.Unmarshal(raw, &s) != nil || !uuidText.MatchString(s) {
		f.fail([]string{name}, "Input should be a valid UUID")
		return nil
	}
	hex := strings.Map(func(r rune) rune {
		if strings.ContainsRune("0123456789abcdef", unicode.ToLower(r)) {
			return unicode.ToLower(r)
		}
		return -1
	}, strings.TrimPrefix(strings.ToLower(s), "urn:uuid:"))
	out := hex[:8] + "-" + hex[8:12] + "-" + hex[12:16] + "-" + hex[16:20] + "-" + hex[20:]
	return &out
}

type revealedRow struct {
	Stored     stored  `json:"stored"`
	Credential *opened `json:"credential"`
}

func (s *Store) reveal(r *http.Request, _ crud.Params) (any, error) {
	f, err := readForm(r, "id", "all", "purpose", "session_id", "turn_id")
	if err != nil {
		return nil, err
	}
	id := credentialID(f, false)
	all := f.boolean("all", false)
	purpose := f.choice("purpose", nil, "turn", "redact", "test")
	ids := audited{session: optionalUUID(f, "session_id"), turn: optionalUUID(f, "turn_id")}
	if err := f.err(); err != nil {
		return nil, err
	}
	kek := s.Keys.Current
	action := "reveal." + purpose
	out := struct {
		Credentials *[]revealedRow `json:"credentials"`
		Credential  *opened        `json:"credential"`
	}{}
	var sealErr error
	err = pgx.BeginFunc(r.Context(), s.Pool, func(tx pgx.Tx) error {
		ctx := r.Context()
		if all {
			found, err := rows(ctx, tx, nil, true)
			if err != nil {
				return err
			}
			list, count := []revealedRow{}, 0
			for i := range found {
				var o *opened
				if found[i].KEKID == kek.ID && found[i].SealVersion != seal.V1 {
					o, _ = open(kek, &found[i])
				}
				if o != nil {
					count++
				}
				list = append(list, revealedRow{found[i].stored(), o})
			}
			out.Credentials = &list
			// Redaction reads every credential at tool-call rate: one row a window says it did.
			// It gets the agent's own rows only. That is correct: the agent's process never holds
			// a fleet secret, so it has none to redact.
			if purpose == "redact" {
				lately, err := recordedLately(ctx, tx, "credential", []string{action}, redactAuditWindow, nil)
				if err != nil || lately {
					return err
				}
			}
			return audit(ctx, tx, "credential", action, "ok",
				fmt.Sprintf("opened %d of %d credential(s)", count, len(list)), ids)
		}
		found, err := rows(ctx, tx, id, true)
		if err != nil {
			return err
		}
		if len(found) == 0 {
			return audit(ctx, tx, "credential", action, "refused", "no such credential", ids)
		}
		o, err := open(kek, &found[0])
		if err != nil {
			sealErr = err
			return audit(ctx, tx, "credential", action, "error", found[0].label()+" did not open", ids)
		}
		out.Credential = o
		return audit(ctx, tx, "credential", action, "ok", found[0].label(), ids)
	})
	if err != nil {
		return nil, err
	}
	if sealErr != nil {
		return nil, httpx.WithReason(http.StatusConflict, sealErr.Error(), credentialSealed)
	}
	return out, nil
}
