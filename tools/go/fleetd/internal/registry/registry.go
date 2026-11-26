// Package registry is fleetd's client of core's session registry routes (#7985, #8003):
// PUT …/fleet-rotator/machines/{machine_id} and
// PUT …/fleet-rotator/machines/{machine_id}/sessions/{session_id}. It sends a personal
// access token from a file as `Authorization: Bearer`, as bambuddy-fs does (#8041), over
// https only (http only to localhost), and reads the file again for every request.
package registry

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"

	"github.com/eh-homelab/ScadBuddy/tools/go/fleetd/internal/discover"
)

// path is the fleet-rotator caller's routes, under the API root (…/api/v1).
const path = "/internal/crud/fleet-rotator"

// requestTimeout bounds each request.
const requestTimeout = 30 * time.Second

var (
	// ErrRouteMissing is a 404 with no problem reason: the route is not there, as an older
	// core pod answers during a rollout (measured on prod, 2026-11-26). It is transient: try
	// again at the next scan.
	ErrRouteMissing = errors.New("core has no such route yet (an older core during a rollout?)")
	// ErrUnknownMachine is core's 404 unknown-machine: register the machine again.
	ErrUnknownMachine = errors.New("core does not know this machine")
)

// ProblemError is an answer that is not 2xx, with its problem reason when it has one.
// Detail is core's text: log it on stderr only.
type ProblemError struct {
	Status         int
	Reason, Detail string
}

func (p *ProblemError) Error() string {
	s := fmt.Sprintf("core answered %d", p.Status)
	if p.Reason != "" {
		s += " (" + p.Reason + ")"
	}
	if p.Detail != "" {
		s += ": " + p.Detail
	}
	return s
}

// Client writes this machine's registry rows.
type Client struct {
	base      string
	tokenFile string
	http      *http.Client
}

type options struct {
	base http.RoundTripper
	wrap func(http.RoundTripper) http.RoundTripper
}

// Option configures New.
type Option func(*options)

// WithBaseTransport sets the transport under any wrapper (a test's TLS client).
func WithBaseTransport(rt http.RoundTripper) Option { return func(o *options) { o.base = rt } }

// WrapTransport wraps the transport, such as (*telemetry.Telemetry).Transport.
func WrapTransport(wrap func(http.RoundTripper) http.RoundTripper) Option {
	return func(o *options) { o.wrap = wrap }
}

// CheckAPI refuses an API root that is not https, unless it is http to localhost.
func CheckAPI(api string) (*url.URL, error) {
	u, err := url.Parse(api)
	if err != nil || u.Host == "" {
		return nil, fmt.Errorf("--api %q is not a URL like https://host/api/v1", api)
	}
	switch u.Scheme {
	case "https":
		return u, nil
	case "http":
		if isLocal(u.Hostname()) {
			return u, nil
		}
		return nil, errors.New("--api must be https: the token is sent only over https, or http to localhost")
	}
	return nil, fmt.Errorf("--api must be https, not %q", u.Scheme)
}

func isLocal(host string) bool {
	if host == "localhost" {
		return true
	}
	ip := net.ParseIP(host)
	return ip != nil && ip.IsLoopback()
}

// New is a client of the API root api (like https://host/api/v1) with the token in
// tokenFile.
func New(api, tokenFile string, opts ...Option) (*Client, error) {
	if _, err := CheckAPI(api); err != nil {
		return nil, err
	}
	if tokenFile == "" {
		return nil, errors.New("no token file: pass --token-file or set $FLEETD_TOKEN_FILE")
	}
	o := options{base: http.DefaultTransport}
	for _, f := range opts {
		f(&o)
	}
	rt := o.base
	if o.wrap != nil {
		rt = o.wrap(rt)
	}
	return &Client{
		base:      strings.TrimRight(api, "/") + path,
		tokenFile: tokenFile,
		http: &http.Client{
			Timeout:   requestTimeout,
			Transport: rt,
			// A redirect could take the token to another scheme or host: never follow one.
			CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
		},
	}, nil
}

// token is the token file's contents, trimmed, read now.
func (c *Client) token() (string, error) {
	b, err := os.ReadFile(c.tokenFile)
	if err != nil {
		return "", fmt.Errorf("reading the token file: %w", err)
	}
	t := strings.TrimSpace(string(b))
	if t == "" {
		return "", errors.New("the token file is empty")
	}
	return t, nil
}

func machinePath(id string) string { return "/machines/" + url.PathEscape(id) }

// PutMachine registers the machine, or refreshes its facts and last_seen.
func (c *Client) PutMachine(ctx context.Context, m discover.Machine) error {
	return c.put(ctx, machinePath(m.ID), m)
}

// PutSession writes the session's row on the machine, whole.
func (c *Client) PutSession(ctx context.Context, machineID string, r discover.Row) error {
	return c.put(ctx, machinePath(machineID)+"/sessions/"+url.PathEscape(r.SessionID), r)
}

func (c *Client) put(ctx context.Context, route string, body any) error {
	token, err := c.token()
	if err != nil {
		return err
	}
	b, err := json.Marshal(body)
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPut, c.base+route, bytes.NewReader(b))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")
	req.Header.Set("Authorization", "Bearer "+token)
	res, err := c.http.Do(req)
	if err != nil {
		return err
	}
	defer res.Body.Close()
	if res.StatusCode/100 == 2 {
		_, _ = io.Copy(io.Discard, io.LimitReader(res.Body, 1<<20))
		return nil
	}
	return problemOf(res)
}

func problemOf(res *http.Response) error {
	var p struct {
		Detail string `json:"detail"`
		Reason string `json:"reason"`
	}
	_ = json.NewDecoder(io.LimitReader(res.Body, 64<<10)).Decode(&p)
	if res.StatusCode == http.StatusNotFound && p.Reason == "" {
		return ErrRouteMissing
	}
	pe := &ProblemError{Status: res.StatusCode, Reason: p.Reason, Detail: p.Detail}
	if p.Reason == "unknown-machine" {
		return fmt.Errorf("%w: %w", ErrUnknownMachine, pe)
	}
	return pe
}
