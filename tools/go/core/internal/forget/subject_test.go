package forget

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"io"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/auth"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/seal"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/testdb"
)

func TestMain(m *testing.M) { testdb.Main(m) }

const (
	parent = "aaaaaaaa-0000-4000-8000-000000000001"
	fork   = "aaaaaaaa-0000-4000-8000-000000000002"
	bare   = "aaaaaaaa-0000-4000-8000-000000000003"
)

var kek = seal.NewKEK(bytes.Repeat([]byte{0x42}, 32))

type api struct {
	t     *testing.T
	pool  *pgxpool.Pool
	url   string
	token string
}

func newAPI(t *testing.T, keys *seal.Keys) *api {
	t.Helper()
	pool := testdb.New(t)
	router := &crud.Router{Resolver: &auth.Resolver{Pool: pool}, Next: http.NotFoundHandler()}
	router.Add("agent", Areas(pool, keys)["forget"]...)
	server := httptest.NewServer(router)
	t.Cleanup(server.Close)
	a := &api{t: t, pool: pool, url: server.URL}
	id, secret := make([]byte, 8), make([]byte, 32)
	_, _ = rand.Read(id)
	_, _ = rand.Read(secret)
	a.token = "sbsvc_" + hex.EncodeToString(id) + "." + base64.RawURLEncoding.EncodeToString(secret)
	hash := sha256.Sum256([]byte(a.token))
	a.exec(`INSERT INTO service_credentials (id, caller, scopes, secret_hash, expires_at, proof)
		VALUES ($1, 'agent', '{api,crud.agent}', $2, now() + interval '15 minutes', 'inprocess')`,
		hex.EncodeToString(id), hash[:])
	return a
}

func (a *api) exec(query string, args ...any) {
	a.t.Helper()
	if _, err := a.pool.Exec(context.Background(), query, args...); err != nil {
		a.t.Fatal(err)
	}
}

func (a *api) one(query string, args ...any) string {
	a.t.Helper()
	var out string
	if err := a.pool.QueryRow(context.Background(), query, args...).Scan(&out); err != nil {
		a.t.Fatal(err)
	}
	return out
}

func (a *api) forget(subject string) (int, string) {
	a.t.Helper()
	req, _ := http.NewRequest("POST", a.url+"/api/v1/internal/crud/agent/forgotten-subjects/"+subject, nil)
	req.Header.Set("Authorization", "Bearer "+a.token)
	res, err := http.DefaultClient.Do(req)
	if err != nil {
		a.t.Fatal(err)
	}
	defer res.Body.Close()
	raw, _ := io.ReadAll(res.Body)
	return res.StatusCode, string(raw)
}

func (a *api) session(id string, forkOf string, cut, eventCut any) {
	a.t.Helper()
	var parentID any
	if forkOf != "" {
		parentID = forkOf
	}
	a.exec(`INSERT INTO ai_sessions (id, origin, owner_kind, owner_id, owner_label, creator_kind,
		creator_id, status, max_turns, budget_usd, fork_parent_id, fork_cut, fork_event_cut)
		VALUES ($1, 'chat', 'browser', 'ada', 'Ada', 'browser', 'ada', 'idle', 10, 5, $2, $3, $4)`,
		id, parentID, cut, eventCut)
}

// key makes a session's data key, as payload_keys.py makes one.
func (a *api) key(session string) []byte {
	a.t.Helper()
	dek := make([]byte, 32)
	_, _ = rand.Read(dek)
	subject := "session-" + session
	a.exec(`INSERT INTO ai_payload_keys (subject, dek_sealed, kek_id) VALUES ($1, $2, $3)`,
		subject, seal.SealBytes(kek.Key(), dek, seal.PayloadKeyContext(subject)), kek.ID)
	return dek
}

func (a *api) openKey(session string) []byte {
	a.t.Helper()
	var sealed []byte
	subject := "session-" + session
	if err := a.pool.QueryRow(context.Background(), `SELECT dek_sealed FROM ai_payload_keys
		WHERE subject = $1`, subject).Scan(&sealed); err != nil {
		a.t.Fatal(err)
	}
	dek, err := seal.OpenBytes(kek.Key(), sealed, seal.PayloadKeyContext(subject))
	if err != nil {
		a.t.Fatal(err)
	}
	return dek
}

var entries = []string{
	`{"type":"user","uuid":"u1","message":"hi"}`,
	`{"type":"assistant","message":"ok"}`,
	`{"type":"user","uuid":"u3","message":"past the cut"}`,
}

