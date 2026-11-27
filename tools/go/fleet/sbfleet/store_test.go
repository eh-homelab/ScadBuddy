package sbfleet

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/eh-homelab/ScadBuddy/tools/go/fleetchat"
)

const (
	sid   = "0a1b2c3d-0000-4000-8000-000000000001"
	other = "9f8e7d6c-0000-4000-8000-000000000002"
)

// request is what the fake core saw.
type request struct {
	Method, Path, Query, Auth, Body string
}

// fakeCore answers every request with status and body, and records it.
type fakeCore struct {
	mu     sync.Mutex
	seen   []request
	status int
	body   string
}

func (f *fakeCore) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	b, _ := io.ReadAll(r.Body)
	f.mu.Lock()
	f.seen = append(f.seen, request{r.Method, r.URL.Path, r.URL.RawQuery, r.Header.Get("Authorization"), string(b)})
	status, body := f.status, f.body
	f.mu.Unlock()
	if status >= 400 {
		w.Header().Set("Content-Type", "application/problem+json")
	} else {
		w.Header().Set("Content-Type", "application/json")
	}
	w.WriteHeader(status)
	_, _ = io.WriteString(w, body)
}

func (f *fakeCore) last(t *testing.T) request {
	t.Helper()
	f.mu.Lock()
	defer f.mu.Unlock()
	if len(f.seen) == 0 {
		t.Fatal("core saw no request")
	}
	return f.seen[len(f.seen)-1]
}

func newStore(t *testing.T, status int, body string) (fleetchat.Store, *fakeCore) {
	t.Helper()
	f := &fakeCore{status: status, body: body}
	srv := httptest.NewServer(f)
	t.Cleanup(srv.Close)
	return NewStore(srv.URL+"/", func() (string, error) { return "tok", nil }), f
}

const base = "/api/v1/internal/crud/fleet"

func TestStoreRequests(t *testing.T) {
	ctx := context.Background()
	cases := []struct {
		name   string
		body   string
		call   func(s fleetchat.Store) error
		method string
		path   string
		query  string
		sent   string
	}{
		{"touch", `{"handle":"0a1b2c3d"}`, func(s fleetchat.Store) error { return s.Touch(ctx, sid, "0a1b2c3d") },
			"POST", base + "/agents/" + sid + "/touch", "", ""},
		{"join", `{"joined":true}`, func(s fleetchat.Store) error { return s.Join(ctx, sid, "ops") },
			"PUT", base + "/channels/ops/members/" + sid, "", ""},
		{"leave", `{"left":true}`, func(s fleetchat.Store) error { return s.Leave(ctx, sid, "ops") },
			"DELETE", base + "/channels/ops/members/" + sid, "", ""},
		{"rewind", `{"seq":7}`, func(s fleetchat.Store) error { return s.Rewind(ctx, sid, 7) },
			"PUT", base + "/agents/" + sid + "/cursor", "", `{"seq":7}`},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			s, f := newStore(t, 200, c.body)
			if err := c.call(s); err != nil {
				t.Fatal(err)
			}
			got := f.last(t)
			want := request{c.method, c.path, c.query, "Bearer tok", c.sent}
			if got != want {
				t.Errorf("got %+v, want %+v", got, want)
			}
		})
	}
}

