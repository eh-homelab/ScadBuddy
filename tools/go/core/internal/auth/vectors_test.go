package auth

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"reflect"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/testdb"
)

func TestMain(m *testing.M) { testdb.Main(m) }

// vectorFile is the shared vectors that backend/tests/auth/test_auth_vectors.py writes.
const vectorFile = "../../../../../backend/tests/fixtures/auth-vectors.json"

type sessionRow struct {
	Name, Subject, Kind, Token string
	LastSeen                   int64 `json:"last_seen"`
	Idle, Absolute             int64
	Revoked                    *int64
}

type tokenRow struct {
	Name, Scope, Token, ID, Principal string
	Expires                           int64
	LastUsed                          *int64 `json:"last_used"`
	Revoked                           *int64
}

type credentialRow struct {
	Name, Caller, Token, ID string
	Scopes                  []string
	Expires                 int64
	LastUsed                *int64 `json:"last_used"`
	Revoked                 *int64
}

type vectors struct {
	SessionCookie string `json:"session_cookie"`
	IdleTimeout   int64  `json:"idle_timeout_seconds"`
	Issuer        string
	Sessions      []sessionRow
	Tokens        []tokenRow
	Credentials   []credentialRow
	Suspended     []string
	Callers       []struct {
		Name    string
		Headers map[string]string
		Caller  map[string]any
		Writes  []string
	}
	ModeCallers map[string]map[string]string `json:"mode_callers"`
	Modes       []struct {
		Mode, Caller, Method, Path string
		RequestHeader              bool `json:"request_header"`
		Accepted                   *bool
		Status                     int
		Body                       map[string]any
		WWWAuthenticate            *string `json:"www_authenticate"`
		Counted                    [][2]string
	}
	WouldRefuse struct {
		Callers map[string]map[string]any
		Columns []string
		Rows    [][]any
	} `json:"would_refuse"`
	Refusals []struct {
		Class, Reason, Type, Title, Detail string
		Bearer                             bool
		Status                             int
		Extensions                         map[string]any
		WWWAuthenticate                    *string `json:"www_authenticate"`
	}
	ModeSettings []struct {
		Env  *string
		Mode *string
	} `json:"mode_settings"`
	Metric struct {
		Name   string
		Labels []string
	}
}

func loadVectors(t *testing.T) *vectors {
	t.Helper()
	raw, err := os.ReadFile(vectorFile)
	if err != nil {
		t.Fatal(err)
	}
	var v vectors
	if err := json.Unmarshal(raw, &v); err != nil {
		t.Fatal(err)
	}
	return &v
}

// seed writes the vectors' rows, their times counted from now, as the Python test does.
func seed(t *testing.T, pool *pgxpool.Pool, v *vectors) {
	t.Helper()
	ctx := context.Background()
	now := time.Now().UTC()
	at := func(s *int64) *time.Time {
		if s == nil {
			return nil
		}
		x := now.Add(time.Duration(*s) * time.Second)
		return &x
	}
	n := func(s int64) *int64 { return &s }
	exec := func(sql string, args ...any) {
		t.Helper()
		if _, err := pool.Exec(ctx, sql, args...); err != nil {
			t.Fatal(err)
		}
	}
	for _, r := range v.Sessions {
		h := sha256.Sum256([]byte(r.Token))
		exec(`INSERT INTO auth_sessions (id_hash, principal, issuer, subject, kind, created_at,
			last_seen_at, idle_expires_at, absolute_expires_at, revoked_at)
			VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)`,
			h[:], "user:"+v.Issuer+"#"+r.Subject, v.Issuer, r.Subject, r.Kind, at(n(-86400)),
			at(&r.LastSeen), at(&r.Idle), at(&r.Absolute), at(r.Revoked))
	}
	for _, r := range v.Tokens {
		h := sha256.Sum256([]byte(r.Token))
		exec(`INSERT INTO personal_access_tokens (id, principal, name, scope, token_hash, created_at,
			expires_at, last_used_at, revoked_at) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)`,
			r.ID, r.Principal, r.Name, r.Scope, h[:], at(n(-86400)), at(&r.Expires), at(r.LastUsed), at(r.Revoked))
	}
	for _, r := range v.Credentials {
		h := sha256.Sum256([]byte(r.Token))
		exec(`INSERT INTO service_credentials (id, caller, scopes, secret_hash, created_at, expires_at,
			last_used_at, revoked_at, proof) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'inprocess')`,
			r.ID, r.Caller, r.Scopes, h[:], at(n(-60)), at(&r.Expires), at(r.LastUsed), at(r.Revoked))
	}
	for _, c := range v.Suspended {
		exec(`INSERT INTO service_caller_suspensions (caller, suspended_at, suspended_by)
			VALUES ($1, $2, 'test')`, c, now)
	}
}

