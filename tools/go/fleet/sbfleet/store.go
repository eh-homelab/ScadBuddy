// Package sbfleet is ScadBuddy's host for fleetchat: a Store over core's fleet CRUD
// routes and a Bus over the API's fleet change stream, both with the `fleet` service
// credential (spec 2026-11-25-agent-fleet-messaging-design §5). It imports nothing from
// core; it reaches it only over HTTP.
package sbfleet

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"

	"github.com/eh-homelab/ScadBuddy/tools/go/fleetchat"
)

// path is the fleet caller's routes, under core's URL.
const path = "/api/v1/internal/crud/fleet"

// requestTimeout bounds every call but the change stream.
const requestTimeout = 30 * time.Second

// ErrHandleTaken says that another session already has the handle: two session ids
// share their first 8 hex characters (core's 409 handle-taken).
var ErrHandleTaken = errors.New("another session has this session's handle")

// reasons map core's problem reasons to the errors fleetchat knows.
var reasons = map[string]error{
	"unknown-agent": fleetchat.ErrUnknownAgent,
	"not-member":    fleetchat.ErrNotMember,
	"handle-taken":  ErrHandleTaken,
	"seq-ahead":     fleetchat.ErrBadSeq,
}

type client struct {
	base       string
	token      func() (string, error)
	invalidate func(string)
	http       *http.Client
}

// Option configures NewStore and NewBus.
type Option func(*client)

// OnUnauthorized names the function told of a credential core refused (401), such as
// (*Exchange).Invalidate. With it, a request refused with 401 is sent once more with the
// credential token returns next. Core's CRUD guard refuses before a route runs, so the
// route has not run twice.
func OnUnauthorized(invalidate func(token string)) Option {
	return func(c *client) { c.invalidate = invalidate }
}

func newClient(baseURL string, token func() (string, error), timeout time.Duration, opts []Option) *client {
	c := &client{base: strings.TrimRight(baseURL, "/") + path, token: token, http: &http.Client{Timeout: timeout}}
	for _, o := range opts {
		o(c)
	}
	return c
}

// send sends a request built with the credential token returns now, and, when core
// refuses it (401) and OnUnauthorized was given, tells that function and sends it once
// more. body is sent as is each time; header sets the request's other headers.
func (c *client) send(ctx context.Context, method, route string, body []byte, header http.Header) (*http.Response, error) {
	for attempt := 0; ; attempt++ {
		token, err := c.token()
		if err != nil {
			return nil, fmt.Errorf("reading the fleet credential: %w", err)
		}
		var reader io.Reader
		if body != nil {
			reader = bytes.NewReader(body)
		}
		req, err := http.NewRequestWithContext(ctx, method, c.base+route, reader)
		if err != nil {
			return nil, err
		}
		for k, v := range header {
			req.Header[k] = v
		}
		req.Header.Set("Authorization", "Bearer "+token)
		res, err := c.http.Do(req)
		if err != nil {
			return nil, err
		}
		if res.StatusCode != http.StatusUnauthorized || c.invalidate == nil || attempt > 0 {
			return res, nil
		}
		_, _ = io.Copy(io.Discard, io.LimitReader(res.Body, 64<<10))
		res.Body.Close()
		c.invalidate(token)
	}
}

// do sends body (JSON, or none when nil) and decodes a 2xx answer into out (when not nil).
// A problem answer is an error wrapping the fleetchat error its reason names.
func (c *client) do(ctx context.Context, method, route string, body, out any) error {
	var encoded []byte
	header := http.Header{"Accept": {"application/json"}}
	if body != nil {
		// No HTML escapes: core caps a send's body at about twice the largest text.
		var b bytes.Buffer
		enc := json.NewEncoder(&b)
		enc.SetEscapeHTML(false)
		if err := enc.Encode(body); err != nil {
			return err
		}
		encoded = bytes.TrimSuffix(b.Bytes(), []byte("\n"))
		header.Set("Content-Type", "application/json")
	}
	res, err := c.send(ctx, method, route, encoded, header)
	if err != nil {
		return err
	}
	defer res.Body.Close()
	if res.StatusCode/100 != 2 {
		return problemOf(res)
	}
	if out == nil {
		_, _ = io.Copy(io.Discard, io.LimitReader(res.Body, 1<<20))
		return nil
	}
	if err := json.NewDecoder(res.Body).Decode(out); err != nil {
		return fmt.Errorf("%s %s: reading core's answer: %w", method, route, err)
	}
	return nil
}

