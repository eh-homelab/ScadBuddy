package main

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// The fleet's session registry (internal/fleet/registry.go; spec
// 2026-11-26-fleet-session-registry-and-rotator-design §6.3): the fleet-rotator writes, the
// fleet reads.

const rotatorBase = "/api/v1/internal/crud/fleet-rotator"

// machineID is a machine's id as the rotator sends it: a SHA-256, in hex.
const machineID = "5f1c0e7d2b9a8c4e3f6d1a0b9c8e7f6a5d4c3b2a1f0e9d8c7b6a5f4e3d2c1b0a"

// onMachine starts the path of a session's row on machineID; the session id follows.
const onMachine = "/machines/" + machineID + "/sessions/"

// hostPath is the path of a session's row on a machine.
func hostPath(machine, session string) string {
	return "/machines/" + machine + "/sessions/" + session
}

// hosts is a read session's hosts, one per machine.
func hosts(t *testing.T, session map[string]any) []map[string]any {
	t.Helper()
	list, ok := session["hosts"].([]any)
	if !ok {
		t.Fatalf("no hosts in %v", session)
	}
	out := make([]map[string]any, len(list))
	for i, h := range list {
		out[i] = h.(map[string]any)
	}
	return out
}

// onlyHost is the one host of a read session.
func onlyHost(t *testing.T, session map[string]any) map[string]any {
	t.Helper()
	h := hosts(t, session)
	if len(h) != 1 {
		t.Fatalf("%d hosts, want 1: %v", len(h), session)
	}
	return h[0]
}

type registry struct {
	*api
	fleetToken, rotatorToken string
}

func newRegistry(t *testing.T) *registry {
	t.Helper()
	a := newAPI(t, "fleet")
	return &registry{
		api:          a,
		fleetToken:   a.credential("fleet", "crud.fleet"),
		rotatorToken: rotatorCredential(a, "devbox"),
	}
}

// rotatorCredential is a fleet-rotator credential as the exchange issues it for the proof
// file fleet-rotator.<machine>: the row names the machine, whose file is in the bootstrap
// directory.
func rotatorCredential(a *api, machine string) string {
	a.t.Helper()
	if err := os.WriteFile(filepath.Join(a.bootstrap, "fleet-rotator."+machine), []byte("a-bootstrap-secret-of-at-least-32-characters"), 0o600); err != nil {
		a.t.Fatal(err)
	}
	id, secret := make([]byte, 8), make([]byte, 32)
	_, _ = rand.Read(id)
	_, _ = rand.Read(secret)
	token := "sbsvc_" + hex.EncodeToString(id) + "." + base64.RawURLEncoding.EncodeToString(secret)
	hash := sha256.Sum256([]byte(token))
	a.sql(`INSERT INTO service_credentials (id, caller, scopes, secret_hash, expires_at, proof, machine)
		VALUES ($1, 'fleet-rotator', '{crud.fleet-rotator}', $2, now() + interval '15 minutes',
			'bootstrap', $3)`,
		hex.EncodeToString(id), hash[:], machine)
	return token
}

func bearer(token string) func(*http.Request) {
	return func(r *http.Request) { r.Header.Set("Authorization", "Bearer "+token) }
}

func (g *registry) write(path, body string, status int) answer {
	g.t.Helper()
	out := g.do("PUT", rotatorBase+path, body, bearer(g.rotatorToken))
	if out.status != status {
		g.t.Fatalf("PUT %s: %d %s, want %d", path, out.status, out.raw, status)
	}
	return out
}

func (g *registry) read(path string, status int) answer {
	g.t.Helper()
	out := g.do("GET", fleetBase+path, "", bearer(g.fleetToken))
	if out.status != status {
		g.t.Fatalf("GET %s: %d %s, want %d", path, out.status, out.raw, status)
	}
	return out
}

func machineBody() string {
	return object("hostname", "devbox", "os", "linux", "os_user", "elan", "service_version", "0.1.0")
}

