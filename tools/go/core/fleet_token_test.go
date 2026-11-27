package main

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"strings"
	"testing"
)

// fleetd's personal access token (#8041, #8179): scope `fleet` reaches core's fleet-rotator
// routes and nothing else; a `read` or `write` token reaches none of them; one token is one
// machine.

// accessToken is a live personal access token of scope, as Settings → Access tokens mints
// one (auth/personal_tokens.py mint), and its id.
func accessToken(a *api, scope string) (token, id string) {
	a.t.Helper()
	raw, secret := make([]byte, 8), make([]byte, 32)
	_, _ = rand.Read(raw)
	_, _ = rand.Read(secret)
	id = hex.EncodeToString(raw)
	token = "sbpat_" + id + "." + base64.RawURLEncoding.EncodeToString(secret)
	hash := sha256.Sum256([]byte(token))
	a.sql(`INSERT INTO personal_access_tokens (id, principal, name, scope, token_hash, expires_at)
		VALUES ($1, 'user:https://idp.example#alice', 'fleetd', $2, $3, now() + interval '90 days')`,
		id, scope, hash[:])
	return token, id
}

func TestAFleetTokenUsesEveryRotatorRoute(t *testing.T) {
	a := newAPI(t, "fleet")
	a.token = a.credential("fleet", "crud.fleet")
	token, id := accessToken(a, "fleet")
	put := func(path, body string) answer { return a.do("PUT", rotatorBase+path, body, bearer(token)) }

	if got := put("/machines/"+machineID, machineBody()); got.status != 200 {
		t.Fatalf("PUT machine: %d %s", got.status, got.raw)
	}
	expect(t, a.count(`SELECT count(*) FROM fleet_machines WHERE token_id = '`+id+`' AND proof_name IS NULL`), 1)
	// A second PUT from the same token refreshes the row.
	if got := put("/machines/"+machineID, machineBody()); got.status != 200 {
		t.Fatalf("PUT machine again: %d %s", got.status, got.raw)
	}
	session := uuid()
	if got := put(onMachine+session, sessionBody()); got.status != 200 {
		t.Fatalf("PUT session: %d %s", got.status, got.raw)
	}
	ann := a.agent()
	a.fleet("PUT", "/channels/ops/members/"+ann, "", 200)
	// Virtual senders are the service credential's only: a machine's token posts as none.
	post := a.do("POST", virtualBase, object("sender", "rotator", "channel", "ops", "text", "logins moved"), bearer(token))
	if post.status == 200 {
		t.Fatalf("a fleet token posted a virtual message: %s", post.raw)
	}
	expect(t, a.count(`SELECT count(*) FROM fleet_messages`), 0)
}

func TestAFleetTokenIsRefusedElsewhere(t *testing.T) {
	for _, mode := range []string{"", "observe", "enforce"} {
		a := newAPIInMode(t, "fleet,unknown-trays", mode)
		token, _ := accessToken(a, "fleet")
		for name, c := range map[string]struct{ method, path string }{
			"the fleet's sessions": {"GET", fleetBase + "/sessions"},
			"the fleet's inbox":    {"GET", fleetBase + "/agents/" + uuid() + "/inbox"},
			"an api read":          {"GET", "/api/v1/filament/unknown-trays"},
			"an api write":         {"POST", "/api/v1/filament/unknown-trays/" + uuid() + "/dismiss"},
		} {
			got := a.do(c.method, c.path, "", bearer(token))
			if got.status != 403 || got.body["reason"] != "out_of_scope" {
				t.Errorf("mode %q, %s: %d %s", mode, name, got.status, got.raw)
			}
		}
	}
}

func TestAReadOrWriteTokenReachesNoRotatorRoute(t *testing.T) {
	a := newAPI(t, "fleet")
	a.token = a.credential("fleet", "crud.fleet")
	for _, scope := range []string{"read", "write"} {
		token, _ := accessToken(a, scope)
		for name, c := range map[string]struct{ method, path, body string }{
			"a machine": {"PUT", rotatorBase + "/machines/" + machineID, machineBody()},
			"a session": {"PUT", rotatorBase + onMachine + uuid(), sessionBody()},
		} {
			got := a.do(c.method, c.path, c.body, bearer(token))
			if got.status != 403 || got.body["reason"] != "out_of_scope" {
				t.Errorf("%s token, %s: %d %s", scope, name, got.status, got.raw)
			}
		}
	}
	expect(t, a.count(`SELECT count(*) FROM fleet_machines`), 0)
	expect(t, a.count(`SELECT count(*) FROM fleet_messages`), 0)
}

func TestARevokedOrUnknownFleetTokenIsUnauthorized(t *testing.T) {
	a := newAPI(t, "fleet")
	token, id := accessToken(a, "fleet")
	put := func(tok string) answer {
		return a.do("PUT", rotatorBase+"/machines/"+machineID, machineBody(), bearer(tok))
	}
	expect(t, put(token).status, 200)
	a.sql(`UPDATE personal_access_tokens SET revoked_at = now() WHERE id = $1`, id)
	got := put(token)
	expect(t, got.status, 401)
	expect(t, got.header.Get("WWW-Authenticate"), `Bearer realm="scadbuddy"`)
	expired, expiredID := accessToken(a, "fleet")
	a.sql(`UPDATE personal_access_tokens SET expires_at = now() - interval '1 second' WHERE id = $1`, expiredID)
	expect(t, put(expired).status, 401)
	expect(t, put("sbpat_"+strings.Repeat("0", 16)+"."+strings.Repeat("A", 43)).status, 401)
}

// One token is one machine: the token that registered a machine first owns it, and another
// token, or a machine's bootstrap proof, cannot write it. Nor can that token write another's.
func TestAMachineBelongsToItsToken(t *testing.T) {
	g := newRegistry(t)
	mine, _ := accessToken(g.api, "fleet")
	theirs, _ := accessToken(g.api, "fleet")
	laptopID := strings.Repeat("1", 64)
	put := func(path, body, token string) answer {
		return g.do("PUT", rotatorBase+path, body, bearer(token))
	}
	expect(t, put("/machines/"+machineID, machineBody(), mine).status, 200)
	expect(t, put("/machines/"+laptopID, machineBody(), theirs).status, 200)

	stolen := object("hostname", "stolen", "os", "linux", "os_user", "u", "service_version", "1")
	for name, token := range map[string]string{"another token": theirs, "a bootstrap proof": g.rotatorToken} {
		got := put("/machines/"+machineID, stolen, token)
		if got.status != 403 || got.body["reason"] != "machine-of-another-proof" {
			t.Errorf("%s takes the machine over: %d %s", name, got.status, got.raw)
		}
		got = put(onMachine+uuid(), sessionBody(), token)
		if got.status != 403 || got.body["reason"] != "machine-of-another-proof" {
			t.Errorf("%s writes the machine's session: %d %s", name, got.status, got.raw)
		}
	}
	expect(t, g.count(`SELECT count(*) FROM fleet_machines WHERE hostname = 'stolen'`), 0)
	expect(t, g.count(`SELECT count(*) FROM fleet_session_hosts`), 0)

	// A machine a proof registered is not a token's either.
	g.write("/machines/"+strings.Repeat("2", 64), machineBody(), 200)
	got := put("/machines/"+strings.Repeat("2", 64), stolen, mine)
	expect(t, got.status, 403)
	expect(t, got.body["reason"], "machine-of-another-proof")
}
