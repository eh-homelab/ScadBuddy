package fleetchat

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"slices"
	"strings"
	"sync"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

const inboxPrefix = "fleet://inbox/"

// NewServer returns the fleet's MCP server: the fleet_* tools and the inbox
// resource template fleet://inbox/{session_id}, which clients may subscribe
// to. It subscribes to the Bus with ctx, and stops listening when ctx ends.
func NewServer(ctx context.Context, svc *Service, name, version string) *mcp.Server {
	srv, _ := newServer(ctx, svc, name, version)
	return srv
}

// NewHandler serves NewServer's server over streamable HTTP, one server for
// every session, so a Bus event reaches each subscribed session.
func NewHandler(ctx context.Context, svc *Service, name, version string) http.Handler {
	srv := NewServer(ctx, svc, name, version)
	return mcp.NewStreamableHTTPHandler(func(*http.Request) *mcp.Server { return srv }, &mcp.StreamableHTTPOptions{
		// The largest text, with room for its JSON escaping and the envelope.
		// Text that is mostly control bytes (6x when escaped), or HTML-heavy
		// text from an encoder that escapes <>&, can get HTTP 413 before
		// ErrTooLong.
		MaxRequestBodyBytes: 2*int64(svc.cfg.MaxText) + 1<<20,
		SessionTimeout:      svc.cfg.SessionTimeout,
	})
}

func newServer(ctx context.Context, svc *Service, name, version string) (*mcp.Server, *subscriptions) {
	subs := &subscriptions{byURI: map[string]map[*mcp.ServerSession]struct{}{}, watched: map[*mcp.ServerSession]bool{}}
	srv := mcp.NewServer(&mcp.Implementation{Name: name, Version: version}, &mcp.ServerOptions{
		SubscribeHandler: func(_ context.Context, req *mcp.SubscribeRequest) error {
			if _, ok := inboxSession(req.Params.URI); !ok {
				return fmt.Errorf("only %s<session_id> can be subscribed to, with a canonical lower-case UUID: %q", inboxPrefix, req.Params.URI)
			}
			subs.add(req.Params.URI, req.Session)
			return nil
		},
		UnsubscribeHandler: func(_ context.Context, req *mcp.UnsubscribeRequest) error {
			subs.remove(req.Params.URI, req.Session)
			return nil
		},
	})
	addTools(srv, svc)
	srv.AddResourceTemplate(&mcp.ResourceTemplate{
		Name:        "fleet_inbox",
		URITemplate: inboxPrefix + "{session_id}",
		Description: "This agent's unread messages; reading does not move the cursor. Subscribe for updates.",
		MIMEType:    "text/plain",
	}, func(ctx context.Context, req *mcp.ReadResourceRequest) (*mcp.ReadResourceResult, error) {
		sid, ok := inboxSession(req.Params.URI)
		if !ok {
			return nil, mcp.ResourceNotFoundError(req.Params.URI)
		}
		msgs, more, err := svc.Inbox(ctx, sid, 0, true)
		if err != nil {
			return nil, err
		}
		text, err := inboxText(msgs, more)
		if err != nil {
			return nil, err
		}
		return &mcp.ReadResourceResult{Contents: []*mcp.ResourceContents{{URI: req.Params.URI, MIMEType: "text/plain", Text: text}}}, nil
	})

	go func() {
		err := svc.bus.Subscribe(ctx, func(ev Event) {
			var uris []string
			if ev.Resync {
				uris = subs.uris()
			} else {
				for _, sid := range ev.SessionIDs {
					if uri := inboxPrefix + sid; subs.has(uri) {
						uris = append(uris, uri)
					}
				}
			}
			for _, uri := range uris {
				if err := srv.ResourceUpdated(ctx, &mcp.ResourceUpdatedNotificationParams{URI: uri}); err != nil {
					slog.WarnContext(ctx, "fleetchat: resource update failed", "uri", uri, "err", err)
				}
			}
		})
		if err != nil && ctx.Err() == nil {
			slog.ErrorContext(ctx, "fleetchat: bus subscription ended; inbox updates stop", "err", err)
		}
	}()
	return srv, subs
}