func TestStoreAgents(t *testing.T) {
	s, f := newStore(t, 200, `{"agents":[{"handle":"0a1b2c3d","last_seen":"2026-11-25T10:00:00.123456Z"}]}`)
	got, err := s.Agents(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	want := time.Date(2026, 11, 25, 10, 0, 0, 123456000, time.UTC)
	if len(got) != 1 || got[0].Handle != "0a1b2c3d" || !got[0].LastSeen.Equal(want) {
		t.Errorf("agents: %+v", got)
	}
	if r := f.last(t); r.Method != "GET" || r.Path != base+"/agents" {
		t.Errorf("request %+v", r)
	}
}

func TestStoreChannels(t *testing.T) {
	s, f := newStore(t, 200, `{"channels":[{"name":"ops","members":["0a1b2c3d"],"unread":3}]}`)
	got, err := s.Channels(context.Background(), sid)
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 1 || got[0].Name != "ops" || got[0].Unread != 3 || len(got[0].Members) != 1 {
		t.Errorf("channels: %+v", got)
	}
	if r := f.last(t); r.Method != "GET" || r.Path != base+"/agents/"+sid+"/channels" {
		t.Errorf("request %+v", r)
	}
}

func TestStoreSend(t *testing.T) {
	for _, c := range []struct {
		name   string
		target fleetchat.Target
		key    string
	}{
		{"dm", fleetchat.Target{Agent: "9f8e7d6c"}, "agent"},
		{"channel", fleetchat.Target{Channel: "ops"}, "channel"},
	} {
		t.Run(c.name, func(t *testing.T) {
			s, f := newStore(t, 200, `{"seq":12,"recipients":["`+sid+`","`+other+`"]}`)
			seq, recipients, err := s.Send(context.Background(), sid, c.target, "hi <there>")
			if err != nil {
				t.Fatal(err)
			}
			if seq != 12 || len(recipients) != 2 || recipients[1] != other {
				t.Errorf("seq %d recipients %v", seq, recipients)
			}
			r := f.last(t)
			if r.Method != "POST" || r.Path != base+"/messages" {
				t.Errorf("request %+v", r)
			}
			var body map[string]any
			if err := json.Unmarshal([]byte(r.Body), &body); err != nil {
				t.Fatal(err)
			}
			want := map[string]any{"from": sid, c.key: c.target.Agent + c.target.Channel, "text": "hi <there>"}
			if len(body) != len(want) {
				t.Errorf("body %v, want %v", body, want)
			}
			for k, v := range want {
				if body[k] != v {
					t.Errorf("body %v, want %v", body, want)
				}
			}
		})
	}
}

func TestStoreInbox(t *testing.T) {
	s, f := newStore(t, 200, `{"messages":[{"seq":3,"from":"0a1b2c3d","channel":"ops","text":"x","sent_at":"2026-11-25T10:00:00Z"},
		{"seq":4,"from":"9f8e7d6c","to":"0a1b2c3d","text":"y","sent_at":"2026-11-25T10:00:01Z"}],"more":true}`)
	msgs, more, err := s.Inbox(context.Background(), sid, 2, true)
	if err != nil {
		t.Fatal(err)
	}
	if !more || len(msgs) != 2 || msgs[0].Channel != "ops" || msgs[1].To != "0a1b2c3d" || msgs[1].Seq != 4 {
		t.Errorf("inbox %+v more %v", msgs, more)
	}
	r := f.last(t)
	if r.Method != "GET" || r.Path != base+"/agents/"+sid+"/inbox" || r.Query != "limit=2&peek=true" {
		t.Errorf("request %+v", r)
	}
	if _, _, err := s.Inbox(context.Background(), sid, 50, false); err != nil {
		t.Fatal(err)
	}
	if r := f.last(t); r.Query != "limit=50&peek=false" {
		t.Errorf("query %q", r.Query)
	}
}

func problem(status int, reason string) string {
	return `{"type":"about:blank","title":"x","status":` + strconv.Itoa(status) + `,"detail":"core says no","reason":"` + reason + `"}`
}

func TestStoreErrors(t *testing.T) {
	ctx := context.Background()
	cases := []struct {
		name   string
		status int
		reason string
		want   error
	}{
		{"unknown agent", 404, "unknown-agent", fleetchat.ErrUnknownAgent},
		{"not member", 403, "not-member", fleetchat.ErrNotMember},
		{"unknown channel", 404, "unknown-channel", fleetchat.ErrUnknownChannel},
		{"handle taken", 409, "handle-taken", ErrHandleTaken},
	}
	t.Run("text too long", func(t *testing.T) {
		s, _ := newStore(t, 422, `{"detail":"text is longer than 16777216 bytes","reason":"text-too-long"}`)
		_, _, err := s.Send(ctx, sid, fleetchat.Target{Channel: "ops"}, "x")
		if !errors.Is(err, fleetchat.ErrTooLong) || !strings.Contains(err.Error(), "16777216 bytes") {
			t.Fatalf("err %v, want ErrTooLong naming core's limit", err)
		}
	})
	t.Run("virtual sender", func(t *testing.T) {
		reject := `"rotator" is the login rotator, a virtual sender.`
		s, _ := newStore(t, 422, `{"detail":"\"rotator\" is the login rotator, a virtual sender.","reason":"virtual-sender"}`)
		_, _, err := s.Send(ctx, sid, fleetchat.Target{Agent: "rotator"}, "x")
		if !errors.Is(err, fleetchat.ErrVirtualSender) || err.Error() != reject {
			t.Fatalf("err %v, want ErrVirtualSender whose text is the reject message", err)
		}
	})
	t.Run("seq ahead", func(t *testing.T) {
		s, _ := newStore(t, 422, problem(422, "seq-ahead"))
		if err := s.Rewind(ctx, sid, 99); !errors.Is(err, fleetchat.ErrBadSeq) {
			t.Fatalf("err %v, want ErrBadSeq", err)
		}
	})
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			s, _ := newStore(t, c.status, problem(c.status, c.reason))
			_, _, err := s.Send(ctx, sid, fleetchat.Target{Channel: "ops"}, "x")
			if !errors.Is(err, c.want) {
				t.Fatalf("err %v, want %v", err, c.want)
			}
			if !strings.Contains(err.Error(), "core says no") {
				t.Errorf("err %q does not carry core's detail", err)
			}
		})
	}
	t.Run("leave not member", func(t *testing.T) {
		s, _ := newStore(t, 403, problem(403, "not-member"))
		if err := s.Leave(ctx, sid, "ops"); !errors.Is(err, fleetchat.ErrNotMember) {
			t.Fatalf("err %v", err)
		}
	})
	t.Run("other status", func(t *testing.T) {
		s, _ := newStore(t, 500, `{"detail":"boom"}`)
		err := s.Touch(ctx, sid, "0a1b2c3d")
		if err == nil || errors.Is(err, fleetchat.ErrUnknownAgent) || errors.Is(err, fleetchat.ErrNotMember) {
			t.Fatalf("err %v", err)
		}
		if !strings.Contains(err.Error(), "500") {
			t.Errorf("err %q names no status", err)
		}
	})
	t.Run("a 403 of the guard is no ErrNotMember", func(t *testing.T) {
		s, _ := newStore(t, 403, `{"detail":"this route is for another caller"}`)
		if err := s.Join(ctx, sid, "ops"); err == nil || errors.Is(err, fleetchat.ErrNotMember) {
			t.Fatalf("err %v", err)
		}
	})
	t.Run("credential read fails", func(t *testing.T) {
		f := &fakeCore{status: 200, body: `{}`}
		srv := httptest.NewServer(f)
		t.Cleanup(srv.Close)
		s := NewStore(srv.URL, func() (string, error) { return "", errors.New("no file") })
		if err := s.Touch(ctx, sid, "0a1b2c3d"); err == nil || !strings.Contains(err.Error(), "no file") {
			t.Fatalf("err %v", err)
		}
		if len(f.seen) != 0 {
			t.Errorf("core saw %d requests without a credential", len(f.seen))
		}
	})
}
