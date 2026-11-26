package secrets

import (
	"context"
	"strings"
	"testing"
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

func TestAClaudeLoginIsSealedWithItsAccountInClear(t *testing.T) {
	a := newAPI(t, keysOf(kek))
	created := a.post("/credentials", obj{"kind": "claude_login", "secret": login, "email": "ops@example.com"})
	expect(t, created.status, 201)
	row := created.body["credential"].(obj)
	expect(t, []any{row["kind"], row["scope"], row["last4"], row["base_url"]}, []any{"claude_login", "fleet", "AAAA", nil})
	expect(t, []any{row["account_uuid"], row["organization_uuid"], row["subscription_type"], row["rate_limit_tier"], row["email"]},
		[]any{"8d2c3a4e-0000-4000-8000-00000000000a", "8d2c3a4e-0000-4000-8000-00000000000b", "max", "default_claude_max_20x", "ops@example.com"})
	expect(t, []any{row["leased_to"], row["leased_at"], row["lease_epoch"]}, []any{nil, nil, 0})
	listed := a.get("/credentials")
	one := a.get("/credentials/one?id=" + row["id"].(string))
	for _, raw := range []string{created.raw, listed.raw, one.raw} {
		if strings.Contains(raw, loginAccess) || strings.Contains(raw, loginRefresh) {
			t.Fatal("a token was answered outside a reveal")
		}
	}
	expect(t, listed.body["credentials"].([]any)[0].(obj)["scope"], "fleet")
	opened := a.post("/credentials/reveal", obj{"id": row["id"], "purpose": "test"}).body["credential"].(obj)
	expect(t, []any{opened["kind"], opened["secret"]}, []any{"claude_login", login})
}

func TestAClaudeLoginKeepsTheKindsRules(t *testing.T) {
	a := newAPI(t, keysOf(kek))
	refused := func(body obj) (int, any) {
		t.Helper()
		r := a.post("/credentials", body)
		return r.status, r.body["detail"]
	}
	for _, secret := range []string{"not json", `["a"]`, `{"accessToken": "x", "expiresAt": 1}`,
		`{"accessToken": "x", "refreshToken": "y", "expiresAt": "soon"}`,
		`{"accessToken": "x", "refreshToken": "y", "expiresAt": 1, "accountUuid": 7}`} {
		code, _ := refused(obj{"kind": "claude_login", "secret": secret})
		expect(t, code, 400)
	}
	code, _ := refused(obj{"kind": "claude_login", "secret": login, "scope": "agent"})
	expect(t, code, 400)
	code, _ = refused(obj{"kind": "claude_login", "secret": login, "base_url": "https://gw.example.com"})
	expect(t, code, 400)
	code, _ = refused(obj{"kind": "anthropic_api_key", "secret": apiKey, "email": "ops@example.com"})
	expect(t, code, 400)
	code, _ = refused(obj{"kind": "claude_login", "secret": login, "email": "two words@example.com"})
	expect(t, code, 400)
	code, _ = refused(obj{"kind": "anthropic_api_key", "secret": apiKey, "scope": "everyone"})
	expect(t, code, 422)
	both := a.post("/credentials", obj{"kind": "claude_login", "secret": login, "scope": "both"})
	expect(t, []any{both.status, both.body["credential"].(obj)["scope"], both.body["credential"].(obj)["email"]},
		[]any{201, "both", nil})
}

func TestTheScopeDefaultsToTheAgentAndASaveMovesIt(t *testing.T) {
	a := newAPI(t, keysOf(kek))
	key := a.post("/credentials", obj{"kind": "anthropic_api_key", "secret": apiKey}).body["credential"].(obj)
	expect(t, []any{key["scope"], key["account_uuid"], key["email"]}, []any{"agent", nil, nil})
	fleet := a.post("/credentials", obj{"kind": "anthropic_api_key", "secret": apiKey + "x", "scope": "fleet"})
	expect(t, fleet.body["credential"].(obj)["scope"], "fleet")
	// A scope change keeps the secret and its epoch.
	moved := a.post("/credentials/save", obj{"id": key["id"], "update": obj{"kind": "anthropic_api_key", "scope": "both"}})
	expect(t, []any{moved.status, moved.body["credential"].(obj)["scope"], moved.body["credential"].(obj)["epoch"]},
		[]any{200, "both", 0})
	// A save that names no scope keeps the stored one.
	kept := a.post("/credentials/save", obj{"id": key["id"], "update": obj{"kind": "anthropic_api_key", "secret": apiKey + "y"}})
	expect(t, []any{kept.body["credential"].(obj)["scope"], kept.body["credential"].(obj)["epoch"]}, []any{"both", 1})
	// A key that becomes a login moves to the fleet; a login that becomes a key loses its account.
	asLogin := a.post("/credentials/save", obj{"id": key["id"], "update": obj{"kind": "claude_login", "secret": login,
		"email": "ops@example.com"}}).body["credential"].(obj)
	expect(t, []any{asLogin["scope"], asLogin["subscription_type"], asLogin["email"]}, []any{"both", "max", "ops@example.com"})
	back := a.post("/credentials/save", obj{"id": key["id"], "update": obj{"kind": "anthropic_api_key", "secret": apiKey}})
	expect(t, []any{back.body["credential"].(obj)["account_uuid"], back.body["credential"].(obj)["email"]}, []any{nil, nil})
	agentKey := a.post("/credentials", obj{"kind": "anthropic_api_key", "secret": apiKey + "z"}).body["credential"].(obj)
	login2 := a.post("/credentials/save", obj{"id": agentKey["id"], "update": obj{"kind": "claude_login", "secret": login}})
	expect(t, login2.body["credential"].(obj)["scope"], "fleet")
	// The email of a stored login changes with no secret.
	mailed := a.post("/credentials/save", obj{"id": agentKey["id"], "update": obj{"kind": "claude_login", "email": "new@example.com"}})
	expect(t, []any{mailed.body["credential"].(obj)["email"], mailed.body["credential"].(obj)["epoch"]},
		[]any{"new@example.com", login2.body["credential"].(obj)["epoch"]})
}

func TestOnlyALoginHoldsALeaseOrAnAccount(t *testing.T) {
	a := newAPI(t, keysOf(kek))
	key := a.post("/credentials", obj{"kind": "anthropic_api_key", "secret": apiKey}).body["credential"].(obj)
	held := a.post("/credentials", obj{"kind": "claude_login", "secret": login}).body["credential"].(obj)
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
