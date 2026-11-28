package secrets

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"testing"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/seal"
)

// #7983: the pool's claude_login kind, its scope and its machine lease.

const (
	loginAccess  = "sk-ant-oat01-access-token-vector-AAAA"
	loginRefresh = "sk-ant-ort01-refresh-token-vector-BBBB"
)

// login is a claudeAiOauth object as Claude Code writes it, with a space and a newline in it.
const login = `{"accessToken": "` + loginAccess + `", "refreshToken": "` + loginRefresh + `",
	"expiresAt": 1795000000000, "refreshTokenExpiresAt": null,
	"accountUuid": "8d2c3a4e-0000-4000-8000-00000000000a",
	"organizationUuid": "8d2c3a4e-0000-4000-8000-00000000000b",
	"rateLimitTier": "default_claude_max_20x", "scopes": ["user:inference", "user:profile"],
	"subscriptionType": "max", "subscriptionTypeSource": "account"}`

func text(s string) *string { return &s }

// fleetWrite is a create (or, with id, a save) by a caller other than the agent's: the agent's
// routes refuse a claude_login, a fleet row and a row whose scope is both (#8587), so the fleet's
// rows are written here. It answers the stored row as the routes do, or the refusal's status.
func (a *api) fleetWrite(u update, id string) (obj, int) {
	a.t.Helper()
	s := &Store{Pool: a.pool, Keys: keysOf(kek)}
	var r *row
	var err error
	if id == "" {
		r, err = s.createRow(context.Background(), u, false)
	} else {
		r, err = s.saveRow(context.Background(), &id, u, false)
	}
	var problem *httpx.Problem
	if errors.As(err, &problem) {
		return nil, problem.Status
	}
	if err != nil {
		a.t.Fatal(err)
	}
	raw, _ := json.Marshal(r.stored())
	var out obj
	_ = json.Unmarshal(raw, &out)
	return out, 200
}

// fleet is fleetWrite that must succeed.
func (a *api) fleet(u update, id string) obj {
	a.t.Helper()
	out, status := a.fleetWrite(u, id)
	if status != 200 {
		a.t.Fatalf("the fleet's write was refused: %d", status)
	}
	return out
}

func TestAClaudeLoginIsSealedWithItsAccountInClear(t *testing.T) {
	a := newAPI(t, keysOf(kek))
	row := a.fleet(update{kind: claudeLogin, secret: text(login), email: text("ops@example.com")}, "")
	expect(t, []any{row["kind"], row["scope"], row["last4"], row["base_url"]}, []any{"claude_login", "fleet", "AAAA", nil})
	expect(t, []any{row["account_uuid"], row["organization_uuid"], row["subscription_type"], row["rate_limit_tier"], row["email"]},
		[]any{"8d2c3a4e-0000-4000-8000-00000000000a", "8d2c3a4e-0000-4000-8000-00000000000b", "max", "default_claude_max_20x", "ops@example.com"})
	expect(t, []any{row["leased_to"], row["leased_at"], row["lease_epoch"]}, []any{nil, nil, 0})
	listed := a.get("/credentials")
	one := a.get("/credentials/one?id=" + row["id"].(string))
	for _, raw := range []string{listed.raw, one.raw} {
		if strings.Contains(raw, loginAccess) || strings.Contains(raw, loginRefresh) {
			t.Fatal("a token was answered outside a reveal")
		}
	}
	expect(t, listed.body["credentials"].([]any)[0].(obj)["scope"], "fleet")
	expect(t, one.body["credential"].(obj)["email"], "ops@example.com")
	// The agent's reveal never opens a fleet row: as for an unknown id.
	expect(t, a.post("/credentials/reveal", obj{"id": row["id"], "purpose": "test"}).body, obj{"credential": nil, "credentials": nil})
	sealed := a.rows(`SELECT secret_sealed, dek_sealed, kek_id FROM ai_credentials WHERE id = $1`, row["id"])[0]
	secret, err := seal.OpenSecret(kek, seal.Envelope{SecretSealed: sealed["secret_sealed"].([]byte),
		DEKSealed: sealed["dek_sealed"].([]byte), KEKID: sealed["kek_id"].(string)},
		credentialAAD(row["id"].(string), "claude_login", nil))
	expect(t, []any{secret, err}, []any{login, nil})
}

