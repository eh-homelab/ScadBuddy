package main

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"net/http"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"testing"
)

// fleetd's personal access token (#8041, #8179): scope `fleet` reaches core's fleet-rotator
// routes and nothing else; a `read` or `write` token reaches none of them; one token is one
// machine.

// accessToken is a live personal access token of scope, as Settings → Access tokens mints
// one (auth/personal_tokens.py mint), and its id.
func accessToken(a *api, scope string) (token, id string) {
	a.t.Helper()
	return accessTokenOf(a, scope, alice)
}

const alice = "user:https://idp.example#alice"

// accessTokenOf is accessToken minted by principal.
func accessTokenOf(a *api, scope, principal string) (token, id string) {
	a.t.Helper()
	raw, secret := make([]byte, 8), make([]byte, 32)
	_, _ = rand.Read(raw)
	_, _ = rand.Read(secret)
	id = hex.EncodeToString(raw)
	token = "sbpat_" + id + "." + base64.RawURLEncoding.EncodeToString(secret)
	hash := sha256.Sum256([]byte(token))
	a.sql(`INSERT INTO personal_access_tokens (id, principal, name, scope, token_hash, expires_at)
		VALUES ($1, $4, 'fleetd', $2, $3, now() + interval '90 days')`,
		id, scope, hash[:], principal)
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
	// The other token is the same person's and lives: machine-token-live. A proof never
	// takes a token's machine: machine-of-another-proof.
	for name, c := range map[string]struct{ token, reason string }{
		"another token":     {theirs, "machine-token-live"},
		"a bootstrap proof": {g.rotatorToken, "machine-of-another-proof"},
	} {
		token := c.token
		got := put("/machines/"+machineID, stolen, token)
		if got.status != 403 || got.body["reason"] != c.reason {
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
	expect(t, got.body["reason"], "machine-of-a-proof")
}

// A machine moves to a new token only when its owner is dead: a token's machine to a new
// token of the same person once the old token is revoked or expired.
func TestAMachineMovesToItsPersonsNewToken(t *testing.T) {
	for name, kill := range map[string]string{
		"expired": `UPDATE personal_access_tokens SET expires_at = now() - interval '1 second' WHERE id = $1`,
		"revoked": `UPDATE personal_access_tokens SET revoked_at = now() WHERE id = $1`,
	} {
		t.Run(name, func(t *testing.T) {
			a := newAPI(t, "fleet")
			old, oldID := accessToken(a, "fleet")
			fresh, freshID := accessToken(a, "fleet")
			put := func(token string) answer {
				return a.do("PUT", rotatorBase+"/machines/"+machineID, machineBody(), bearer(token))
			}
			expect(t, put(old).status, 200)
			// While the old token lives, the new one is refused.
			got := put(fresh)
			expect(t, got.status, 403)
			expect(t, got.body["reason"], "machine-token-live")
			a.sql(kill, oldID)
			if got := put(fresh); got.status != 200 {
				t.Fatalf("the new token: %d %s", got.status, got.raw)
			}
			expect(t, a.count(`SELECT count(*) FROM fleet_machines WHERE token_id = $1 AND proof_name IS NULL`, freshID), 1)
			if got := a.do("PUT", rotatorBase+onMachine+uuid(), sessionBody(), bearer(fresh)); got.status != 200 {
				t.Fatalf("the new token's session: %d %s", got.status, got.raw)
			}
		})
	}
}

func TestAnotherPersonsTokenNeverTakesAMachine(t *testing.T) {
	a := newAPI(t, "fleet")
	old, oldID := accessToken(a, "fleet")
	bob, _ := accessTokenOf(a, "fleet", "user:https://idp.example#bob")
	put := func(token string) answer {
		return a.do("PUT", rotatorBase+"/machines/"+machineID, machineBody(), bearer(token))
	}
	expect(t, put(old).status, 200)
	a.sql(`UPDATE personal_access_tokens SET revoked_at = now() WHERE id = $1`, oldID)
	got := put(bob)
	expect(t, got.status, 403)
	expect(t, got.body["reason"], "machine-of-another-person")
	expect(t, a.count(`SELECT count(*) FROM fleet_machines WHERE token_id = $1`, oldID), 1)
}

// A machine whose token row is gone has no known person: no token takes it.
func TestAMachineOfAForgottenTokenIsRefused(t *testing.T) {
	a := newAPI(t, "fleet")
	old, oldID := accessToken(a, "fleet")
	fresh, _ := accessToken(a, "fleet")
	put := func(token string) answer {
		return a.do("PUT", rotatorBase+"/machines/"+machineID, machineBody(), bearer(token))
	}
	expect(t, put(old).status, 200)
	a.sql(`DELETE FROM personal_access_tokens WHERE id = $1`, oldID)
	got := put(fresh)
	expect(t, got.status, 403)
	expect(t, got.body["reason"], "machine-of-unknown-owner")
	expect(t, a.count(`SELECT count(*) FROM fleet_machines WHERE token_id = $1`, oldID), 1)
}

// A proof has no person, so no token takes its machine, even once the proof file is gone.
func TestATokenNeverTakesAProofsMachine(t *testing.T) {
	g := newRegistry(t)
	token, id := accessToken(g.api, "fleet")
	put := func(tok string) answer {
		return g.do("PUT", rotatorBase+"/machines/"+machineID, machineBody(), bearer(tok))
	}
	g.write("/machines/"+machineID, machineBody(), 200)
	got := put(token)
	expect(t, got.status, 403)
	expect(t, got.body["reason"], "machine-of-a-proof")
	if err := os.Remove(filepath.Join(g.bootstrap, "fleet-rotator.devbox")); err != nil {
		t.Fatal(err)
	}
	got = put(token)
	expect(t, got.status, 403)
	expect(t, got.body["reason"], "machine-of-a-proof")
	expect(t, g.count(`SELECT count(*) FROM fleet_machines WHERE token_id = $1`, id), 0)
}

func TestAProofNeverTakesATokensMachine(t *testing.T) {
	g := newRegistry(t)
	token, id := accessToken(g.api, "fleet")
	expect(t, g.do("PUT", rotatorBase+"/machines/"+machineID, machineBody(), bearer(token)).status, 200)
	// Even with the token dead, the proof is refused.
	g.sql(`UPDATE personal_access_tokens SET revoked_at = now() WHERE id = $1`, id)
	got := g.do("PUT", rotatorBase+"/machines/"+machineID, machineBody(), bearer(g.rotatorToken))
	expect(t, got.status, 403)
	expect(t, got.body["reason"], "machine-of-another-proof")
	expect(t, g.count(`SELECT count(*) FROM fleet_machines WHERE token_id = $1 AND proof_name IS NULL`, id), 1)
}

// Two new tokens of the same person race for a dead token's machine: the row lock lets one
// take it, and the other then finds a live token (#8370).
func TestTwoNewTokensRaceForADeadTokensMachine(t *testing.T) {
	a := newAPI(t, "fleet")
	old, oldID := accessToken(a, "fleet")
	put := func(token string) answer {
		return a.do("PUT", rotatorBase+"/machines/"+machineID, machineBody(), bearer(token))
	}
	expect(t, put(old).status, 200)
	a.sql(`UPDATE personal_access_tokens SET revoked_at = now() WHERE id = $1`, oldID)
	first, firstID := accessToken(a, "fleet")
	second, secondID := accessToken(a, "fleet")
	var (
		wg    sync.WaitGroup
		start = make(chan struct{})
		got   [2]answer
	)
	for i, token := range []string{first, second} {
		wg.Add(1)
		go func() {
			defer wg.Done()
			<-start
			got[i] = put(token)
		}()
	}
	close(start)
	wg.Wait()
	statuses := []int{got[0].status, got[1].status}
	slices.Sort(statuses)
	if statuses[0] != 200 || statuses[1] != 403 {
		t.Fatalf("the race: %d %s, %d %s; want one 200 and one 403", got[0].status, got[0].raw, got[1].status, got[1].raw)
	}
	loser := got[0]
	if loser.status == 200 {
		loser = got[1]
	}
	expect(t, loser.body["reason"], "machine-token-live")
	expect(t, a.count(`SELECT count(*) FROM fleet_machines WHERE token_id IN ($1, $2)`, firstID, secondID), 1)
}

// A refused fleet token's WebSocket handshake is a 403 with no body, before any upgrade: what
// Python's auth_check answers when it closes the socket with 1008 before accept (Starlette
// sends a 403 for a close before accept). Pinned in every mode.
func TestAFleetTokensWebSocketHandshakeIsRefused(t *testing.T) {
	for _, mode := range []string{"", "observe", "enforce"} {
		a := newAPIInMode(t, "fleet,unknown-trays", mode)
		token, _ := accessToken(a, "fleet")
		got := a.do("GET", "/api/v1/filament/unknown-trays", "", bearer(token), func(r *http.Request) {
			r.Header.Set("Connection", "Upgrade")
			r.Header.Set("Upgrade", "websocket")
			r.Header.Set("Sec-WebSocket-Version", "13")
			r.Header.Set("Sec-WebSocket-Key", "dGhlIHNhbXBsZSBub25jZQ==")
		})
		if got.status != 403 || got.raw != "" {
			t.Errorf("mode %q: %d %q; want a 403 with no body", mode, got.status, got.raw)
		}
	}
}