// rowTimes is each row's times that a lookup may move, by token.
func rowTimes(t *testing.T, pool *pgxpool.Pool, v *vectors) map[string]string {
	t.Helper()
	ctx := context.Background()
	out := map[string]string{}
	one := func(token, sql string, arg any) {
		var s string
		if err := pool.QueryRow(ctx, sql, arg).Scan(&s); err != nil {
			t.Fatal(err)
		}
		out[token] = s
	}
	for _, r := range v.Sessions {
		h := sha256.Sum256([]byte(r.Token))
		one(r.Token, `SELECT last_seen_at::text || idle_expires_at::text FROM auth_sessions WHERE id_hash = $1`, h[:])
	}
	for _, r := range v.Tokens {
		one(r.Token, `SELECT coalesce(last_used_at::text, '') FROM personal_access_tokens WHERE id = $1`, r.ID)
	}
	for _, r := range v.Credentials {
		one(r.Token, `SELECT coalesce(last_used_at::text, '') FROM service_credentials WHERE id = $1`, r.ID)
	}
	return out
}

func describe(c Caller) map[string]any {
	switch c.Kind {
	case KindPerson:
		return map[string]any{"kind": "person", "principal": c.Principal, "session_kind": c.SessionKind}
	case KindToken:
		return map[string]any{"kind": "token", "principal": c.Principal, "scope": c.TokenScope, "token_id": c.TokenID}
	case KindService:
		scopes := slices.Clone(c.Service.Scopes)
		slices.Sort(scopes)
		list := make([]any, len(scopes))
		for i, s := range scopes {
			list[i] = s
		}
		return map[string]any{"kind": "service", "caller": c.Service.Caller, "scopes": list}
	}
	return map[string]any{"kind": "anonymous"}
}

func header(m map[string]string) http.Header {
	h := http.Header{}
	for k, v := range m {
		h.Set(k, v)
	}
	return h
}

func TestCallerVectors(t *testing.T) {
	v := loadVectors(t)
	pool := testdb.New(t)
	seed(t, pool, v)
	r := &Resolver{Pool: pool}
	for _, c := range v.Callers {
		before := rowTimes(t, pool, v)
		got := r.Caller(context.Background(), header(c.Headers))
		after := rowTimes(t, pool, v)
		if !reflect.DeepEqual(describe(got), c.Caller) {
			t.Errorf("%s: caller %v, want %v", c.Name, describe(got), c.Caller)
		}
		var writes []string
		for token, s := range after {
			if s != before[token] {
				writes = append(writes, token)
			}
		}
		if len(writes) != len(c.Writes) || (len(writes) == 1 && writes[0] != c.Writes[0]) {
			t.Errorf("%s: wrote %v, want %v", c.Name, writes, c.Writes)
		}
	}
	// The slide: the idle deadline is the last-seen time plus the idle timeout, at most the
	// absolute deadline.
	for _, r := range v.Sessions {
		if r.Name != "slides" && r.Name != "capped" {
			continue
		}
		h := sha256.Sum256([]byte(r.Token))
		var right bool
		if err := pool.QueryRow(context.Background(), `SELECT idle_expires_at =
			LEAST(last_seen_at + make_interval(secs => $2), absolute_expires_at)
			FROM auth_sessions WHERE id_hash = $1`, h[:], v.IdleTimeout).Scan(&right); err != nil {
			t.Fatal(err)
		}
		if !right {
			t.Errorf("session %s: the idle deadline did not slide as Python slides it", r.Name)
		}
	}
}

