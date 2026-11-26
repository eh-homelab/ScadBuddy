package sbfleet

import (
	"context"
	"errors"
	"net/http/httptest"
	"testing"
	"time"
)

func newRegistry(t *testing.T, status int, body string) (*Registry, *fakeCore) {
	t.Helper()
	f := &fakeCore{status: status, body: body}
	srv := httptest.NewServer(f)
	t.Cleanup(srv.Close)
	return NewRegistry(srv.URL+"/", func() (string, error) { return "tok", nil }), f
}

const hostJSON = `{"pid":4242,"proc_start":"97360477",
	"cwd":"/home/u/repo","config_dir":"/home/u/.claude","transcript_path":null,"git_remote":"git@github.com:o/r.git",
	"git_branch":"main","worktree_root":"/home/u/repo","git_head":null,"cc_version":"2.1.332","kind":"interactive",
	"entrypoint":"cli","name":null,"status":"busy","credential_id":"c1",
	"credential":{"id":"c1","name":"Main","kind":"claude_oauth_token","last4":"wxyz"},
	"account_uuid":null,"last_rate_limit_at":"2026-11-26T10:00:00Z","last_reset_at":null,
	"last_seen":"2026-11-26T12:00:00.5Z","previous_session_id":"` + other + `",
	"created_at":"2026-11-26T09:00:00Z","updated_at":"2026-11-26T12:00:00Z",
	"machine":{"id":"m1","hostname":"devbox","os":"linux","os_user":"u","service_version":"0.1.0",
		"first_seen":"2026-11-20T00:00:00Z","last_seen":"2026-11-26T12:00:00Z",
		"created_at":"2026-11-20T00:00:00Z","updated_at":"2026-11-26T12:00:00Z"}}`

// sessionJSON is a session on two machines: devbox seen last, then the laptop.
const sessionJSON = `{"session_id":"` + sid + `","handle":"0a1b2c3d","last_seen":"2026-11-26T12:00:00.5Z",
	"hosts":[` + hostJSON + `,{"pid":7,"proc_start":"1","cwd":"/home/u/laptop","config_dir":"/home/u/.claude",
	"transcript_path":null,"git_remote":null,"git_branch":null,"worktree_root":null,"git_head":null,
	"cc_version":null,"kind":null,"entrypoint":null,"name":null,"status":null,"credential_id":null,
	"credential":null,"account_uuid":null,"last_rate_limit_at":null,"last_reset_at":null,
	"last_seen":"2026-11-26T11:00:00Z","previous_session_id":null,
	"created_at":"2026-11-26T09:00:00Z","updated_at":"2026-11-26T11:00:00Z",
	"machine":{"id":"m2","hostname":"laptop","os":"linux","os_user":"u","service_version":"0.1.0",
		"first_seen":"2026-11-20T00:00:00Z","last_seen":"2026-11-26T11:00:00Z",
		"created_at":"2026-11-20T00:00:00Z","updated_at":"2026-11-26T11:00:00Z"}}]}`

func TestRegistrySession(t *testing.T) {
	r, f := newRegistry(t, 200, sessionJSON)
	s, err := r.Session(context.Background(), sid)
	if err != nil {
		t.Fatal(err)
	}
	got := f.last(t)
	if got.Method != "GET" || got.Path != base+"/sessions/"+sid || got.Auth != "Bearer tok" {
		t.Fatalf("core saw %+v", got)
	}
	if s.SessionID != sid || s.Handle != "0a1b2c3d" || len(s.Hosts) != 2 ||
		!s.LastSeen.Equal(time.Date(2026, 11, 26, 12, 0, 0, 500_000_000, time.UTC)) {
		t.Fatalf("read %+v", s)
	}
	h := s.Hosts[0]
	if h.PID != 4242 || h.Machine.Hostname != "devbox" || h.PreviousSessionID == nil || *h.PreviousSessionID != other {
		t.Fatalf("host %+v", h)
	}
	if h.Credential == nil || h.Credential.Last4 != "wxyz" || h.Credential.Name == nil || *h.Credential.Name != "Main" {
		t.Fatalf("credential %+v", h.Credential)
	}
	if h.LastRateLimitAt == nil || !h.LastRateLimitAt.Equal(time.Date(2026, 11, 26, 10, 0, 0, 0, time.UTC)) {
		t.Fatalf("last_rate_limit_at %v", h.LastRateLimitAt)
	}
	if h.LastResetAt != nil || h.TranscriptPath != nil {
		t.Fatalf("a null read as a value: %+v", h)
	}
	laptop := s.Hosts[1]
	if laptop.Machine.ID != "m2" || laptop.PID != 7 || laptop.Cwd != "/home/u/laptop" || laptop.Credential != nil {
		t.Fatalf("second host %+v", laptop)
	}
}

func TestRegistrySessions(t *testing.T) {
	r, f := newRegistry(t, 200, `{"sessions":[`+sessionJSON+`],"more":true}`)
	list, more, err := r.Sessions(context.Background(), 2)
	if err != nil {
		t.Fatal(err)
	}
	got := f.last(t)
	if got.Path != base+"/sessions" || got.Query != "limit=2" {
		t.Fatalf("core saw %+v", got)
	}
	if len(list) != 1 || !more || list[0].SessionID != sid || len(list[0].Hosts) != 2 {
		t.Fatalf("read %+v, %v", list, more)
	}
}

func TestRegistryErrors(t *testing.T) {
	r, _ := newRegistry(t, 404, problem(404, "unknown-session"))
	if _, err := r.Session(context.Background(), sid); !errors.Is(err, ErrUnknownSession) {
		t.Fatalf("got %v", err)
	}
	r, _ = newRegistry(t, 403, `{"detail":"These routes belong to another service.","reason":"wrong_caller"}`)
	if _, _, err := r.Sessions(context.Background(), 0); err == nil || errors.Is(err, ErrUnknownSession) {
		t.Fatalf("got %v", err)
	}
}