func TestTheAgentsRevealNeverOpensAFleetRow(t *testing.T) {
	a := newAPI(t, keysOf(kek))
	login := a.fleet(update{kind: claudeLogin, secret: text(login)}, "")
	fleet := a.fleet(update{kind: "anthropic_api_key", secret: text(apiKey + "f"), scope: text("fleet")}, "")
	own := a.post("/credentials", obj{"kind": "anthropic_api_key", "secret": apiKey}).body["credential"].(obj)
	both := a.fleet(update{kind: "anthropic_api_key", secret: text(apiKey + "b"), scope: text("both")}, "")
	all := a.post("/credentials/reveal", obj{"all": true, "purpose": "turn"})
	var ids []any
	for _, r := range all.body["credentials"].([]any) {
		ids = append(ids, r.(obj)["stored"].(obj)["id"])
	}
	expect(t, ids, []any{own["id"], both["id"]})
	if strings.Contains(all.raw, loginRefresh) || strings.Contains(all.raw, apiKey+"f") {
		t.Fatal("a fleet secret reached the agent")
	}
	for _, id := range []any{login["id"], fleet["id"]} {
		one := a.post("/credentials/reveal", obj{"id": id, "purpose": "test"})
		expect(t, []any{one.status, one.body}, []any{200, obj{"credential": nil, "credentials": nil}})
	}
	// With no id, the first of the agent's rows, past the fleet's.
	first := a.post("/credentials/reveal", obj{"purpose": "test"}).body["credential"].(obj)
	expect(t, first["secret"], apiKey)
	// The list still names every row, with its scope.
	expect(t, len(a.get("/credentials").body["credentials"].([]any)), 4)
}

// A fleet machine installs a claude_login, so the agent's caller may not plant one, nor replace
// (or change) a row whose scope is fleet, nor make a row the fleet's.
func TestTheAgentsCallerNeverWritesAFleetRow(t *testing.T) {
	a := newAPI(t, keysOf(kek))
	for _, body := range []obj{
		{"kind": "claude_login", "secret": login},
		{"kind": "claude_login", "secret": login, "scope": "fleet"},
		{"kind": "anthropic_api_key", "secret": apiKey, "scope": "fleet"},
	} {
		r := a.post("/credentials", body)
		expect(t, []any{r.status, r.body["reason"], r.body["detail"]}, []any{409, credentialRefused, fleetRowMessage})
	}
	loginRow := a.fleet(update{kind: claudeLogin, secret: text(login)}, "")
	fleet := a.fleet(update{kind: "anthropic_api_key", secret: text(apiKey + "f"), scope: text("fleet")}, "")
	own := a.post("/credentials", obj{"kind": "anthropic_api_key", "secret": apiKey}).body["credential"].(obj)
	for _, save := range []struct {
		id     any
		update obj
	}{
		{loginRow["id"], obj{"kind": "claude_login", "secret": strings.Replace(login, "AAAA", "CCCC", 1)}},
		{loginRow["id"], obj{"kind": "claude_login", "email": "other@example.com"}},
		{loginRow["id"], obj{"kind": "anthropic_api_key", "secret": apiKey}},
		{fleet["id"], obj{"kind": "anthropic_api_key", "secret": apiKey + "x"}},
		{fleet["id"], obj{"kind": "anthropic_api_key", "secret": apiKey + "x", "scope": "both"}},
		{own["id"], obj{"kind": "claude_login", "secret": login}},
		{own["id"], obj{"kind": "anthropic_api_key", "scope": "fleet"}},
	} {
		r := a.post("/credentials/save", obj{"id": save.id, "update": save.update})
		expect(t, []any{save.update, r.status, r.body["reason"]}, []any{save.update, 409, credentialRefused})
	}
	listed := map[any]obj{}
	for _, c := range a.get("/credentials").body["credentials"].([]any) {
		listed[c.(obj)["id"]] = c.(obj)
	}
	expect(t, len(listed), 3)
	expect(t, []any{listed[loginRow["id"]]["epoch"], listed[fleet["id"]]["epoch"], listed[own["id"]]["epoch"]}, []any{0, 0, 0})
	expect(t, []any{listed[loginRow["id"]]["kind"], listed[loginRow["id"]]["email"]}, []any{"claude_login", nil})
	expect(t, listed[own["id"]]["scope"], "agent")
	secrets := map[string]string{}
	for _, r := range a.rows(`SELECT id, secret_sealed, dek_sealed, kek_id, kind, base_url FROM ai_credentials`) {
		base, _ := r["base_url"].(*string)
		secret, err := seal.OpenSecret(kek, seal.Envelope{SecretSealed: r["secret_sealed"].([]byte),
			DEKSealed: r["dek_sealed"].([]byte), KEKID: r["kek_id"].(string)},
			credentialAAD(r["id"].(string), r["kind"].(string), base))
		if err != nil {
			t.Fatal(err)
		}
		secrets[r["id"].(string)] = secret
	}
	expect(t, secrets, map[string]string{loginRow["id"].(string): login, fleet["id"].(string): apiKey + "f",
		own["id"].(string): apiKey})
}