func problemOf(res *http.Response) error {
	var p struct {
		Detail string `json:"detail"`
		Reason string `json:"reason"`
	}
	_ = json.NewDecoder(io.LimitReader(res.Body, 64<<10)).Decode(&p)
	if known, ok := reasons[p.Reason]; ok {
		return fmt.Errorf("%w: %s", known, p.Detail)
	}
	if p.Detail != "" {
		return fmt.Errorf("core answered %d: %s", res.StatusCode, p.Detail)
	}
	return fmt.Errorf("core answered %d", res.StatusCode)
}

type store struct{ c *client }

// NewStore returns a fleetchat.Store over core's fleet routes at baseURL (core's URL,
// without the route prefix). token returns the `fleet` credential and is called for
// every request. OnUnauthorized renews a refused credential.
func NewStore(baseURL string, token func() (string, error), opts ...Option) fleetchat.Store {
	return &store{c: newClient(baseURL, token, requestTimeout, opts)}
}

func agentPath(sessionID string) string { return "/agents/" + url.PathEscape(sessionID) }

func memberPath(sessionID, channel string) string {
	return "/channels/" + url.PathEscape(channel) + "/members/" + url.PathEscape(sessionID)
}

// Touch's handle is derived by core from the session id; the argument is not sent.
func (s *store) Touch(ctx context.Context, sessionID, _ string) error {
	return s.c.do(ctx, http.MethodPost, agentPath(sessionID)+"/touch", nil, nil)
}

func (s *store) Agents(ctx context.Context) ([]fleetchat.AgentInfo, error) {
	var out struct {
		Agents []fleetchat.AgentInfo `json:"agents"`
	}
	err := s.c.do(ctx, http.MethodGet, "/agents", nil, &out)
	return out.Agents, err
}

func (s *store) Join(ctx context.Context, sessionID, channel string) error {
	return s.c.do(ctx, http.MethodPut, memberPath(sessionID, channel), nil, nil)
}

func (s *store) Leave(ctx context.Context, sessionID, channel string) error {
	return s.c.do(ctx, http.MethodDelete, memberPath(sessionID, channel), nil, nil)
}

func (s *store) Channels(ctx context.Context, sessionID string) ([]fleetchat.ChannelInfo, error) {
	var out struct {
		Channels []fleetchat.ChannelInfo `json:"channels"`
	}
	err := s.c.do(ctx, http.MethodGet, agentPath(sessionID)+"/channels", nil, &out)
	return out.Channels, err
}

func (s *store) Send(ctx context.Context, sessionID string, t fleetchat.Target, text string) (int64, []string, error) {
	body := map[string]string{"from": sessionID, "text": text}
	if t.Channel != "" {
		body["channel"] = t.Channel
	} else {
		body["agent"] = t.Agent
	}
	var out struct {
		Seq        int64    `json:"seq"`
		Recipients []string `json:"recipients"`
	}
	if err := s.c.do(ctx, http.MethodPost, "/messages", body, &out); err != nil {
		return 0, nil, err
	}
	return out.Seq, out.Recipients, nil
}

func (s *store) Inbox(ctx context.Context, sessionID string, limit int, peek bool) ([]fleetchat.Message, bool, error) {
	q := "?limit=" + strconv.Itoa(limit) + "&peek=" + strconv.FormatBool(peek)
	var out struct {
		Messages []fleetchat.Message `json:"messages"`
		More     bool                `json:"more"`
	}
	if err := s.c.do(ctx, http.MethodGet, agentPath(sessionID)+"/inbox"+q, nil, &out); err != nil {
		return nil, false, err
	}
	return out.Messages, out.More, nil
}

func (s *store) Rewind(ctx context.Context, sessionID string, seq int64) error {
	return s.c.do(ctx, http.MethodPut, agentPath(sessionID)+"/cursor", map[string]int64{"seq": seq}, nil)
}