// sessionBody is a full session row, with pairs changing or adding fields (nil drops one).
func sessionBody(pairs ...any) string {
	m := map[string]any{
		"pid":                4242,
		"proc_start":         "97360477",
		"cwd":                "/home/elan/repos/ScadBuddy",
		"config_dir":         "/home/elan/.claude",
		"transcript_path":    "/home/elan/.claude/projects/x/s.jsonl",
		"git_remote":         "https://github.com/eh-homelab/ScadBuddy.git",
		"git_branch":         "feat/registry",
		"worktree_root":      "/home/elan/repos/ScadBuddy",
		"git_head":           strings.Repeat("ab", 20),
		"cc_version":         "2.1.332",
		"kind":               "interactive",
		"entrypoint":         "cli",
		"name":               "registry work",
		"status":             "busy",
		"credential_id":      "cred-1",
		"account_uuid":       "11111111-2222-4333-8444-555555555555",
		"last_rate_limit_at": "2026-11-26T10:00:00Z",
		"last_reset_at":      "2026-11-26T15:00:00Z",
	}
	for i := 0; i < len(pairs); i += 2 {
		if pairs[i+1] == nil {
			delete(m, pairs[i].(string))
		} else {
			m[pairs[i].(string)] = pairs[i+1]
		}
	}
	pairsOut := make([]any, 0, 2*len(m))
	for k, v := range m {
		pairsOut = append(pairsOut, k, v)
	}
	return object(pairsOut...)
}

func TestTheRegistryStoresMachinesAndSessions(t *testing.T) {
	g := newRegistry(t)
	g.sql(`INSERT INTO ai_credentials (id, priority, kind, name, secret_sealed, dek_sealed, kek_id, last4)
		VALUES ('cred-1', 0, 'claude_oauth_token', 'Main', '\x01', '\x02', 'kek', 'wxyz')`)
	first := g.write("/machines/"+machineID, machineBody(), 200).body
	expect(t, first["id"], machineID)
	expect(t, first["hostname"], "devbox")
	time.Sleep(5 * time.Millisecond)
	again := g.write("/machines/"+machineID, object("hostname", "devbox2", "os", "linux",
		"os_user", "elan", "service_version", "0.2.0"), 200).body
	expect(t, again["hostname"], "devbox2")
	expect(t, again["first_seen"], first["first_seen"])
	expect(t, again["created_at"], first["created_at"])
	if again["updated_at"] == first["updated_at"] {
		t.Fatalf("updated_at did not move: %v", again["updated_at"])
	}

	session := uuid()
	// The PUT answers the caller's own row: the session's id and handle, and the host.
	put := g.write(onMachine+session, sessionBody(), 200).body
	expect(t, put["session_id"], session)
	expect(t, put["handle"], session[:8])
	expect(t, put["machine"].(map[string]any)["id"], machineID)
	if _, ok := put["hosts"]; ok {
		t.Fatalf("the PUT answers the hosts of other machines: %v", put)
	}
	// The session's fleet agent is made as touch makes it.
	expect(t, g.count(`SELECT count(*) FROM fleet_agents WHERE session_id = '`+session+`'`), 1)

	read := g.read("/sessions/"+session, 200).body
	expect(t, read["session_id"], session)
	expect(t, read["handle"], session[:8])
	got := onlyHost(t, read)
	expect(t, read["last_seen"], got["last_seen"])
	expect(t, got["pid"], float64(4242))
	expect(t, got["proc_start"], "97360477")
	expect(t, got["cwd"], "/home/elan/repos/ScadBuddy")
	expect(t, got["git_head"], strings.Repeat("ab", 20))
	expect(t, got["account_uuid"], "11111111-2222-4333-8444-555555555555")
	expect(t, got["last_rate_limit_at"], "2026-11-26T10:00:00Z")
	expect(t, got["previous_session_id"], nil)
	machine := got["machine"].(map[string]any)
	expect(t, machine["id"], machineID)
	expect(t, machine["hostname"], "devbox2")
	expect(t, got["credential_id"], "cred-1")
	expect(t, got["credential"], map[string]any{
		"id": "cred-1", "name": "Main", "kind": "claude_oauth_token", "last4": "wxyz",
	})
	for _, secret := range []string{"secret", "sealed", "dek", "kek"} {
		if strings.Contains(g.read("/sessions/"+session, 200).raw, secret) {
			t.Fatalf("the answer names %q", secret)
		}
	}

	// A PUT replaces the row: an absent optional field is cleared.
	g.write(onMachine+session, sessionBody("credential_id", nil, "status", "idle"), 200)
	got = onlyHost(t, g.read("/sessions/"+session, 200).body)
	expect(t, got["status"], "idle")
	expect(t, got["credential_id"], nil)
	expect(t, got["credential"], nil)

	// A credential that is gone reads as none, with its id kept.
	g.write(onMachine+session, sessionBody("credential_id", "deleted"), 200)
	got = onlyHost(t, g.read("/sessions/"+session, 200).body)
	expect(t, got["credential_id"], "deleted")
	expect(t, got["credential"], nil)

	list := g.read("/sessions", 200).body
	expect(t, len(list["sessions"].([]any)), 1)
	expect(t, list["more"], false)
	listed := list["sessions"].([]any)[0].(map[string]any)
	expect(t, listed["session_id"], session)
	expect(t, onlyHost(t, listed)["machine"].(map[string]any)["id"], machineID)

	g.read("/sessions/"+uuid(), 404)
}