func TestAClaudeLoginKeepsTheKindsRules(t *testing.T) {
	a := newAPI(t, keysOf(kek))
	refused := func(u update) int {
		t.Helper()
		_, status := a.fleetWrite(u, "")
		return status
	}
	for _, secret := range []string{"not json", `["a"]`, `{"accessToken": "x", "expiresAt": 1}`,
		`{"accessToken": "x", "refreshToken": "y", "expiresAt": "soon"}`,
		`{"accessToken": "x", "refreshToken": "y", "expiresAt": 1, "accountUuid": 7}`} {
		expect(t, refused(update{kind: claudeLogin, secret: text(secret)}), 400)
	}
	for _, scope := range []string{"agent", "both"} {
		expect(t, refused(update{kind: claudeLogin, secret: text(login), scope: text(scope)}), 422)
	}
	expect(t, refused(update{kind: claudeLogin, secret: text(login), baseURL: text("https://gw.example.com")}), 400)
	expect(t, refused(update{kind: "anthropic_api_key", secret: text(apiKey), email: text("ops@example.com")}), 400)
	expect(t, refused(update{kind: claudeLogin, secret: text(login), email: text("two words@example.com")}), 400)
	code := a.post("/credentials", obj{"kind": "anthropic_api_key", "secret": apiKey, "scope": "everyone"}).status
	expect(t, code, 422)
	fleet := a.fleet(update{kind: claudeLogin, secret: text(login), scope: text("fleet")}, "")
	expect(t, []any{fleet["scope"], fleet["email"]}, []any{"fleet", nil})
}

// A null scope keeps the stored one, as in credentials.py.
func TestANullScopeKeepsTheStoredOne(t *testing.T) {
	a := newAPI(t, keysOf(kek))
	created := a.post("/credentials", obj{"kind": "anthropic_api_key", "secret": apiKey, "scope": nil})
	expect(t, []any{created.status, created.body["credential"].(obj)["scope"]}, []any{201, "agent"})
	id := created.body["credential"].(obj)["id"]
	kept := a.post("/credentials/save", obj{"id": id, "update": obj{"kind": "anthropic_api_key", "secret": apiKey + "n", "scope": nil}})
	expect(t, []any{kept.status, kept.body["credential"].(obj)["scope"]}, []any{200, "agent"})
	// Only a caller other than the agent's widens a row to both (#8587); its save with no scope
	// keeps that one too.
	a.fleet(update{kind: "anthropic_api_key", scope: text("both")}, id.(string))
	shared := a.fleet(update{kind: "anthropic_api_key", secret: text(apiKey + "m")}, id.(string))
	expect(t, shared["scope"], "both")
}