func TestModeVectors(t *testing.T) {
	v := loadVectors(t)
	pool := testdb.New(t)
	seed(t, pool, v)
	resolver := &Resolver{Pool: pool}
	for _, mode := range []Mode{Off, Observe, Enforce} {
		reader := sdkmetric.NewManualReader()
		gate := NewGate(resolver, mode, sdkmetric.NewMeterProvider(sdkmetric.WithReader(reader)))
		mux := http.NewServeMux()
		ok := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			_ = httpx.WriteJSON(w, map[string]bool{"ok": true})
		})
		for _, class := range []string{"public", "api", "person", "flow.host", "internal.outputs",
			"crud.agent", "crud.render", "crud.fleet-rotator", "transfer", "exchange"} {
			mux.Handle("/v/"+class, gate.Wrap(Route{Class: class, Template: "/v/" + class}, ok))
			mux.Handle("GET /v/"+class+"/img",
				gate.Wrap(Route{Class: class, Template: "/v/" + class + "/img", Scriptless: true}, ok))
			mux.Handle("GET /ws/"+class, gate.Wrap(Route{Class: class, Template: "/ws/" + class}, ok))
		}
		counts := map[[2]string]int64{}
		for _, c := range v.Modes {
			if c.Mode != string(mode) {
				continue
			}
			method := c.Method
			if method == "SOCKET" {
				method = http.MethodGet
			}
			req := httptest.NewRequest(method, c.Path, nil)
			req.Header = header(v.ModeCallers[c.Caller])
			if c.RequestHeader {
				req.Header.Set("X-ScadBuddy-Request", "1")
			}
			if c.Method == "SOCKET" {
				req.Header.Set("Connection", "Upgrade")
				req.Header.Set("Upgrade", "websocket")
			}
			rec := httptest.NewRecorder()
			mux.ServeHTTP(rec, req)
			name := c.Mode + " " + c.Caller + " " + c.Method + " " + c.Path
			if c.Accepted != nil {
				// A refused socket is uvicorn's 403 with no body: closed before it is accepted.
				want := http.StatusOK
				if !*c.Accepted {
					want = http.StatusForbidden
				}
				if rec.Code != want || (want == http.StatusForbidden && rec.Body.Len() != 0) {
					t.Errorf("%s: %d %q, want %d", name, rec.Code, rec.Body.String(), want)
				}
			} else {
				var body map[string]any
				if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
					t.Fatalf("%s: %v", name, err)
				}
				if rec.Code != c.Status || !reflect.DeepEqual(body, c.Body) {
					t.Errorf("%s: %d %v, want %d %v", name, rec.Code, body, c.Status, c.Body)
				}
				got := rec.Header().Get("WWW-Authenticate")
				if (c.WWWAuthenticate == nil) != (got == "") || (c.WWWAuthenticate != nil && *c.WWWAuthenticate != got) {
					t.Errorf("%s: WWW-Authenticate %q, want %v", name, got, c.WWWAuthenticate)
				}
			}
			now := collect(t, reader)
			var counted [][2]string
			for k, n := range now {
				if n != counts[k] {
					counted = append(counted, k)
				}
			}
			counts = now
			if len(counted) != len(c.Counted) || (len(counted) == 1 && counted[0] != c.Counted[0]) {
				t.Errorf("%s: counted %v, want %v", name, counted, c.Counted)
			}
		}
	}
}

// collect is the would-refuse counter's value by class and reason.
func collect(t *testing.T, reader sdkmetric.Reader) map[[2]string]int64 {
	t.Helper()
	var rm metricdata.ResourceMetrics
	if err := reader.Collect(context.Background(), &rm); err != nil {
		t.Fatal(err)
	}
	out := map[[2]string]int64{}
	for _, sm := range rm.ScopeMetrics {
		for _, m := range sm.Metrics {
			if m.Name != WouldRefuseMetric {
				continue
			}
			for _, p := range m.Data.(metricdata.Sum[int64]).DataPoints {
				class, _ := p.Attributes.Value("class")
				reason, _ := p.Attributes.Value("reason")
				if p.Attributes.Len() != 2 {
					t.Errorf("the counter has attributes %v", p.Attributes.ToSlice())
				}
				out[[2]string{class.AsString(), reason.AsString()}] = p.Value
			}
		}
	}
	return out
}

func TestWouldRefuseVectors(t *testing.T) {
	v := loadVectors(t)
	want := []string{"class", "caller", "method", "request_header", "header_needed", "reason"}
	if !slices.Equal(v.WouldRefuse.Columns, want) {
		t.Fatalf("columns %v", v.WouldRefuse.Columns)
	}
	callers := map[string]Caller{}
	for name, d := range v.WouldRefuse.Callers {
		c := Caller{Kind: Kind(d["kind"].(string))}
		c.Principal, _ = d["principal"].(string)
		c.SessionKind, _ = d["session_kind"].(string)
		c.TokenScope, _ = d["scope"].(string)
		c.TokenID, _ = d["token_id"].(string)
		if c.Kind == KindService {
			s := &Service{Caller: d["caller"].(string)}
			for _, x := range d["scopes"].([]any) {
				s.Scopes = append(s.Scopes, x.(string))
			}
			c.Service = s
		}
		if !reflect.DeepEqual(describe(c), d) {
			t.Fatalf("caller %s: %v, want %v", name, describe(c), d)
		}
		callers[name] = c
	}
	for _, row := range v.WouldRefuse.Rows {
		want, _ := row[5].(string)
		got := WouldRefuse(row[0].(string), callers[row[1].(string)], row[3].(bool), row[4].(bool), row[2].(string))
		if got != want {
			t.Errorf("%v: %q", row, got)
		}
	}
}