func TestTheSessionsListPages(t *testing.T) {
	g := newRegistry(t)
	g.write("/machines/"+machineID, machineBody(), 200)
	for range 3 {
		g.write(onMachine+uuid(), sessionBody(), 200)
	}
	page := g.read("/sessions?limit=2", 200).body
	expect(t, len(page["sessions"].([]any)), 2)
	expect(t, page["more"], true)
	g.read("/sessions?limit=-1", 422)
}

func TestAClearedSessionLinksToTheOneBefore(t *testing.T) {
	g := newRegistry(t)
	g.write("/machines/"+machineID, machineBody(), 200)
	before, after, elsewhere := uuid(), uuid(), uuid()
	g.write(onMachine+before, sessionBody(), 200)
	time.Sleep(5 * time.Millisecond)
	g.write(onMachine+after, sessionBody(), 200)
	g.write(onMachine+elsewhere, sessionBody("proc_start", "1"), 200)
	previous := func(session string) any {
		return onlyHost(t, g.read("/sessions/"+session, 200).body)["previous_session_id"]
	}
	expect(t, previous(after), before)
	expect(t, previous(elsewhere), nil)
	// A later PUT of the first session does not link it to the second.
	g.write(onMachine+before, sessionBody(), 200)
	expect(t, previous(before), nil)
	expect(t, previous(after), before)

	// The link stays on one machine: another machine's process with the same pid and
	// proc_start is no session before this one.
	laptop := rotatorCredential(g.api, "laptop")
	laptopID := strings.Repeat("1", 64)
	if got := g.do("PUT", rotatorBase+"/machines/"+laptopID, machineBody(), bearer(laptop)); got.status != 200 {
		t.Fatalf("the laptop registers: %d %s", got.status, got.raw)
	}
	time.Sleep(5 * time.Millisecond)
	onLaptop := uuid()
	if got := g.do("PUT", rotatorBase+hostPath(laptopID, onLaptop), sessionBody(), bearer(laptop)); got.status != 200 {
		t.Fatalf("the laptop writes a session: %d %s", got.status, got.raw)
	}
	expect(t, previous(onLaptop), nil)
}