func TestTheScopeDefaultsToTheAgentAndASaveMovesIt(t *testing.T) {
	a := newAPI(t, keysOf(kek))
	key := a.post("/credentials", obj{"kind": "anthropic_api_key", "secret": apiKey}).body["credential"].(obj)
	expect(t, []any{key["scope"], key["account_uuid"], key["email"]}, []any{"agent", nil, nil})
	ownScope := a.post("/credentials/save", obj{"id": key["id"], "update": obj{"kind": "anthropic_api_key", "scope": "agent"}})
	expect(t, []any{ownScope.status, ownScope.body["credential"].(obj)["scope"]}, []any{200, "agent"})
	// A scope change keeps the secret and its epoch. Only a caller other than the agent's widens a
	// row to both (#8587).
	id := key["id"].(string)
	moved := a.fleet(update{kind: "anthropic_api_key", scope: text("both")}, id)
	expect(t, []any{moved["scope"], moved["epoch"]}, []any{"both", 0})
	// A save that names no scope keeps the stored one.
	kept := a.fleet(update{kind: "anthropic_api_key", secret: text(apiKey + "y")}, id)
	expect(t, []any{kept["scope"], kept["epoch"]}, []any{"both", 1})
	// The fleet's caller turns a key into a login, which moves to the fleet; a login that becomes
	// a key loses its account.
	asLogin := a.fleet(update{kind: claudeLogin, secret: text(login), email: text("ops@example.com")}, id)
	expect(t, []any{asLogin["scope"], asLogin["subscription_type"], asLogin["email"]}, []any{"fleet", "max", "ops@example.com"})
	back := a.fleet(update{kind: "anthropic_api_key", secret: text(apiKey)}, id)
	expect(t, []any{back["account_uuid"], back["email"]}, []any{nil, nil})
	agentKey := a.post("/credentials", obj{"kind": "anthropic_api_key", "secret": apiKey + "z"}).body["credential"].(obj)
	login2 := a.fleet(update{kind: claudeLogin, secret: text(login)}, agentKey["id"].(string))
	expect(t, login2["scope"], "fleet")
	// The email of a stored login changes with no secret.
	mailed := a.fleet(update{kind: claudeLogin, email: text("new@example.com")}, agentKey["id"].(string))
	expect(t, []any{mailed["email"], mailed["epoch"]}, []any{"new@example.com", login2["epoch"]})
}

func TestAScopeOrEmailSaveMovesUpdatedAt(t *testing.T) {
	a := newAPI(t, keysOf(kek))
	key := a.post("/credentials", obj{"kind": "anthropic_api_key", "secret": apiKey}).body["credential"].(obj)
	held := a.fleet(update{kind: claudeLogin, secret: text(login)}, "")
	a.sql(`UPDATE ai_credentials SET updated_at = '2026-01-01T00:00:00Z'`)
	moved := a.fleet(update{kind: "anthropic_api_key", scope: text("both")}, key["id"].(string))
	mailed := a.fleet(update{kind: claudeLogin, email: text("ops@example.com")}, held["id"].(string))
	for _, r := range []obj{moved, mailed} {
		expect(t, r["epoch"], 0)
		if strings.HasPrefix(r["updated_at"].(string), "2026-01-01") {
			t.Fatalf("updated_at stayed: %v", r)
		}
	}
}

func TestOnlyALoginHoldsALeaseOrAnAccount(t *testing.T) {
	a := newAPI(t, keysOf(kek))
	key := a.post("/credentials", obj{"kind": "anthropic_api_key", "secret": apiKey}).body["credential"].(obj)
	held := a.fleet(update{kind: claudeLogin, secret: text(login)}, "")
	if _, err := a.pool.Exec(context.Background(), `UPDATE ai_credentials SET scope = 'both' WHERE id = $1`, held["id"]); err == nil {
		t.Fatal("a claude_login took scope both")
	}
	for _, q := range []string{
		`UPDATE ai_credentials SET leased_to = 'm1', leased_at = now() WHERE id = $1`,
		`UPDATE ai_credentials SET email = 'ops@example.com' WHERE id = $1`,
		`UPDATE ai_credentials SET scope = 'nobody' WHERE id = $1`,
	} {
		if _, err := a.pool.Exec(context.Background(), q, key["id"]); err == nil {
			t.Fatalf("%s: no check refused it", q)
		}
	}
	if _, err := a.pool.Exec(context.Background(), `UPDATE ai_credentials SET leased_to = 'm1' WHERE id = $1`, held["id"]); err == nil {
		t.Fatal("a lease with no time was taken")
	}
	a.sql(`UPDATE ai_credentials SET leased_to = 'm1', leased_at = '2026-11-26T00:00:00Z', lease_epoch = 3 WHERE id = $1`, held["id"])
	leased := a.get("/credentials/one?id=" + held["id"].(string)).body["credential"].(obj)
	expect(t, []any{leased["leased_to"], leased["leased_at"], leased["lease_epoch"]}, []any{"m1", "2026-11-26T00:00:00+00:00", 3})
}