// inboxSession returns the session id of an inbox URI, or false when the URI
// is not one with a canonical UUID.
func inboxSession(uri string) (string, bool) {
	sid, ok := strings.CutPrefix(uri, inboxPrefix)
	if !ok {
		return "", false
	}
	if _, err := Handle(sid); err != nil {
		return "", false
	}
	return sid, true
}

// subscriptions tracks which MCP sessions watch which inbox URIs, so that a
// Resync can notify every watched inbox.
type subscriptions struct {
	mu      sync.Mutex
	byURI   map[string]map[*mcp.ServerSession]struct{}
	watched map[*mcp.ServerSession]bool
}

func (s *subscriptions) add(uri string, ss *mcp.ServerSession) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.byURI[uri] == nil {
		s.byURI[uri] = map[*mcp.ServerSession]struct{}{}
	}
	s.byURI[uri][ss] = struct{}{}
	if !s.watched[ss] {
		s.watched[ss] = true
		go func() {
			_ = ss.Wait()
			s.drop(ss)
		}()
	}
}

func (s *subscriptions) remove(uri string, ss *mcp.ServerSession) {
	s.mu.Lock()
	defer s.mu.Unlock()
	delete(s.byURI[uri], ss)
	if len(s.byURI[uri]) == 0 {
		delete(s.byURI, uri)
	}
}

// drop forgets a closed session's subscriptions.
func (s *subscriptions) drop(ss *mcp.ServerSession) {
	s.mu.Lock()
	defer s.mu.Unlock()
	delete(s.watched, ss)
	for uri, m := range s.byURI {
		delete(m, ss)
		if len(m) == 0 {
			delete(s.byURI, uri)
		}
	}
}

func (s *subscriptions) has(uri string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return len(s.byURI[uri]) > 0
}

func (s *subscriptions) uris() []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]string, 0, len(s.byURI))
	for uri := range s.byURI {
		out = append(out, uri)
	}
	slices.Sort(out)
	return out
}

type sessionArgs struct {
	SessionID string `json:"session_id" jsonschema:"Your Claude Code session id (a lower-case UUID). Your handle is its first 8 characters."`
}

type channelArgs struct {
	SessionID string `json:"session_id" jsonschema:"Your Claude Code session id (a lower-case UUID). Your handle is its first 8 characters."`
	Channel   string `json:"channel" jsonschema:"The channel name, ^[a-z0-9-]{1,40}$."`
}

type sendArgs struct {
	SessionID string `json:"session_id" jsonschema:"Your Claude Code session id (a lower-case UUID). Your handle is its first 8 characters."`
	Channel   string `json:"channel,omitempty" jsonschema:"Post to this channel (you must be a member). Give channel or agent, not both."`
	Agent     string `json:"agent,omitempty" jsonschema:"Send a direct message to the agent with this 8-character handle. Give channel or agent, not both. A virtual sender's name (such as rotator) takes no messages."`
	Text      string `json:"text" jsonschema:"The message text."`
}

type inboxArgs struct {
	SessionID string `json:"session_id" jsonschema:"Your Claude Code session id (a lower-case UUID). Your handle is its first 8 characters."`
	Limit     int    `json:"limit,omitempty" jsonschema:"The most messages to return (default 50, at most 200)."`
	Peek      bool   `json:"peek,omitempty" jsonschema:"Read without moving your cursor."`
}

type rewindArgs struct {
	SessionID string `json:"session_id" jsonschema:"Your Claude Code session id (a lower-case UUID). Your handle is its first 8 characters."`
	Seq       int64  `json:"seq" jsonschema:"Set your cursor back to this seq; messages after it are unread again (0 for all)."`
}