func TestTheRegistryRefusesBadFields(t *testing.T) {
	g := newRegistry(t)
	g.write("/machines/"+machineID, machineBody(), 200)
	session := uuid()

	for name, path := range map[string]string{
		"an upper-case session id": onMachine + strings.ToUpper(session),
		"no uuid":                  onMachine + "not-a-uuid",
		"a raw machine id":         hostPath("0123456789abcdef0123456789abcdef", session),
		"an upper-case machine id": hostPath(strings.ToUpper(machineID), session),
	} {
		if got := g.do("PUT", rotatorBase+path, sessionBody(), bearer(g.rotatorToken)); got.status != 422 {
			t.Errorf("%s: %d %s", name, got.status, got.raw)
		}
	}
	for name, path := range map[string]string{
		"a raw machine id": "/machines/0123456789abcdef0123456789abcdef",
		"upper-case hex":   "/machines/" + strings.ToUpper(machineID),
		"too long a hash":  "/machines/" + machineID + "00",
	} {
		if got := g.do("PUT", rotatorBase+path, machineBody(), bearer(g.rotatorToken)); got.status != 422 && got.status != 404 {
			t.Errorf("%s: %d %s", name, got.status, got.raw)
		}
	}
	for name, body := range map[string]string{
		"a control character in a hostname": object("hostname", "dev\nbox", "os", "linux", "os_user", "u", "service_version", "1"),
		"no hostname":                       object("os", "linux", "os_user", "u", "service_version", "1"),
		"too long a hostname":               object("hostname", strings.Repeat("h", 254), "os", "linux", "os_user", "u", "service_version", "1"),
		"an unknown field":                  object("hostname", "h", "os", "linux", "os_user", "u", "service_version", "1", "raw_machine_id", "x"),
	} {
		if got := g.do("PUT", rotatorBase+"/machines/"+machineID, body, bearer(g.rotatorToken)); got.status != 422 {
			t.Errorf("%s: %d %s", name, got.status, got.raw)
		}
	}
	for name, body := range map[string]string{
		"a relative cwd":                    sessionBody("cwd", "repos/x"),
		"a relative config dir":             sessionBody("config_dir", "~/.claude"),
		"a relative transcript path":        sessionBody("transcript_path", "x.jsonl"),
		"a relative worktree root":          sessionBody("worktree_root", "./x"),
		"a control character in a path":     sessionBody("cwd", "/home/\x00x"),
		"too long a path":                   sessionBody("cwd", "/"+strings.Repeat("p", 4096)),
		"a control character in the name":   sessionBody("name", "a\u001bb"),
		"too long a branch":                 sessionBody("git_branch", strings.Repeat("b", 256)),
		"a short git head":                  sessionBody("git_head", "abc123"),
		"an upper-case git head":            sessionBody("git_head", strings.Repeat("AB", 20)),
		"pid 0":                             sessionBody("pid", 0),
		"a pid past pid_max":                sessionBody("pid", 4194305),
		"a proc start that is no number":    sessionBody("proc_start", "12a"),
		"a machine id in the body":          sessionBody("machine_id", machineID),
		"an upper-case account uuid":        sessionBody("account_uuid", "11111111-2222-4333-8444-55555555555A"),
		"a bad time":                        sessionBody("last_reset_at", "tomorrow"),
		"no cwd":                            sessionBody("cwd", nil),
		"an unknown field":                  sessionBody("secret", "x"),
		"a version with a space":            sessionBody("cc_version", "2.1 beta"),
		"a control character in the status": sessionBody("status", "busy\t"),
		"too long a credential id":          sessionBody("credential_id", strings.Repeat("c", 65)),
	} {
		if got := g.do("PUT", rotatorBase+onMachine+session, body, bearer(g.rotatorToken)); got.status != 422 {
			t.Errorf("%s: %d %s", name, got.status, got.raw)
		}
	}
	expect(t, g.count(`SELECT count(*) FROM fleet_session_hosts`), 0)
	expect(t, g.count(`SELECT count(*) FROM fleet_agents`), 0)

	// A machine that never registered is a 404, and makes no agent.
	other := strings.Repeat("0", 64)
	got := g.do("PUT", rotatorBase+hostPath(other, session), sessionBody(), bearer(g.rotatorToken))
	expect(t, got.status, 404)
	expect(t, got.body["reason"], "unknown-machine")
	expect(t, g.count(`SELECT count(*) FROM fleet_agents`), 0)
}

func TestASessionWhoseHandleIsTakenIsAConflict(t *testing.T) {
	g := newRegistry(t)
	g.write("/machines/"+machineID, machineBody(), 200)
	first := uuid()
	g.write(onMachine+first, sessionBody(), 200)
	twin := first[:8] + uuid()[8:]
	got := g.do("PUT", rotatorBase+onMachine+twin, sessionBody(), bearer(g.rotatorToken))
	expect(t, got.status, 409)
	expect(t, got.body["reason"], "handle-taken")
}