func TestAKindChangeThatEndsALeaseBumpsLeaseEpoch(t *testing.T) {
	a := newAPI(t, keysOf(kek))
	id := a.fleet(update{kind: claudeLogin, secret: text(login)}, "")["id"].(string)
	a.sql(`UPDATE ai_credentials SET leased_to = 'm1', leased_at = now(), lease_epoch = 3 WHERE id = $1`, id)
	// The lease ends with the kind, and each lease change bumps lease_epoch, so the machine that
	// held it cannot write back.
	ended := a.fleet(update{kind: "anthropic_api_key", secret: text(apiKey)}, id)
	expect(t, []any{ended["leased_to"], ended["leased_at"], ended["lease_epoch"]}, []any{nil, nil, 4})
	// A kind change with no lease held changes no lease, so the epoch stays.
	a.fleet(update{kind: claudeLogin, secret: text(login)}, id)
	again := a.fleet(update{kind: "anthropic_api_key", secret: text(apiKey)}, id)
	expect(t, again["lease_epoch"], 4)
}

func TestWideningAFleetRowToTheAgentNeedsItsSecretAgain(t *testing.T) {
	a := newAPI(t, keysOf(kek))
	id := a.fleet(update{kind: "anthropic_api_key", secret: text(apiKey + "f"), scope: text("fleet")}, "")["id"].(string)
	for _, scope := range []string{"agent", "both"} {
		_, status := a.fleetWrite(update{kind: "anthropic_api_key", scope: text(scope)}, id)
		expect(t, status, 422)
		row := a.get("/credentials/one?id=" + id).body["credential"].(obj)
		expect(t, []any{row["scope"], row["epoch"]}, []any{"fleet", 0})
		expect(t, a.post("/credentials/reveal", obj{"id": id, "purpose": "test"}).body,
			obj{"credential": nil, "credentials": nil})
	}
	// With its secret in the same save, the row widens and its epoch moves.
	widened := a.fleet(update{kind: "anthropic_api_key", secret: text(apiKey + "w"), scope: text("both")}, id)
	expect(t, []any{widened["scope"], widened["epoch"]}, []any{"both", 1})
	expect(t, a.post("/credentials/reveal", obj{"id": id, "purpose": "test"}).body["credential"].(obj)["secret"],
		apiKey+"w")
	// A narrowing needs no secret and keeps the epoch.
	narrowed := a.fleet(update{kind: "anthropic_api_key", scope: text("fleet")}, id)
	expect(t, []any{narrowed["scope"], narrowed["epoch"]}, []any{"fleet", 1})
}

// state is every row's priority, name, status, epoch and last error, by id.
func (a *api) state() map[any][]any {
	a.t.Helper()
	out := map[any][]any{}
	for _, c := range a.get("/credentials").body["credentials"].([]any) {
		r := c.(obj)
		out[r["id"]] = []any{r["priority"], r["name"], r["status"], r["epoch"], r["last_error"]}
	}
	return out
}

