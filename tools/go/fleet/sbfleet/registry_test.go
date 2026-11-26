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

const sessionJSON = `{"session_id":"` + sid + `","handle":"0a1b2c3d","pid":4242,"proc_start":"97360477",
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
	if s.SessionID != sid || s.PID != 4242 || s.Machine.Hostname != "devbox" || s.PreviousSessionID == nil ||
		*s.PreviousSessionID != other {
		t.Fatalf("read %+v", s)
	}
	if s.Credential == nil || s.Credential.Last4 != "wxyz" || s.Credential.Name == nil || *s.Credential.Name != "Main" {
		t.Fatalf("credential %+v", s.Credential)
	}
	if s.LastRateLimitAt == nil || !s.LastRateLimitAt.Equal(time.Date(2026, 11, 26, 10, 0, 0, 0, time.UTC)) {
		t.Fatalf("last_rate_limit_at %v", s.LastRateLimitAt)
	}
	if s.LastResetAt != nil || s.TranscriptPath != nil {
		t.Fatalf("a null read as a value: %+v", s)
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
	if len(list) != 1 || !more || list[0].SessionID != sid {
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