func TestEachCallerReachesOnlyItsRegistryRoutes(t *testing.T) {
	g := newRegistry(t)
	g.write("/machines/"+machineID, machineBody(), 200)
	session := uuid()
	g.write(onMachine+session, sessionBody(), 200)
	for name, c := range map[string]struct{ method, path, body, token string }{
		"fleet writes a machine":      {"PUT", rotatorBase + "/machines/" + machineID, machineBody(), g.fleetToken},
		"fleet writes a session":      {"PUT", rotatorBase + onMachine + session, sessionBody(), g.fleetToken},
		"the rotator lists sessions":  {"GET", fleetBase + "/sessions", "", g.rotatorToken},
		"the rotator reads a session": {"GET", fleetBase + "/sessions/" + session, "", g.rotatorToken},
		"the rotator reads the inbox": {"GET", fleetBase + "/agents/" + session + "/inbox", "", g.rotatorToken},
		"the agent writes a machine":  {"PUT", rotatorBase + "/machines/" + machineID, machineBody(), g.credential("agent", "crud.agent", "crud.fleet-rotator")},
		"a rotator without its scope": {"PUT", rotatorBase + "/machines/" + machineID, machineBody(), g.credential("fleet-rotator", "crud.fleet")},
	} {
		if got := g.do(c.method, c.path, c.body, bearer(c.token)); got.status != 403 {
			t.Errorf("%s: %d %s", name, got.status, got.raw)
		}
	}
	none := g.do("PUT", rotatorBase+"/machines/"+machineID, machineBody(), func(r *http.Request) { r.Header.Del("Authorization") })
	expect(t, none.status, 401)
}

// A machine belongs to the proof that registered it first. Only that proof's credential
// refreshes it or writes its sessions.
func TestAMachineBelongsToItsProof(t *testing.T) {
	g := newRegistry(t)
	laptop := rotatorCredential(g.api, "laptop")
	laptopID := strings.Repeat("1", 64)
	put := func(path, body, token string) answer {
		return g.do("PUT", rotatorBase+path, body, bearer(token))
	}

	g.write("/machines/"+machineID, machineBody(), 200)
	expect(t, g.count(`SELECT count(*) FROM fleet_machines WHERE proof_name = 'devbox'`), 1)
	// Another machine's credential cannot take the row over.
	got := put("/machines/"+machineID, object("hostname", "stolen", "os", "linux",
		"os_user", "u", "service_version", "1"), laptop)
	expect(t, got.status, 403)
	expect(t, got.body["reason"], "machine-of-another-proof")
	expect(t, g.count(`SELECT count(*) FROM fleet_machines WHERE hostname = 'stolen'`), 0)
	if got := put("/machines/"+laptopID, machineBody(), laptop); got.status != 200 {
		t.Fatalf("the laptop registers its own machine: %d %s", got.status, got.raw)
	}

	// A session on a machine of another proof is refused, and makes no agent.
	session := uuid()
	got = put(onMachine+session, sessionBody(), laptop)
	expect(t, got.status, 403)
	expect(t, got.body["reason"], "machine-of-another-proof")
	expect(t, g.count(`SELECT count(*) FROM fleet_agents`), 0)

	// A fleet-rotator credential that names no machine writes nothing.
	unbound := g.credential("fleet-rotator", "crud.fleet-rotator")
	got = put("/machines/"+strings.Repeat("3", 64), machineBody(), unbound)
	expect(t, got.status, 403)
	expect(t, got.body["reason"], "no-machine")
	got = put(onMachine+uuid(), sessionBody(), unbound)
	expect(t, got.status, 403)
	expect(t, got.body["reason"], "no-machine")
}

// A machine whose proof file is gone from SCADBUDDY_SERVICE_BOOTSTRAP_DIR is refused at
// once, as Python's resolve refuses it (#8172).
func TestARevokedMachineIsRefused(t *testing.T) {
	g := newRegistry(t)
	g.write("/machines/"+machineID, machineBody(), 200)
	if err := os.Remove(filepath.Join(g.bootstrap, "fleet-rotator.devbox")); err != nil {
		t.Fatal(err)
	}
	g.write("/machines/"+machineID, machineBody(), 401)
}

