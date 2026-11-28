package main

import (
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/seal"
)

// The fleet's logins (#7984; spec 2026-11-26-fleet-session-registry-and-rotator-design §6.2):
// the rotator's lease, write-back, release, event and rotation routes, each bound to the
// caller's machine, and a person's route that adds a fleet login.

const fleetLoginsPath = "/api/v1/fleet/logins"

// logins is the API with areas fleet, fleet-logins and credentials, and a KEK.
type logins struct {
	*api
	// token is a fleet access token that owns machineID.
	token string
	// cookie is a person signed in themselves.
	cookie string
}

func newLogins(t *testing.T) *logins {
	t.Helper()
	file := filepath.Join(t.TempDir(), "kek")
	if err := os.WriteFile(file, []byte(base64.StdEncoding.EncodeToString(make([]byte, 32))+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	keys, err := seal.LoadKeys(func(name string) string { return map[string]string{seal.Variable: file}[name] })
	if err != nil {
		t.Fatal(err)
	}
	a := newAPIWith(t, "fleet, fleet-logins, credentials", "", keys)
	l := &logins{api: a}
	l.token, _ = accessToken(a, "fleet")
	l.cookie = l.person(alice)
	expect(t, l.do("PUT", rotatorBase+"/machines/"+machineID, machineBody(), bearer(l.token)).status, 200)
	return l
}

// person is a session cookie of principal, signed in themselves.
func (l *logins) person(principal string) string {
	l.t.Helper()
	b := make([]byte, 32)
	_, _ = rand.Read(b)
	token := base64.RawURLEncoding.EncodeToString(b)
	l.sql(`INSERT INTO auth_sessions (id_hash, principal, issuer, subject, kind, created_at, last_seen_at,
		idle_expires_at, absolute_expires_at) VALUES (sha256($1::bytea), $2, 'i', $2, 'login', now(), now(),
		now() + interval '1 day', now() + interval '2 days')`, []byte(token), principal)
	return "__Host-scadbuddy_session=" + token
}

func cookie(c string) func(*http.Request) {
	return func(r *http.Request) {
		r.Header.Del("Authorization")
		r.Header.Set("Cookie", c)
		r.Header.Set("X-ScadBuddy-Request", "1")
	}
}

// loginOf is a claudeAiOauth object of account, its access token ending in tail.
func loginOf(account, tail string) string {
	return object("accessToken", "sk-ant-oat01-access-"+tail, "refreshToken", "sk-ant-ort01-refresh-"+tail,
		"expiresAt", 1795000000000, "accountUuid", account, "subscriptionType", "max")
}

// add adds a fleet login as a person does, and answers its id.
func (l *logins) add(login string) string {
	l.t.Helper()
	got := l.do("POST", fleetLoginsPath, object("kind", "claude_login", "secret", login), cookie(l.cookie))
	if got.status != 200 {
		l.t.Fatalf("add a fleet login: %d %s", got.status, got.raw)
	}
	return got.body["credential"].(map[string]any)["id"].(string)
}

func (l *logins) rotator(method, path, body string, status int) answer {
	l.t.Helper()
	got := l.do(method, rotatorBase+path, body, bearer(l.token))
	if got.status != status {
		l.t.Fatalf("%s %s: %d %s, want %d", method, path, got.status, got.raw, status)
	}
	return got
}

// homeDir is the config dir the tests lease for.
const homeDir = "/home/elan/.claude"

func (l *logins) lease() answer {
	l.t.Helper()
	return l.rotator("POST", "/leases", object("machine_id", machineID, "config_dir", homeDir), 200)
}

// leaseIn leases for dir; replacing, when given, is the {credential_id, lease_epoch} the new
// lease is to replace (a rotation).
func (l *logins) leaseIn(dir string, replacing map[string]any) answer {
	l.t.Helper()
	body := map[string]any{"machine_id": machineID, "config_dir": dir}
	if replacing != nil {
		body["replacing"] = replacing
	}
	return l.rotator("POST", "/leases", mustJSON(body), 200)
}

// leaseOf is a lease answer's lease.
func leaseOf(t *testing.T, got answer) map[string]any {
	t.Helper()
	lease, ok := got.body["lease"].(map[string]any)
	if !ok {
		t.Fatalf("no lease: %s", got.raw)
	}
	return lease
}

func (l *logins) audits(action string) int {
	l.t.Helper()
	return l.count(`SELECT count(*) FROM ai_audit WHERE kind = 'credential' AND action = '` + action + `'`)
}

const (
	accountA = "8d2c3a4e-0000-4000-8000-00000000000a"
	accountB = "8d2c3a4e-0000-4000-8000-00000000000b"
	accountC = "8d2c3a4e-0000-4000-8000-00000000000c"
	accountD = "8d2c3a4e-0000-4000-8000-00000000000d"
)

func TestALeaseRevealsTheBestUsableLogin(t *testing.T) {
	l := newLogins(t)
	disabled := l.add(loginOf(accountA, "disabled-AAAA"))
	cooling := l.add(loginOf(accountB, "cooling-BBBB"))
	best := l.add(loginOf(accountC, "best-CCCC"))
	// Its account is cooling down on another login, so it waits too (#7995 §6.1).
	sibling := l.add(loginOf(accountB, "sibling-DDDD"))
	next := l.add(loginOf(accountD, "next-EEEE"))
	l.sql(`UPDATE ai_credentials SET status = 'disabled' WHERE id = $1`, disabled)
	l.sql(`UPDATE ai_credentials SET status = 'cooling_down', cooldown_until = now() + interval '2 hours' WHERE id = $1`, cooling)

	lease := leaseOf(t, l.lease())
	credential := lease["credential"].(map[string]any)
	expect(t, credential["id"], best)
	expect(t, []any{credential["leased_to"], lease["lease_epoch"]}, []any{machineID, 1.0})
	var login map[string]any
	if err := json.Unmarshal([]byte(mustJSON(lease["login"])), &login); err != nil {
		t.Fatal(err)
	}
	expect(t, login["accessToken"], "sk-ant-oat01-access-best-CCCC")
	expect(t, l.audits("fleet.lease"), 1)
	expect(t, l.audits("fleet.reveal"), 1)

	// A login held by a machine is not leased again.
	expect(t, leaseOf(t, l.leaseIn("/home/elan/.claude-work", nil))["credential"].(map[string]any)["id"], next)
	// The rest are disabled, cooling, or of a cooling account: none is leased.
	expect(t, l.leaseIn("/home/elan/.claude-third", nil).body["lease"], nil)
	expect(t, l.count(`SELECT count(*) FROM ai_credentials WHERE id = '`+sibling+`' AND leased_to IS NULL
		AND status = 'active'`), 1)
}

func TestNoUsableLoginAnswersWhenOneRecovers(t *testing.T) {
	l := newLogins(t)
	got := l.lease()
	expect(t, []any{got.body["lease"], got.body["recovers_at"]}, []any{nil, nil})
	cooling := l.add(loginOf(accountA, "cooling-AAAA"))
	l.sql(`UPDATE ai_credentials SET status = 'cooling_down', cooldown_until = '2099-01-01T00:00:00Z' WHERE id = $1`, cooling)
	got = l.lease()
	expect(t, []any{got.body["lease"], got.body["recovers_at"]}, []any{nil, "2099-01-01T00:00:00+00:00"})
	expect(t, l.audits("fleet.reveal"), 0)
	expect(t, l.count(`SELECT count(*) FROM ai_audit WHERE action = 'fleet.lease' AND outcome = 'refused'`), 2)
}

func TestAWriteBackAndAReleaseNeedTheLeaseEpoch(t *testing.T) {
	l := newLogins(t)
	id := l.add(loginOf(accountA, "first-AAAA"))
	epoch := leaseOf(t, l.lease())["lease_epoch"].(float64)
	refreshed := loginOf(accountA, "refreshed-ZZZZ")
	write := func(e float64, login string) answer {
		return l.do("PUT", rotatorBase+"/leases/"+id, `{"machine_id":"`+machineID+`","lease_epoch":`+
			jsonNumber(e)+`,"login":`+login+`}`, bearer(l.token))
	}
	stale := write(epoch-1, refreshed)
	expect(t, []any{stale.status, stale.body["reason"]}, []any{409, "lease-stale"})
	other := write(epoch, loginOf(accountB, "other-BBBB"))
	expect(t, []any{other.status, other.body["reason"]}, []any{409, "another-account"})
	ok := write(epoch, refreshed)
	expect(t, ok.status, 200)
	if strings.Contains(ok.raw, "refreshed-ZZZZ\"") || strings.Contains(ok.raw, "refresh-refreshed") {
		t.Fatalf("a write-back answered the secret: %s", ok.raw)
	}
	expect(t, ok.body["credential"].(map[string]any)["last4"], "ZZZZ")
	expect(t, l.audits("fleet.write_back"), 1)

	release := func(e float64) answer {
		return l.do("DELETE", rotatorBase+"/leases/"+id+"?machine_id="+machineID+"&lease_epoch="+jsonNumber(e), "",
			bearer(l.token))
	}
	gone := release(epoch + 1)
	expect(t, []any{gone.status, gone.body["reason"]}, []any{409, "lease-stale"})
	done := release(epoch)
	expect(t, done.status, 200)
	expect(t, done.body["credential"].(map[string]any)["leased_to"], nil)
	expect(t, l.audits("fleet.release"), 1)
	// The old epoch no longer writes back.
	expect(t, write(epoch, refreshed).status, 409)

	// The written-back login is what the next lease reveals.
	again := leaseOf(t, l.lease())
	expect(t, again["lease_epoch"], epoch+2)
	var login map[string]any
	_ = json.Unmarshal([]byte(mustJSON(again["login"])), &login)
	expect(t, login["refreshToken"], "sk-ant-ort01-refresh-refreshed-ZZZZ")
}

func TestAnEventIsRecordedOncePerEntryAndCoolsTheAccount(t *testing.T) {
	l := newLogins(t)
	id := l.add(loginOf(accountA, "mine-AAAA"))
	sibling := l.add(loginOf(accountA, "sibling-BBBB"))
	other := l.add(loginOf(accountB, "other-CCCC"))
	credential := leaseOf(t, l.lease())["credential"].(map[string]any)
	expect(t, credential["id"], id)
	until := time.Now().Add(3 * time.Hour).UTC().Format(time.RFC3339)
	entry := uuid()
	event := object("machine_id", machineID, "entry_uuid", entry, "session_id", uuid(), "credential_id", id,
		"epoch", credential["epoch"], "kind", "rate_limit", "until", until, "reset_source", "epoch")
	got := l.rotator("POST", "/events", event, 200)
	expect(t, got.body["duplicate"], false)
	expect(t, got.body["transition"], map[string]any{"before": "active", "after": "cooling_down", "recovered": false})
	expect(t, got.body["cooled"], []any{sibling})
	expect(t, l.count(`SELECT count(*) FROM ai_credentials WHERE status = 'cooling_down'`), 2)
	expect(t, l.count(`SELECT count(*) FROM ai_credentials WHERE id = '`+other+`' AND status = 'active'`), 1)

	again := l.rotator("POST", "/events", event, 200)
	expect(t, []any{again.body["duplicate"], again.body["transition"]}, []any{true, nil})
	expect(t, l.count(`SELECT count(*) FROM fleet_credential_events`), 1)

	// A login the machine does not lease takes no event.
	refused := l.do("POST", rotatorBase+"/events", object("machine_id", machineID, "entry_uuid", uuid(),
		"credential_id", other, "epoch", 0, "kind", "permanent", "reason", "billing_error"), bearer(l.token))
	expect(t, []any{refused.status, refused.body["reason"]}, []any{409, "not-leased"})
}

func TestARotationIsRecordedAndChecked(t *testing.T) {
	l := newLogins(t)
	from, to := l.add(loginOf(accountA, "from-AAAA")), l.add(loginOf(accountB, "to-BBBB"))
	made := l.rotator("POST", "/rotations", object("machine_id", machineID, "config_dir", "/home/elan/.claude",
		"from_credential_id", from, "to_credential_id", to, "reason", "rate_limit"), 201)
	id := made.body["id"].(string)
	expect(t, []any{made.body["sessions"], made.body["verified_at"]}, []any{[]any{}, nil})
	session := uuid()
	checked := l.rotator("PUT", "/rotations/"+id, object("sessions",
		[]any{map[string]any{"session_id": session, "result": "passed", "at": "2026-11-28T12:00:00Z"}},
		"verified_at", "2026-11-28T12:00:01Z"), 200)
	expect(t, checked.body["sessions"], []any{map[string]any{"session_id": session, "result": "passed",
		"at": "2026-11-28T12:00:00Z"}})
	if checked.body["verified_at"] == nil {
		t.Fatalf("no verified_at: %s", checked.raw)
	}
	expect(t, l.audits("fleet.rotation"), 1)
	expect(t, l.audits("fleet.rotation_check"), 1)
	missing := l.do("PUT", rotatorBase+"/rotations/"+uuid(), object("sessions", []any{}), bearer(l.token))
	expect(t, []any{missing.status, missing.body["reason"]}, []any{404, "unknown-rotation"})
}

// A token or proof uses these routes for its own machines only.
func TestTheRoutesAreBoundToTheCallersMachine(t *testing.T) {
	l := newLogins(t)
	id := l.add(loginOf(accountA, "mine-AAAA"))
	theirs, _ := accessToken(l.api, "fleet")
	laptop := strings.Repeat("1", 64)
	expect(t, l.do("PUT", rotatorBase+"/machines/"+laptop, machineBody(), bearer(theirs)).status, 200)
	epoch := leaseOf(t, l.lease())["lease_epoch"].(float64)
	rotation := l.rotator("POST", "/rotations", object("machine_id", machineID, "reason", "rate_limit"), 201).body["id"].(string)
	proof := rotatorCredential(l.api, "devbox")

	for who, token := range map[string]string{"another token": theirs, "a bootstrap proof": proof} {
		for name, c := range map[string]struct{ method, path, body string }{
			"lease":     {"POST", "/leases", object("machine_id", machineID, "config_dir", homeDir)},
			"write":     {"PUT", "/leases/" + id, `{"machine_id":"` + machineID + `","lease_epoch":` + jsonNumber(epoch) + `,"login":` + loginOf(accountA, "x-XXXX") + `}`},
			"release":   {"DELETE", "/leases/" + id + "?machine_id=" + machineID + "&lease_epoch=" + jsonNumber(epoch), ""},
			"event":     {"POST", "/events", object("machine_id", machineID, "entry_uuid", uuid(), "credential_id", id, "epoch", 0, "kind", "recovery")},
			"rotation":  {"POST", "/rotations", object("machine_id", machineID, "reason", "x")},
			"its check": {"PUT", "/rotations/" + rotation, object("sessions", []any{})},
		} {
			got := l.do(c.method, rotatorBase+c.path, c.body, bearer(token))
			if got.status != 403 || got.body["reason"] != "machine-of-another-proof" {
				t.Errorf("%s, %s: %d %s", who, name, got.status, got.raw)
			}
		}
	}
	// On its own machine, another token holds no lease of mine.
	write := l.do("PUT", rotatorBase+"/leases/"+id, `{"machine_id":"`+laptop+`","lease_epoch":`+jsonNumber(epoch)+
		`,"login":`+loginOf(accountA, "x-XXXX")+`}`, bearer(theirs))
	expect(t, []any{write.status, write.body["reason"]}, []any{409, "lease-stale"})
	unknown := l.do("POST", rotatorBase+"/leases", object("machine_id", strings.Repeat("2", 64), "config_dir", homeDir), bearer(l.token))
	expect(t, []any{unknown.status, unknown.body["reason"]}, []any{404, "unknown-machine"})
	expect(t, l.audits("fleet.write_back"), 0)
	expect(t, l.count(`SELECT count(*) FROM ai_credentials WHERE leased_to = '`+machineID+`'`), 1)
	expect(t, l.count(`SELECT count(*) FROM fleet_rotations`), 1)
}

// rotatorRoutes are every lease, event and rotation route, with a body that would pass.
func rotatorRoutes(id string) map[string]struct{ method, path, body string } {
	return map[string]struct{ method, path, body string }{
		"lease":     {"POST", "/leases", object("machine_id", machineID, "config_dir", homeDir)},
		"write":     {"PUT", "/leases/" + id, `{"machine_id":"` + machineID + `","lease_epoch":1,"login":` + loginOf(accountA, "x-XXXX") + `}`},
		"release":   {"DELETE", "/leases/" + id + "?machine_id=" + machineID + "&lease_epoch=1", ""},
		"event":     {"POST", "/events", object("machine_id", machineID, "entry_uuid", uuid(), "credential_id", id, "epoch", 0, "kind", "recovery")},
		"rotation":  {"POST", "/rotations", object("machine_id", machineID, "reason", "x")},
		"its check": {"PUT", "/rotations/" + uuid(), object("sessions", []any{})},
	}
}

// The fleet chat server's caller never reaches a secret: every one of these routes refuses it.
func TestTheFleetCallerReachesNoLeaseRoute(t *testing.T) {
	l := newLogins(t)
	id := l.add(loginOf(accountA, "mine-AAAA"))
	fleetCaller := l.credential("fleet", "crud.fleet", "crud.fleet-rotator")
	for name, c := range rotatorRoutes(id) {
		got := l.do(c.method, rotatorBase+c.path, c.body, bearer(fleetCaller))
		if got.status != 403 || got.body["reason"] != "wrong_caller" {
			t.Errorf("%s: %d %s", name, got.status, got.raw)
		}
	}
	expect(t, l.count(`SELECT count(*) FROM ai_credentials WHERE leased_to IS NOT NULL`), 0)
	expect(t, l.count(`SELECT count(*) FROM fleet_rotations`), 0)
}

func TestAReadOrWriteTokenReachesNoLeaseRoute(t *testing.T) {
	l := newLogins(t)
	id := l.add(loginOf(accountA, "mine-AAAA"))
	for _, scope := range []string{"read", "write"} {
		token, _ := accessToken(l.api, scope)
		for name, c := range rotatorRoutes(id) {
			got := l.do(c.method, rotatorBase+c.path, c.body, bearer(token))
			if got.status != 403 || got.body["reason"] != "out_of_scope" {
				t.Errorf("%s token, %s: %d %s", scope, name, got.status, got.raw)
			}
		}
	}
	expect(t, l.count(`SELECT count(*) FROM ai_credentials WHERE leased_to IS NOT NULL`), 0)
}

// A person adds a fleet login without the agent (#8009's agent=False path), and the agent's
// caller still never reveals it.
func TestAPersonAddsAFleetLoginTheAgentNeverReveals(t *testing.T) {
	l := newLogins(t)
	login := loginOf(accountA, "person-AAAA")
	got := l.do("POST", fleetLoginsPath, object("kind", "claude_login", "secret", login, "name", "spark"), cookie(l.cookie))
	expect(t, got.status, 200)
	row := got.body["credential"].(map[string]any)
	expect(t, []any{row["kind"], row["scope"], row["name"], row["account_uuid"]}, []any{"claude_login", "fleet", "spark", accountA})
	if strings.Contains(got.raw, "person-AAAA\"") || strings.Contains(got.raw, "refresh-person") {
		t.Fatalf("the add answered the secret: %s", got.raw)
	}
	expect(t, l.audits("fleet.add"), 1)
	// Another kind is a fleet login when its scope is fleet or both; scope agent is not one.
	both := l.do("POST", fleetLoginsPath, object("kind", "claude_oauth_token", "secret", "sk-ant-oat01-shared-token-1234",
		"scope", "both"), cookie(l.cookie))
	expect(t, both.status, 200)
	agent := l.do("POST", fleetLoginsPath, object("kind", "claude_oauth_token", "secret", "sk-ant-oat01-agent-token-1234",
		"scope", "agent"), cookie(l.cookie))
	expect(t, agent.status, 422)

	// The agent's reveal of every credential opens the `both` row, never the login.
	revealed := l.do("POST", base+"/credentials/reveal", object("all", true, "purpose", "turn"))
	expect(t, revealed.status, 200)
	if strings.Contains(revealed.raw, "person-AAAA") || strings.Contains(revealed.raw, row["id"].(string)) {
		t.Fatalf("the agent revealed the fleet login: %s", revealed.raw)
	}
	one := l.do("POST", base+"/credentials/reveal", object("id", row["id"], "purpose", "turn"))
	expect(t, one.body["credential"], nil)

	// Only a person signed in themselves adds one: no token, service or anonymous caller.
	write, _ := accessToken(l.api, "write")
	fleetToken, _ := accessToken(l.api, "fleet")
	for name, with := range map[string]func(*http.Request){
		"a write token":     bearer(write),
		"a fleet token":     bearer(fleetToken),
		"the agent service": bearer(l.api.token),
		"no one":            func(r *http.Request) { r.Header.Del("Authorization") },
		"no request header": func(r *http.Request) { r.Header.Del("Authorization"); r.Header.Set("Cookie", l.cookie) },
	} {
		got := l.do("POST", fleetLoginsPath, object("kind", "claude_login", "secret", login), with)
		if got.status != 401 && got.status != 403 {
			t.Errorf("%s: %d %s", name, got.status, got.raw)
		}
	}
	expect(t, l.count(`SELECT count(*) FROM ai_credentials WHERE kind = 'claude_login'`), 1)
}

func jsonNumber(f float64) string { return mustJSON(int64(f)) }

// loginWithout is a claudeAiOauth object that names no account.
func loginWithout(tail string) string {
	return object("accessToken", "sk-ant-oat01-access-"+tail, "refreshToken", "sk-ant-ort01-refresh-"+tail,
		"expiresAt", 1795000000000)
}

// A write-back never changes the row's account: a row with one takes only a login of that
// account, and a row with none is never given one (only a person's add sets it).
func TestAWriteBackKeepsTheRowsAccount(t *testing.T) {
	l := newLogins(t)
	id := l.add(loginOf(accountA, "first-AAAA"))
	epoch := jsonNumber(leaseOf(t, l.lease())["lease_epoch"].(float64))
	write := func(id, epoch, login string) answer {
		return l.do("PUT", rotatorBase+"/leases/"+id, `{"machine_id":"`+machineID+`","lease_epoch":`+epoch+
			`,"login":`+login+`}`, bearer(l.token))
	}
	missing := write(id, epoch, loginWithout("none-ZZZZ"))
	expect(t, []any{missing.status, missing.body["reason"]}, []any{409, "another-account"})

	bare := l.add(loginWithout("bare-BBBB"))
	bareEpoch := jsonNumber(leaseOf(t, l.leaseIn("/home/elan/.claude-bare", nil))["lease_epoch"].(float64))
	expect(t, write(bare, bareEpoch, loginOf(accountB, "set-YYYY")).status, 200)
	expect(t, l.count(`SELECT count(*) FROM ai_credentials WHERE id = '`+bare+`' AND account_uuid IS NULL
		AND subscription_type IS NULL`), 1)
}

// A rate limit's until is now to 7 days on: Claude's usage windows are five hours and a week
// (tools/go/claudecreds Event.LimitType, quotaLimits.rateLimitType).
func TestAnEventsUntilIsBounded(t *testing.T) {
	l := newLogins(t)
	id := l.add(loginOf(accountA, "mine-AAAA"))
	l.add(loginOf(accountA, "sibling-BBBB"))
	credential := leaseOf(t, l.lease())["credential"].(map[string]any)
	for name, until := range map[string]time.Time{
		"in the past":    time.Now().Add(-time.Hour),
		"past 7 days on": time.Now().Add(8 * 24 * time.Hour),
	} {
		got := l.do("POST", rotatorBase+"/events", object("machine_id", machineID, "entry_uuid", uuid(),
			"credential_id", id, "epoch", credential["epoch"], "kind", "rate_limit",
			"until", until.UTC().Format(time.RFC3339)), bearer(l.token))
		if got.status != 400 {
			t.Errorf("%s: %d %s", name, got.status, got.raw)
		}
	}
	expect(t, l.count(`SELECT count(*) FROM ai_credentials WHERE status = 'cooling_down'`), 0)
	expect(t, l.count(`SELECT count(*) FROM fleet_credential_events`), 0)
}

// One lease per config dir: a lease asked again answers the one held, a rotation names the
// lease it replaces, and a lease no one renews ends after its TTL.
func TestALeaseIsOnePerConfigDirAndEnds(t *testing.T) {
	l := newLogins(t)
	first := l.add(loginOf(accountA, "first-AAAA"))
	second := l.add(loginOf(accountB, "second-BBBB"))
	third := l.add(loginOf(accountC, "third-CCCC"))
	held := leaseOf(t, l.lease())
	expect(t, held["credential"].(map[string]any)["id"], first)
	again := leaseOf(t, l.lease())
	expect(t, []any{again["credential"].(map[string]any)["id"], again["lease_epoch"]},
		[]any{first, held["lease_epoch"]})
	expect(t, l.count(`SELECT count(*) FROM ai_credentials WHERE leased_to IS NOT NULL`), 1)

	// A rotation: the next login, while the old one is still held; asked again, the same one.
	replacing := map[string]any{"credential_id": first, "lease_epoch": held["lease_epoch"]}
	next := leaseOf(t, l.leaseIn(homeDir, replacing))
	expect(t, next["credential"].(map[string]any)["id"], second)
	expect(t, leaseOf(t, l.leaseIn(homeDir, replacing))["credential"].(map[string]any)["id"], second)
	expect(t, l.count(`SELECT count(*) FROM ai_credentials WHERE leased_to IS NOT NULL`), 2)
	stale := l.do("POST", rotatorBase+"/leases", mustJSON(map[string]any{"machine_id": machineID,
		"config_dir": homeDir, "replacing": map[string]any{"credential_id": third, "lease_epoch": 1}}), bearer(l.token))
	expect(t, []any{stale.status, stale.body["reason"]}, []any{409, "lease-stale"})

	// A renew moves the lease's end on; one that ended is free to another machine.
	renew := l.rotator("POST", "/leases/"+first+"/renew", object("machine_id", machineID,
		"lease_epoch", held["lease_epoch"]), 200)
	expect(t, renew.body["credential"].(map[string]any)["leased_to"], machineID)
	expect(t, l.count(`SELECT count(*) FROM ai_credentials WHERE id = '`+first+`'
		AND lease_expires_at > now() + interval '23 hours'`), 1)
	l.sql(`UPDATE ai_credentials SET lease_expires_at = now() - interval '1 second' WHERE id = $1`, first)
	theirs, _ := accessToken(l.api, "fleet")
	laptop := strings.Repeat("1", 64)
	expect(t, l.do("PUT", rotatorBase+"/machines/"+laptop, machineBody(), bearer(theirs)).status, 200)
	taken := l.do("POST", rotatorBase+"/leases", object("machine_id", laptop, "config_dir", homeDir), bearer(theirs))
	expect(t, taken.status, 200)
	expect(t, leaseOf(t, taken)["credential"].(map[string]any)["id"], first)
	// The old holder's lease is stale now.
	expect(t, l.do("POST", rotatorBase+"/leases/"+first+"/renew", object("machine_id", machineID,
		"lease_epoch", held["lease_epoch"]), bearer(l.token)).status, 409)
}