func (a *api) parentWithFork(cut int) {
	a.t.Helper()
	a.session(parent, "", nil, nil)
	dek := a.key(parent)
	for i, text := range entries {
		id := int64(100 + i)
		bound := "#" + strconv.FormatInt(id, 10)
		var uuid any
		if strings.Contains(text, `"uuid"`) {
			bound = "u" + strconv.Itoa(i+1)
			uuid = bound
		}
		a.exec(`INSERT INTO ai_session_entries (id, project_key, session_id, subpath, uuid, entry_type,
			sealed) VALUES ($1, '', $2, '', $3, 'user', $4)`, id, parent, uuid,
			seal.SealBytes(dek, []byte(text), "ai_session_entries:"+parent+"::"+bound))
	}
	a.exec(`INSERT INTO ai_session_events (session_id, seq, event) VALUES
		($1, 1, '{"type":"user.turn","sessionId":"`+parent+`","n":1E2,"images":[{"name":"1111111111111111111111111111111111111111111111111111111111111111.png","preview":{"name":"2222222222222222222222222222222222222222222222222222222222222222.png"}}]}'),
		($1, 2, '{"type":"status","sessionId":"`+parent+`"}'),
		($1, 3, '{"type":"tool.result","images":[{"name":"3333333333333333333333333333333333333333333333333333333333333333.png"}]}'),
		($1, 4, '{"type":"tool.call","name":"past the cut"}')`, parent)
	a.exec(`INSERT INTO ai_session_blobs (session_id, name, media_type, data) VALUES
		($1, '1111111111111111111111111111111111111111111111111111111111111111.png', 'image/png', '\x01'), ($1, '2222222222222222222222222222222222222222222222222222222222222222.png', 'image/png', '\x02'),
		($1, '3333333333333333333333333333333333333333333333333333333333333333.png', 'image/png', '\x03'), ($1, '4444444444444444444444444444444444444444444444444444444444444444.png', 'image/png', '\x04')`, parent)
	a.session(fork, parent, cut, 3)
}

func TestAForgetHasItsForksTakeTheirRangesFirst(t *testing.T) {
	a := newAPI(t, &seal.Keys{Current: kek})
	a.parentWithFork(2)
	status, raw := a.forget("session-" + parent)
	if status != 200 || raw != `{"key":true}` {
		t.Fatalf("%d %s", status, raw)
	}
	if got := a.one(`SELECT (SELECT count(*) FROM ai_forgotten_subjects WHERE subject = $1) || ' ' ||
		(SELECT count(*) FROM ai_payload_keys WHERE subject = $1)`, "session-"+parent); got != "1 0" {
		t.Fatalf("tombstone and key %s", got)
	}
	// The fork's two inherited rows, sealed under its own key.
	dek := a.openKey(fork)
	rows, err := a.pool.Query(context.Background(), `SELECT id, uuid, entry_type, sealed, inherited
		FROM ai_session_entries WHERE session_id = $1 ORDER BY id`, fork)
	if err != nil {
		t.Fatal(err)
	}
	var got []string
	for rows.Next() {
		var id int64
		var uuid, kind *string
		var sealed []byte
		var inherited bool
		if err := rows.Scan(&id, &uuid, &kind, &sealed, &inherited); err != nil {
			t.Fatal(err)
		}
		bound := "#" + strconv.FormatInt(id, 10)
		if uuid != nil {
			bound = *uuid
		}
		plain, err := seal.OpenBytes(dek, sealed, "ai_session_entries:"+fork+"::"+bound)
		if err != nil || !inherited || kind == nil {
			t.Fatalf("row %d: %v %v", id, err, inherited)
		}
		got = append(got, *kind+" "+string(plain))
	}
	if strings.Join(got, "\n") != "user "+entries[0]+"\nassistant "+entries[1] {
		t.Fatalf("entries %q", got)
	}
	if events := a.one(`SELECT string_agg(seq || ' ' || event, E'\n' ORDER BY seq) FROM ai_session_events
		WHERE session_id = $1`, fork); events != `1 {"type":"user.turn","sessionId":"`+fork+
		`","n":100.0,"images":[{"name":"1111111111111111111111111111111111111111111111111111111111111111.png","preview":{"name":"2222222222222222222222222222222222222222222222222222222222222222.png"}}]}`+"\n"+
		`3 {"type":"tool.result","images":[{"name":"3333333333333333333333333333333333333333333333333333333333333333.png"}],"sessionId":"`+fork+`"}` {
		t.Fatalf("events %s", events)
	}
	if blobs := a.one(`SELECT string_agg(name, ',' ORDER BY name) FROM ai_session_blobs WHERE session_id = $1`,
		fork); blobs != "1111111111111111111111111111111111111111111111111111111111111111.png,2222222222222222222222222222222222222222222222222222222222222222.png,3333333333333333333333333333333333333333333333333333333333333333.png" {
		t.Fatalf("blobs %s", blobs)
	}
	if link := a.one(`SELECT concat_ws(',', fork_parent_id, fork_cut, fork_event_cut, 'cleared')
		FROM ai_sessions WHERE id = $1`, fork); link != "cleared" {
		t.Fatalf("link %s", link)
	}
	// Again: no key now, and no forks.
	if status, raw := a.forget("session-" + parent); status != 200 || raw != `{"key":false}` {
		t.Fatalf("%d %s", status, raw)
	}
}