// A compromised agent pod may not delete, rename, reset, cool down, disable or reorder a fleet
// login (#7983): each is the save's 409, and the row is as it was.
func TestTheAgentsCallerChangesNoFleetRow(t *testing.T) {
	a := newAPI(t, keysOf(kek))
	loginRow := a.fleet(update{kind: claudeLogin, secret: text(login)}, "")
	own := a.post("/credentials", obj{"kind": "anthropic_api_key", "secret": apiKey}).body["credential"].(obj)
	fleet := a.fleet(update{kind: "anthropic_api_key", secret: text(apiKey + "f"), scope: text("fleet")}, "")
	both := a.fleet(update{kind: "anthropic_api_key", secret: text(apiKey + "b"), scope: text("both")}, "")
	before := a.state()
	for _, row := range []obj{loginRow, fleet} {
		for _, c := range []struct {
			path string
			body obj
		}{
			{"/credentials/delete", obj{"id": row["id"]}},
			{"/credentials/reset", obj{"id": row["id"]}},
			{"/credentials/name", obj{"id": row["id"], "name": "Mine"}},
			{"/credentials/events", obj{"id": row["id"], "epoch": 0, "event": obj{"kind": "used"}}},
			{"/credentials/events", obj{"id": row["id"], "epoch": 0, "event": obj{"kind": "disabled", "reason": "401"}}},
			{"/credentials/events", obj{"id": row["id"], "epoch": 0,
				"event": obj{"kind": "cooling_down", "until": "2099-01-01T00:00:00Z", "reason": "429"}}},
		} {
			r := a.post(c.path, c.body)
			expect(t, []any{c.path, r.status, r.body["reason"], r.body["detail"]},
				[]any{c.path, 409, credentialRefused, fleetRowMessage})
		}
	}
	// An order that names a fleet row, or every row, is refused; the agent's rows alone move.
	for _, ids := range [][]any{
		{both["id"], own["id"], loginRow["id"], fleet["id"]},
		{loginRow["id"], both["id"], own["id"]},
	} {
		r := a.post("/credentials/order", obj{"ids": ids})
		expect(t, []any{r.status, r.body["detail"]}, []any{409, fleetRowMessage})
	}
	expect(t, a.state(), before)
	order := a.post("/credentials/order", obj{"ids": []any{both["id"], own["id"]}})
	expect(t, order.status, 200)
	var got []any
	for _, c := range order.body["credentials"].([]any) {
		got = append(got, c.(obj)["id"])
	}
	expect(t, got, []any{loginRow["id"], both["id"], fleet["id"], own["id"]})
	// With no id, a read, a save and a delete go to the first row the agent uses, never the
	// fleet's login before it. That row's scope is both, so the save and the delete are refused
	// (#8587).
	expect(t, a.get("/credentials/one").body["credential"].(obj)["id"], both["id"])
	saved := a.post("/credentials/save", obj{"update": obj{"kind": "anthropic_api_key", "secret": "k"}})
	expect(t, []any{saved.status, saved.body["detail"]}, []any{409, sharedRowMessage})
	gone := a.post("/credentials/delete", obj{})
	expect(t, []any{gone.status, gone.body["detail"]}, []any{409, sharedRowMessage})
	expect(t, a.post("/credentials/delete", obj{"id": own["id"]}).body, obj{"deleted": true})
	left := a.state()
	expect(t, len(left), 3)
	// The rest close up; the fleet's rows are otherwise as they were.
	expect(t, left[loginRow["id"]], before[loginRow["id"]])
	expect(t, left[both["id"]], append([]any{1}, before[both["id"]][1:]...))
	expect(t, left[fleet["id"]], before[fleet["id"]])
}

// expectShared checks that the agent's caller was refused a change to a row the fleet reads too.
func expectShared(t *testing.T, what any, r answer) {
	t.Helper()
	expect(t, []any{what, r.status, r.body["reason"], r.body["detail"]},
		[]any{what, 409, credentialRefused, sharedRowMessage})
}