func addTools(srv *mcp.Server, svc *Service) {
	mcp.AddTool(srv, &mcp.Tool{Name: "fleet_join", Description: "Join a channel, creating it if it does not exist."},
		func(ctx context.Context, _ *mcp.CallToolRequest, in channelArgs) (*mcp.CallToolResult, any, error) {
			if err := svc.Join(ctx, in.SessionID, in.Channel); err != nil {
				return nil, nil, err
			}
			return jsonResult(map[string]any{"joined": in.Channel})
		})
	mcp.AddTool(srv, &mcp.Tool{Name: "fleet_leave", Description: "Leave a channel."},
		func(ctx context.Context, _ *mcp.CallToolRequest, in channelArgs) (*mcp.CallToolResult, any, error) {
			if err := svc.Leave(ctx, in.SessionID, in.Channel); err != nil {
				return nil, nil, err
			}
			return jsonResult(map[string]any{"left": in.Channel})
		})
	mcp.AddTool(srv, &mcp.Tool{Name: "fleet_send", Description: "Send a direct message to an agent (agent, its handle) or post to a channel (channel). Returns the message's seq."},
		func(ctx context.Context, _ *mcp.CallToolRequest, in sendArgs) (*mcp.CallToolResult, any, error) {
			seq, err := svc.Send(ctx, in.SessionID, Target{Channel: in.Channel, Agent: in.Agent}, in.Text)
			if err != nil {
				return nil, nil, err
			}
			return jsonResult(map[string]any{"seq": seq})
		})
	mcp.AddTool(srv, &mcp.Tool{Name: "fleet_inbox", Description: "Your messages after your cursor, oldest first, a page at a time. Moves the cursor past them unless peek."},
		func(ctx context.Context, _ *mcp.CallToolRequest, in inboxArgs) (*mcp.CallToolResult, any, error) {
			msgs, more, err := svc.Inbox(ctx, in.SessionID, in.Limit, in.Peek)
			if err != nil {
				return nil, nil, err
			}
			text, err := inboxText(msgs, more)
			if err != nil {
				return nil, nil, err
			}
			return &mcp.CallToolResult{Content: []mcp.Content{&mcp.TextContent{Text: text}}}, nil, nil
		})
	mcp.AddTool(srv, &mcp.Tool{Name: "fleet_rewind", Description: "Set your cursor back to a seq (0 up to your cursor), so later messages are unread again."},
		func(ctx context.Context, _ *mcp.CallToolRequest, in rewindArgs) (*mcp.CallToolResult, any, error) {
			if err := svc.Rewind(ctx, in.SessionID, in.Seq); err != nil {
				return nil, nil, err
			}
			return jsonResult(map[string]any{"cursor": in.Seq})
		})
	mcp.AddTool(srv, &mcp.Tool{Name: "fleet_channels", Description: "The channels, their members' handles, and your unread count in each."},
		func(ctx context.Context, _ *mcp.CallToolRequest, in sessionArgs) (*mcp.CallToolResult, any, error) {
			chans, err := svc.Channels(ctx, in.SessionID)
			if err != nil {
				return nil, nil, err
			}
			if chans == nil {
				chans = []ChannelInfo{}
			}
			return jsonResult(map[string]any{"channels": chans})
		})
	mcp.AddTool(srv, &mcp.Tool{Name: "fleet_agents", Description: "The agents of the fleet: handle and when each was last seen."},
		func(ctx context.Context, _ *mcp.CallToolRequest, in sessionArgs) (*mcp.CallToolResult, any, error) {
			agents, err := svc.Agents(ctx, in.SessionID)
			if err != nil {
				return nil, nil, err
			}
			if agents == nil {
				agents = []AgentInfo{}
			}
			return jsonResult(map[string]any{"agents": agents})
		})
}

func jsonResult(v any) (*mcp.CallToolResult, any, error) {
	b, err := json.Marshal(v)
	if err != nil {
		return nil, nil, err
	}
	return &mcp.CallToolResult{Content: []mcp.Content{&mcp.TextContent{Text: string(b)}}}, nil, nil
}

// inboxText is the untrusted line, then the page as JSON.
func inboxText(msgs []Message, more bool) (string, error) {
	if msgs == nil {
		msgs = []Message{}
	}
	b, err := json.Marshal(map[string]any{"messages": msgs, "more": more})
	if err != nil {
		return "", err
	}
	return UntrustedLine + "\n" + string(b), nil
}
