package sbfleet

import (
	"context"
	"errors"
	"net/http"
	"net/url"
	"strconv"
	"time"
)

// ErrUnknownSession says that the registry has no row for the session id (core's 404
// unknown-session).
var ErrUnknownSession = errors.New("the fleet registry has no such session")

// Machine is a machine of the session registry. ID is the SHA-256 of its /etc/machine-id
// and the OS user, never the raw machine id.
type Machine struct {
	ID             string    `json:"id"`
	Hostname       string    `json:"hostname"`
	OS             string    `json:"os"`
	OSUser         string    `json:"os_user"`
	ServiceVersion string    `json:"service_version"`
	FirstSeen      time.Time `json:"first_seen"`
	LastSeen       time.Time `json:"last_seen"`
	CreatedAt      time.Time `json:"created_at"`
	UpdatedAt      time.Time `json:"updated_at"`
}

// Credential names the credential a session uses: never its secret.
type Credential struct {
	ID    string  `json:"id"`
	Name  *string `json:"name"`
	Kind  string  `json:"kind"`
	Last4 string  `json:"last4"`
}

// Session is where a session runs: its machine, process, paths, git facts and credential.
// A nil pointer is a fact the machine service did not send. Credential is nil when
// CredentialID is nil or names no credential that still exists.
type Session struct {
	SessionID         string      `json:"session_id"`
	Handle            string      `json:"handle"`
	PID               int64       `json:"pid"`
	ProcStart         string      `json:"proc_start"`
	Cwd               string      `json:"cwd"`
	ConfigDir         string      `json:"config_dir"`
	TranscriptPath    *string     `json:"transcript_path"`
	GitRemote         *string     `json:"git_remote"`
	GitBranch         *string     `json:"git_branch"`
	WorktreeRoot      *string     `json:"worktree_root"`
	GitHead           *string     `json:"git_head"`
	CCVersion         *string     `json:"cc_version"`
	Kind              *string     `json:"kind"`
	Entrypoint        *string     `json:"entrypoint"`
	Name              *string     `json:"name"`
	Status            *string     `json:"status"`
	CredentialID      *string     `json:"credential_id"`
	Credential        *Credential `json:"credential"`
	AccountUUID       *string     `json:"account_uuid"`
	LastRateLimitAt   *time.Time  `json:"last_rate_limit_at"`
	LastResetAt       *time.Time  `json:"last_reset_at"`
	LastSeen          time.Time   `json:"last_seen"`
	PreviousSessionID *string     `json:"previous_session_id"`
	CreatedAt         time.Time   `json:"created_at"`
	UpdatedAt         time.Time   `json:"updated_at"`
	Machine           Machine     `json:"machine"`
}

// Registry reads core's session registry, under the fleet caller's routes.
type Registry struct{ c *client }

// NewRegistry returns a Registry over core's fleet routes at baseURL (core's URL, without
// the route prefix), with the `fleet` credential token returns; it takes the same options
// as NewStore.
func NewRegistry(baseURL string, token func() (string, error), opts ...Option) *Registry {
	return &Registry{c: newClient(baseURL, token, requestTimeout, opts)}
}

// Sessions is up to limit sessions, the most recently seen first, and whether more wait.
// A limit of 0 is core's default (50); core gives at most 200.
func (r *Registry) Sessions(ctx context.Context, limit int) ([]Session, bool, error) {
	route := "/sessions"
	if limit > 0 {
		route += "?limit=" + strconv.Itoa(limit)
	}
	var out struct {
		Sessions []Session `json:"sessions"`
		More     bool      `json:"more"`
	}
	if err := r.c.do(ctx, http.MethodGet, route, nil, &out); err != nil {
		return nil, false, err
	}
	return out.Sessions, out.More, nil
}

// Session is one session's row, or ErrUnknownSession.
func (r *Registry) Session(ctx context.Context, sessionID string) (*Session, error) {
	var out Session
	if err := r.c.do(ctx, http.MethodGet, "/sessions/"+url.PathEscape(sessionID), nil, &out); err != nil {
		return nil, err
	}
	return &out, nil
}