// The fleet's rotator reads a row whose scope is both (#8587), so the agent's caller may not make
// one, nor move its own row to both.
func TestTheAgentsCallerNeverWidensARowToTheFleet(t *testing.T) {
	a := newAPI(t, keysOf(kek))
	for _, body := range []obj{
		{"kind": "anthropic_api_key", "secret": apiKey, "scope": "both"},
		{"kind": "gateway", "base_url": "https://gw.example.com", "secret": apiKey, "scope": "both"},
	} {
		expectShared(t, body, a.post("/credentials", body))
	}
	own := a.post("/credentials", obj{"kind": "anthropic_api_key", "secret": apiKey, "scope": "agent"}).body["credential"].(obj)
	expect(t, own["scope"], "agent")
	for _, u := range []obj{
		{"kind": "anthropic_api_key", "scope": "both"},
		{"kind": "anthropic_api_key", "secret": apiKey + "x", "scope": "both"},
	} {
		expectShared(t, u, a.post("/credentials/save", obj{"id": own["id"], "update": u}))
	}
	// With no id, the save goes to the agent's first row, and is refused the same way.
	expectShared(t, "no id", a.post("/credentials/save", obj{"update": obj{"kind": "anthropic_api_key", "scope": "both"}}))
	listed := a.get("/credentials").body["credentials"].([]any)
	expect(t, len(listed), 1)
	row := listed[0].(obj)
	expect(t, []any{row["id"], row["scope"], row["epoch"]}, []any{own["id"], "agent", 0})
	opened := a.post("/credentials/reveal", obj{"id": own["id"], "purpose": "test"})
	expect(t, opened.body["credential"].(obj)["secret"], apiKey)
}

// A row whose scope is both (#8587): the agent's caller may not change its secret, kind, base URL
// or scope, delete it or disable it. Each is a 409, and the row is as it was.
func TestTheAgentsCallerNeverRewritesASharedRow(t *testing.T) {
	a := newAPI(t, keysOf(kek))
	shared := a.fleet(update{kind: "anthropic_api_key", secret: text(apiKey + "b"), scope: text("both")}, "")
	gateway := a.fleet(update{kind: "gateway", baseURL: text("https://gw.example.com"), secret: text(apiKey + "g"),
		scope: text("both")}, "")
	before := a.state()
	for _, c := range []struct {
		id     any
		update obj
	}{
		{shared["id"], obj{"kind": "anthropic_api_key", "secret": apiKey + "x"}},
		{shared["id"], obj{"kind": "anthropic_api_key", "secret": apiKey + "x", "scope": "agent"}},
		{shared["id"], obj{"kind": "anthropic_api_key", "scope": "agent"}},
		{shared["id"], obj{"kind": "claude_oauth_token", "secret": "sk-ant-oat01-x"}},
		{shared["id"], obj{"kind": "gateway", "base_url": "https://evil.example.com", "secret": apiKey}},
		{gateway["id"], obj{"kind": "gateway", "base_url": "https://evil.example.com", "secret": apiKey}},
		{gateway["id"], obj{"kind": "gateway", "base_url": "https://gw.example.com"}},
	} {
		expectShared(t, c.update, a.post("/credentials/save", obj{"id": c.id, "update": c.update}))
	}
	for _, row := range []obj{shared, gateway} {
		expectShared(t, "delete", a.post("/credentials/delete", obj{"id": row["id"]}))
		expectShared(t, "disable", a.post("/credentials/events",
			obj{"id": row["id"], "epoch": 0, "event": obj{"kind": "disabled", "reason": "401"}}))
	}
	expectShared(t, "delete with no id", a.post("/credentials/delete", obj{}))
	expect(t, a.state(), before)
	for _, c := range []struct {
		row     obj
		kind    string
		baseURL any
		secret  string
	}{
		{shared, "anthropic_api_key", nil, apiKey + "b"},
		{gateway, "gateway", "https://gw.example.com", apiKey + "g"},
	} {
		row := a.get("/credentials/one?id=" + c.row["id"].(string)).body["credential"].(obj)
		expect(t, []any{row["kind"], row["base_url"], row["scope"]}, []any{c.kind, c.baseURL, "both"})
		opened := a.post("/credentials/reveal", obj{"id": c.row["id"], "purpose": "test"})
		expect(t, opened.body["credential"].(obj)["secret"], c.secret)
	}
}