// A machine provisioned again under a new proof name takes its row once the old proof is
// dead: no file and no live credential (#8003). Until then the new proof is refused.
func TestANewProofTakesADeadProofsMachine(t *testing.T) {
	g := newRegistry(t)
	g.write("/machines/"+machineID, machineBody(), 200)
	renamed := rotatorCredential(g.api, "devbox-2")
	put := func() answer {
		return g.do("PUT", rotatorBase+"/machines/"+machineID, machineBody(), bearer(renamed))
	}
	// The old file is there.
	got := put()
	expect(t, got.status, 403)
	expect(t, got.body["reason"], "machine-of-another-proof")
	// The file is gone, but the old proof's credential still lives in the database.
	if err := os.Remove(filepath.Join(g.bootstrap, "fleet-rotator.devbox")); err != nil {
		t.Fatal(err)
	}
	got = put()
	expect(t, got.status, 403)
	expect(t, got.body["reason"], "machine-of-another-proof")
	// The old credential is revoked (or expires): the new proof takes the row.
	g.sql(`UPDATE service_credentials SET revoked_at = now() WHERE machine = 'devbox'`)
	if got := put(); got.status != 200 {
		t.Fatalf("the new proof: %d %s", got.status, got.raw)
	}
	expect(t, g.count(`SELECT count(*) FROM fleet_machines WHERE proof_name = 'devbox-2' AND token_id IS NULL`), 1)
	if got := g.do("PUT", rotatorBase+onMachine+uuid(), sessionBody(), bearer(renamed)); got.status != 200 {
		t.Fatalf("the new proof's session: %d %s", got.status, got.raw)
	}
	// A token still never takes a proof's machine, nor a proof a token's.
	token, _ := accessToken(g.api, "fleet")
	expect(t, g.do("PUT", rotatorBase+"/machines/"+machineID, machineBody(), bearer(token)).body["reason"], "machine-of-a-proof")
}

// One Claude Code session id can run on two machines at once. Each machine writes its own
// row of the session, and no machine changes another machine's row.
func TestASessionRunsOnSeveralMachines(t *testing.T) {
	g := newRegistry(t)
	laptop := rotatorCredential(g.api, "laptop")
	laptopID := strings.Repeat("1", 64)
	put := func(path, body, token string) answer {
		return g.do("PUT", rotatorBase+path, body, bearer(token))
	}
	g.write("/machines/"+machineID, machineBody(), 200)
	if got := put("/machines/"+laptopID, object("hostname", "laptop", "os", "linux",
		"os_user", "elan", "service_version", "0.1.0"), laptop); got.status != 200 {
		t.Fatalf("the laptop registers its own machine: %d %s", got.status, got.raw)
	}

	session := uuid()
	g.write(hostPath(machineID, session), sessionBody("cwd", "/home/elan/on-devbox"), 200)
	time.Sleep(5 * time.Millisecond)
	got := put(hostPath(laptopID, session), sessionBody("cwd", "/home/elan/on-laptop", "pid", 7), laptop)
	if got.status != 200 {
		t.Fatalf("the laptop writes its row of the session: %d %s", got.status, got.raw)
	}
	expect(t, got.body["machine"].(map[string]any)["id"], laptopID)
	expect(t, got.body["cwd"], "/home/elan/on-laptop")
	expect(t, g.count(`SELECT count(*) FROM fleet_agents`), 1)
	expect(t, g.count(`SELECT count(*) FROM fleet_session_hosts WHERE session_id = '`+session+`'`), 2)

	// The read gives the session once, with one host per machine, the most recently seen
	// first.
	read := g.read("/sessions/"+session, 200).body
	expect(t, read["session_id"], session)
	both := hosts(t, read)
	expect(t, len(both), 2)
	expect(t, both[0]["machine"].(map[string]any)["id"], laptopID)
	expect(t, both[0]["cwd"], "/home/elan/on-laptop")
	expect(t, both[0]["pid"], float64(7))
	expect(t, both[1]["machine"].(map[string]any)["id"], machineID)
	expect(t, both[1]["cwd"], "/home/elan/on-devbox")
	expect(t, read["last_seen"], both[0]["last_seen"])
	list := g.read("/sessions", 200).body["sessions"].([]any)
	expect(t, len(list), 1)
	expect(t, len(hosts(t, list[0].(map[string]any))), 2)

	// Neither machine's credential changes the other machine's row.
	got = put(hostPath(laptopID, session), sessionBody("cwd", "/stolen"), g.rotatorToken)
	expect(t, got.status, 403)
	expect(t, got.body["reason"], "machine-of-another-proof")
	got = put(hostPath(machineID, session), sessionBody("cwd", "/stolen"), laptop)
	expect(t, got.status, 403)
	expect(t, got.body["reason"], "machine-of-another-proof")
	expect(t, g.count(`SELECT count(*) FROM fleet_session_hosts WHERE cwd = '/stolen'`), 0)

	// Each machine still writes its own row, and the other row stays as it was.
	g.write(hostPath(machineID, session), sessionBody("cwd", "/home/elan/devbox-2"), 200)
	expect(t, g.count(`SELECT count(*) FROM fleet_session_hosts
		WHERE machine_id = '`+laptopID+`' AND cwd = '/home/elan/on-laptop'`), 1)
	expect(t, g.count(`SELECT count(*) FROM fleet_session_hosts
		WHERE machine_id = '`+machineID+`' AND cwd = '/home/elan/devbox-2'`), 1)
}