func TestAForgottenForkTakesNoRowsOnlyItsLinkCleared(t *testing.T) {
	a := newAPI(t, &seal.Keys{Current: kek})
	a.parentWithFork(2)
	a.exec(`INSERT INTO ai_forgotten_subjects (subject) VALUES ($1)`, "session-"+fork)
	if status, raw := a.forget("session-" + parent); status != 200 {
		t.Fatalf("%d %s", status, raw)
	}
	if got := a.one(`SELECT (SELECT count(*) FROM ai_session_entries WHERE session_id = $1) +
		(SELECT count(*) FROM ai_session_events WHERE session_id = $1::uuid) +
		(SELECT count(*) FROM ai_session_blobs WHERE session_id = $1::uuid) +
		(SELECT count(*) FROM ai_payload_keys WHERE subject = 'session-' || $1) +
		(SELECT count(*) FROM ai_sessions WHERE id = $1::uuid AND fork_parent_id IS NOT NULL)`, fork); got != "0" {
		t.Fatalf("rows %s", got)
	}
}

func TestWithNoKEKAForkThatTakesEntriesIsRefused(t *testing.T) {
	a := newAPI(t, nil)
	a.parentWithFork(1)
	status, raw := a.forget("session-" + parent)
	if status != 409 || !strings.Contains(raw, `"reason":"forks-take-ranges"`) ||
		!strings.Contains(raw, `"detail":"session-`+parent+` has forks, which take their ranges of its`+
			` transcript first, and the API has no SCADBUDDY_SECRET_KEY_FILE."`) {
		t.Fatalf("%d %s", status, raw)
	}
	if got := a.one(`SELECT (SELECT count(*) FROM ai_forgotten_subjects) || ' ' ||
		(SELECT count(*) FROM ai_payload_keys) || ' ' ||
		(SELECT count(*) FROM ai_session_events WHERE session_id = $1::uuid)`, fork); got != "0 1 0" {
		t.Fatalf("written %s", got)
	}
	// A fork that takes no entries needs no KEK: its events still move.
	a.exec(`UPDATE ai_sessions SET fork_cut = 0 WHERE id = $1`, fork)
	if status, raw := a.forget("session-" + parent); status != 200 || raw != `{"key":true}` {
		t.Fatalf("%d %s", status, raw)
	}
	if got := a.one(`SELECT count(*)::text FROM ai_session_events WHERE session_id = $1::uuid`, fork); got != "2" {
		t.Fatalf("events %s", got)
	}
}

func TestAFlowsImagesGoWithItsKey(t *testing.T) {
	a := newAPI(t, nil)
	flow := "flow-" + bare
	a.exec(`INSERT INTO ai_flow_blobs (subject, name, media_type, size, data)
		VALUES ($1, '5555555555555555555555555555555555555555555555555555555555555555.png', 'image/png', 1, '\x01')`, flow)
	a.exec(`INSERT INTO ai_payload_keys (subject, dek_sealed, kek_id) VALUES ($1, '\x00', 'k')`, flow)
	if status, raw := a.forget(flow); status != 200 || raw != `{"key":true}` {
		t.Fatalf("%d %s", status, raw)
	}
	if got := a.one(`SELECT (SELECT count(*) FROM ai_flow_blobs) || ' ' ||
		(SELECT count(*) FROM ai_forgotten_subjects WHERE subject = $1)`, flow); got != "0 1" {
		t.Fatalf("rows %s", got)
	}
	// A session with no row and no forks is forgotten all the same.
	if status, raw := a.forget("session-" + bare); status != 200 || raw != `{"key":false}` {
		t.Fatalf("%d %s", status, raw)
	}
}

func TestASubjectForgetIsRefusedForABridgeOrAnotherName(t *testing.T) {
	a := newAPI(t, nil)
	if status, raw := a.forget("bridge-" + bare); status != 422 ||
		!strings.Contains(raw, `"detail":"a browser call's key is dropped, not forgotten"`) {
		t.Fatalf("%d %s", status, raw)
	}
	if status, raw := a.forget("job-" + bare); status != 422 || !strings.Contains(raw, `"loc":["path","subject"]`) {
		t.Fatalf("%d %s", status, raw)
	}
	if got := a.one(`SELECT count(*)::text FROM ai_forgotten_subjects`); got != "0" {
		t.Fatalf("rows %s", got)
	}
}