// The agent uses a row whose scope is both (#8587): it opens it, renames, resets and reorders it,
// and records its use, a rate limit and a transient error on it.
func TestTheAgentStillUsesASharedRow(t *testing.T) {
	a := newAPI(t, keysOf(kek))
	shared := a.fleet(update{kind: "anthropic_api_key", secret: text(apiKey + "b"), scope: text("both")}, "")
	own := a.post("/credentials", obj{"kind": "anthropic_api_key", "secret": apiKey}).body["credential"].(obj)
	opened := a.post("/credentials/reveal", obj{"id": shared["id"], "purpose": "test"})
	expect(t, opened.body["credential"].(obj)["secret"], apiKey+"b")
	for _, c := range []struct {
		event obj
		after string
	}{
		{obj{"kind": "used"}, "active"},
		{obj{"kind": "transient", "reason": "503"}, "active"},
		{obj{"kind": "cooling_down", "until": "2099-01-01T00:00:00Z", "reason": "429"}, "cooling_down"},
	} {
		r := a.post("/credentials/events", obj{"id": shared["id"], "epoch": 0, "event": c.event})
		expect(t, []any{c.event["kind"], r.status, r.body["transition"].(obj)["after"]}, []any{c.event["kind"], 200, c.after})
	}
	reset := a.post("/credentials/reset", obj{"id": shared["id"]}).body["credential"].(obj)
	expect(t, []any{reset["status"], reset["epoch"]}, []any{"active", 1})
	renamed := a.post("/credentials/name", obj{"id": shared["id"], "name": "Shared"}).body["credential"].(obj)
	expect(t, renamed["name"], "Shared")
	ordered := a.post("/credentials/order", obj{"ids": []any{own["id"], shared["id"]}})
	var got []any
	for _, c := range ordered.body["credentials"].([]any) {
		got = append(got, c.(obj)["id"])
	}
	expect(t, got, []any{own["id"], shared["id"]})
	row := a.get("/credentials/one?id=" + shared["id"].(string)).body["credential"].(obj)
	expect(t, []any{row["scope"], row["kind"]}, []any{"both", "anthropic_api_key"})
}

// agent false (the fleet's own caller, #7984) still deletes, renames, resets, records and reorders
// a fleet row.
func TestTheFleetsCallerStillChangesItsRows(t *testing.T) {
	a := newAPI(t, keysOf(kek))
	loginRow := a.fleet(update{kind: claudeLogin, secret: text(login)}, "")
	fleet := a.fleet(update{kind: "anthropic_api_key", secret: text(apiKey + "f"), scope: text("fleet")}, "")
	own := a.post("/credentials", obj{"kind": "anthropic_api_key", "secret": apiKey}).body["credential"].(obj)
	s := &Store{Pool: a.pool, Keys: keysOf(kek)}
	ctx := context.Background()
	loginID, fleetID, ownID := loginRow["id"].(string), fleet["id"].(string), own["id"].(string)
	if r, err := s.renameRow(ctx, loginID, text("Ops"), false); err != nil || r == nil {
		t.Fatal(r, err)
	}
	found, err := s.recordEvent(ctx, loginID, 0, "disabled", text("401"), nil, nil, false)
	if err != nil || found == nil || found.After != "disabled" {
		t.Fatal(found, err)
	}
	reset, err := s.resetRow(ctx, loginID, false)
	if err != nil || reset == nil {
		t.Fatal(reset, err)
	}
	expect(t, []any{reset.Status, reset.Epoch}, []any{"active", 1})
	ordered, err := s.orderRows(ctx, []string{ownID, fleetID, loginID}, false)
	if err != nil {
		t.Fatal(err)
	}
	expect(t, []any{ordered[0].ID, ordered[1].ID, ordered[2].ID}, []any{ownID, fleetID, loginID})
	if gone, err := s.deleteRow(ctx, &fleetID, false); err != nil || !gone {
		t.Fatal(gone, err)
	}
	if gone, err := s.deleteRow(ctx, nil, false); err != nil || !gone {
		t.Fatal(gone, err)
	}
	listed := a.get("/credentials").body["credentials"].([]any)
	expect(t, len(listed), 1)
	expect(t, []any{listed[0].(obj)["id"], listed[0].(obj)["name"]}, []any{loginID, "Ops"})
}