func TestTheRemoteIsAPlainGitURL(t *testing.T) {
	g := newRegistry(t)
	g.write("/machines/"+machineID, machineBody(), 200)
	for _, c := range []struct {
		remote string
		ok     bool
	}{
		{"https://github.com/o/r", true},
		{"https://github.com/o/r.git", true},
		{"http://git.lan:3000/o/r.git", true},
		{"ssh://git@github.com/o/r", true},
		{"ssh://github.com:22/o/r", true},
		{"git://github.com/o/r.git", true},
		{"git@github.com:o/r.git", true},
		{"deploy.bot-1@git.lan:team/r.git", true},
		{"https://token@github.com/o/r", false},
		{"https://u:p@github.com/o/r", false},
		{"https://u:@github.com/o/r", false},
		{"https://@github.com/o/r", false},
		{"ssh://git:pw@github.com/o/r", false},
		{"ssh://g%20it@github.com/o/r", false},
		{"git://u@github.com/o/r", false},
		{"https://github.com/o/r?x=1", false},
		{"https://github.com/o/r?", false},
		{"https://github.com/o/r#main", false},
		{"https://github.com/o/r#", false},
		{"ssh://git@github.com/o/r?x=1", false},
		{"git@github.com:o/r.git#frag", false},
		{"git@github.com:o/r.git?x", false},
		{"u:p@github.com:o/r.git", false},
		{"t@k@github.com:o/r.git", false},
		{"git@-oProxyCommand=x:o/r", false},
		{"git@github.com:", false},
		{"git@github.com:-u/r", false},
		{"github.com:o/r.git", false},
		{"/home/elan/repos/r", false},
		{"../r", false},
		{"file:///home/elan/repos/r", false},
		{"ext::sh -c touch% /tmp/pwned", false},
		{"fd::17", false},
		{"https:///o/r", false},
		{"https://-x/o/r", false},
		{"ftp://github.com/o/r", false},
		{"git+ssh://git@github.com/o/r", false},
		{"HTTPS://token@github.com/o/r", false},
		{`https://github.com\@evil/o/r`, false},
	} {
		want := 422
		if c.ok {
			want = 200
		}
		got := g.do("PUT", rotatorBase+onMachine+uuid(), sessionBody("git_remote", c.remote), bearer(g.rotatorToken))
		if got.status != want {
			t.Errorf("%q: %d %s, want %d", c.remote, got.status, got.raw, want)
		}
	}
}