func TestRefusalVectors(t *testing.T) {
	v := loadVectors(t)
	for _, c := range v.Refusals {
		p := Refusal(c.Class, c.Reason, c.Bearer)
		rec := httptest.NewRecorder()
		httpx.WriteProblem(rec, httptest.NewRequest(http.MethodGet, "/x", nil), p)
		var body map[string]any
		if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
			t.Fatal(err)
		}
		want := map[string]any{"type": c.Type, "title": c.Title, "status": float64(c.Status),
			"detail": c.Detail, "instance": "/x"}
		for k, x := range c.Extensions {
			want[k] = x
		}
		name := c.Class + " " + c.Reason
		if rec.Code != c.Status || !reflect.DeepEqual(body, want) {
			t.Errorf("%s: %d %v, want %v", name, rec.Code, body, want)
		}
		got := rec.Header().Get("WWW-Authenticate")
		if (c.WWWAuthenticate == nil) != (got == "") || (c.WWWAuthenticate != nil && *c.WWWAuthenticate != got) {
			t.Errorf("%s: WWW-Authenticate %q, want %v", name, got, c.WWWAuthenticate)
		}
	}
}

func TestModeSettingVectors(t *testing.T) {
	for _, c := range loadVectors(t).ModeSettings {
		value, set := "", c.Env != nil
		if set {
			value = *c.Env
		}
		mode, err := ParseMode(value, set)
		switch {
		case c.Mode == nil && err == nil:
			t.Errorf("%q: %q, want an error", value, mode)
		case c.Mode != nil && (err != nil || string(mode) != *c.Mode):
			t.Errorf("%q: %q %v, want %q", value, mode, err, *c.Mode)
		}
	}
}

func TestTheMetricIsPythons(t *testing.T) {
	v := loadVectors(t)
	// Prometheus names an OpenTelemetry counter with "_" for "." and the suffix "_total".
	if got := strings.ReplaceAll(WouldRefuseMetric, ".", "_") + "_total"; got != v.Metric.Name {
		t.Errorf("metric %s, want %s", got, v.Metric.Name)
	}
	if !slices.Equal(v.Metric.Labels, []string{"class", "reason"}) {
		t.Errorf("labels %v", v.Metric.Labels)
	}
	if v.SessionCookie != SessionCookie {
		t.Errorf("cookie %s, want %s", SessionCookie, v.SessionCookie)
	}
	if time.Duration(v.IdleTimeout)*time.Second != IdleTimeout {
		t.Errorf("idle timeout %s", IdleTimeout)
	}
}

// TestNothingSecretIsLogged checks that a failed lookup logs no token, cookie or hash.
func TestNothingSecretIsLogged(t *testing.T) {
	v := loadVectors(t)
	pool := testdb.New(t)
	pool.Close() // every lookup now fails
	var logs bytes.Buffer
	restore := captureLogs(&logs)
	defer restore()
	r := &Resolver{Pool: pool}
	for _, h := range []map[string]string{
		{"cookie": SessionCookie + "=" + v.Sessions[0].Token},
		{"authorization": "Bearer " + v.Tokens[0].Token},
		{"authorization": "Bearer " + v.Credentials[0].Token},
	} {
		if c := r.Caller(context.Background(), header(h)); c.Kind != KindAnonymous {
			t.Errorf("a failed lookup is %v", c)
		}
	}
	if logs.Len() == 0 {
		t.Fatal("a failed lookup logged nothing")
	}
	text := logs.String()
	for _, secret := range []string{v.Sessions[0].Token, v.Tokens[0].Token, v.Credentials[0].Token, "sbpat_", "sbsvc_"} {
		if strings.Contains(text, secret) {
			t.Errorf("the log holds %q: %s", secret, text)
		}
	}
}

// SessionID is the hex SHA-256 of the session cookie Caller reads (Python's
// session.id_hash.hex()): Starlette's parse, a quoted value unquoted, the last of two kept.
// A person's caller names its session by the cookie the gate read, however it was written.
func TestAPersonsCallerNamesItsSession(t *testing.T) {
	v := loadVectors(t)
	pool := testdb.New(t)
	seed(t, pool, v)
	r := &Resolver{Pool: pool}
	for _, c := range v.Callers {
		got := r.Caller(context.Background(), header(c.Headers))
		if got.Kind != KindPerson {
			if got.SessionID != "" {
				t.Errorf("%s: a %s caller names session %q", c.Name, got.Kind, got.SessionID)
			}
			continue
		}
		token := parseCookies(header(c.Headers).Get("Cookie"))[SessionCookie]
		sum := sha256.Sum256([]byte(token))
		if want := hex.EncodeToString(sum[:]); got.SessionID != want {
			t.Errorf("%s: session %q, want %q", c.Name, got.SessionID, want)
		}
	}
}

func captureLogs(w *bytes.Buffer) (restore func()) {
	old := slog.Default()
	slog.SetDefault(slog.New(slog.NewTextHandler(w, nil)))
	return func() { slog.SetDefault(old) }
}
