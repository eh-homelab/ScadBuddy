package fleet

// The session registry (spec 2026-11-26-fleet-session-registry-and-rotator-design §6.3,
// #7985): the machines that run Claude Code sessions, and where each session runs. Caller
// `fleet-rotator` (the machine service) writes it, with a `fleet` access token (#8041) or a
// machine's bootstrap proof (#8003); caller `fleet` (the chat server) reads it.
// A read gives a credential's id, name, kind and last 4 characters, never its secret.

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"regexp"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/auth"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/wire"
)

// RotatorCaller is the machine service's caller, whose routes are under
// /api/v1/internal/crud/fleet-rotator.
const RotatorCaller = "fleet-rotator"

const (
	registryBodyMax = 64 << 10
	pathMax         = 4096
	// pidMax is Linux's highest pid_max (2^22).
	pidMax = 1 << 22
)

var (
	// machinePattern is a machine's id: the SHA-256, in hex, of its /etc/machine-id and the
	// OS user. A raw machine id (32 hex characters) does not match.
	machinePattern = regexp.MustCompile(`^[0-9a-f]{64}$`)
	// plainPattern is a text with no control character (C0, DEL or C1).
	plainPattern = regexp.MustCompile(`^[^\x00-\x1f\x7f\x{80}-\x{9f}]+$`)
	// absolutePattern is an absolute path with no control character.
	absolutePattern = regexp.MustCompile(`^/[^\x00-\x1f\x7f\x{80}-\x{9f}]*$`)
	versionPattern  = regexp.MustCompile(`^[0-9A-Za-z.+_-]+$`)
	procPattern     = regexp.MustCompile(`^[0-9]{1,20}$`)
	headPattern     = regexp.MustCompile(`^[0-9a-f]{40}([0-9a-f]{24})?$`)
	// userPattern is the only user name a remote may carry (ssh's git@).
	userPattern = regexp.MustCompile(`^[A-Za-z0-9._-]+$`)
	// hostPattern is a remote's host name or address: no leading '-', so ssh cannot read it
	// as an option.
	hostPattern = regexp.MustCompile(`^[A-Za-z0-9_\[][A-Za-z0-9._:\[\]-]*$`)
	// scpPattern is git's scp-like form, user@host:path. The user is required, so a local
	// path or a transport such as ext:: is never read as one.
	scpPattern = regexp.MustCompile(`^([A-Za-z0-9._-]+)@([A-Za-z0-9_][A-Za-z0-9.-]*):(.+)$`)
)

// remoteProblem is why remote is no remote the registry keeps, "" when it is one. Only
// http(s), ssh and git URLs and the scp-like form pass. No form may carry a password, a
// token, a query or a fragment; only ssh may carry a user name.
func remoteProblem(remote string) string {
	if strings.ContainsAny(remote, "?#\\") {
		return "a remote must not hold a query, a fragment or a backslash"
	}
	if m := scpPattern.FindStringSubmatch(remote); m != nil {
		if strings.HasPrefix(m[3], "-") || strings.HasPrefix(m[3], ":") {
			return "a remote's path must not start with '-' or ':'"
		}
		return ""
	}
	scheme, _, found := strings.Cut(remote, "://")
	if !found {
		return "a remote is an http(s), ssh or git URL, or user@host:path"
	}
	u, err := url.Parse(remote)
	if err != nil || u.Opaque != "" {
		return "a remote is an http(s), ssh or git URL, or user@host:path"
	}
	switch scheme {
	case "http", "https", "ssh", "git":
	default:
		return "a remote is an http(s), ssh or git URL, or user@host:path"
	}
	if !hostPattern.MatchString(u.Host) {
		return "a remote names a host"
	}
	if u.User != nil {
		_, hasPassword := u.User.Password()
		if scheme != "ssh" || hasPassword || !userPattern.MatchString(u.User.Username()) ||
			strings.Contains(u.User.String(), "%") {
			return "a remote must not hold a password or a token; only ssh names a user"
		}
	}
	return ""
}

// owner is who may write a machine's rows: a fleet access token's id (#8041), or the
// machine of a credential's bootstrap proof (fleet-rotator.<machine>, #8003). Exactly one is
// set; a credential that names no machine is a 403. principal is a token's person.
type owner struct {
	proof, token *string
	principal    string
}

func ownerOf(r *http.Request) (owner, error) {
	if c := auth.CallerOf(r.Context()); c.Kind == auth.KindToken && c.TokenID != "" {
		return owner{token: &c.TokenID, principal: c.Principal}, nil
	}
	if s := crud.ServiceOf(r); s != nil && s.Machine != "" {
		return owner{proof: &s.Machine}, nil
	}
	return owner{}, httpx.WithReason(http.StatusForbidden,
		"this credential names no machine: use the machine's fleet access token", "no-machine")
}

func same(a, b *string) bool { return (a == nil) == (b == nil) && (a == nil || *a == *b) }

func anotherProof() error {
	return httpx.WithReason(http.StatusForbidden,
		"another machine's token or proof registered that machine", "machine-of-another-proof")
}

// unknownOwner is the 403 for a machine whose token row is gone: its person is unknown, so
// no token takes it over.
func proofOwner() error {
	return httpx.WithReason(http.StatusForbidden,
		"a bootstrap proof registered that machine, and no token takes over a proof's machine: "+
			"ask an admin to delete the machine's row", "machine-of-a-proof")
}

func unknownOwner() error {
	return httpx.WithReason(http.StatusForbidden,
		"the token that registered that machine is gone, so its owner is unknown and no token "+
			"can take it over: ask an admin to delete the machine's row", "machine-of-unknown-owner")
}

// RegistryReads are caller fleet's registry routes, under /api/v1/internal/crud/fleet.
func RegistryReads(pool *pgxpool.Pool) []crud.Route {
	f := &Fleet{Pool: pool}
	return []crud.Route{
		{Method: "GET", Path: "/sessions", Handle: f.sessions},
		{Method: "GET", Path: "/sessions/{session_id}", Handle: f.session},
	}
}

// RegistryWrites are caller fleet-rotator's registry routes, under
// /api/v1/internal/crud/fleet-rotator.
func RegistryWrites(pool *pgxpool.Pool) []crud.Route {
	w := &registryWriter{Fleet: Fleet{Pool: pool}}
	f := &w.Fleet
	return []crud.Route{
		{Method: "PUT", Path: "/machines/{id}", Handle: w.putMachine, MaxBody: registryBodyMax},
		{Method: "PUT", Path: "/machines/{machine_id}/sessions/{session_id}", Handle: f.putSession,
			MaxBody: registryBodyMax},
	}
}

type machine struct {
	ID             string    `json:"id"`
	Hostname       string    `json:"hostname"`
	OS             string    `json:"os"`
	OSUser         string    `json:"os_user"`
	ServiceVersion string    `json:"service_version"`
	FirstSeen      wire.Time `json:"first_seen"`
	LastSeen       wire.Time `json:"last_seen"`
	CreatedAt      wire.Time `json:"created_at"`
	UpdatedAt      wire.Time `json:"updated_at"`
}

const machineColumns = `m.id, m.hostname, m.os, m.os_user, m.service_version, m.first_seen, m.last_seen,
	m.created_at, m.updated_at`

func (m *machine) targets() []any {
	return []any{&m.ID, &m.Hostname, &m.OS, &m.OSUser, &m.ServiceVersion, &m.FirstSeen, &m.LastSeen,
		&m.CreatedAt, &m.UpdatedAt}
}

type registryWriter struct {
	Fleet
}

// putMachine registers the machine under the caller's token or proof name, or refreshes its
// facts and last_seen. A machine another token or proof registered is a 403, unless its
// owner is dead: a token's machine moves to a new token of the same person once the old
// token is revoked or expired. A token never takes a proof's machine (403 machine-of-a-proof:
// an admin deletes the row first), and a proof never takes a token's machine. The row is locked while this decides.
func (f *registryWriter) putMachine(r *http.Request, p crud.Params) (any, error) {
	own, err := ownerOf(r)
	if err != nil {
		return nil, err
	}
	id, err := p.Match("id", machinePattern)
	if err != nil {
		return nil, err
	}
	b, err := crud.ReadBody(r, "hostname", "os", "os_user", "service_version")
	if err != nil {
		return nil, err
	}
	hostname := b.Match("hostname", 1, 253, plainPattern)
	os := b.Match("os", 1, 100, plainPattern)
	user := b.Match("os_user", 1, 64, plainPattern)
	version := b.Match("service_version", 1, 64, versionPattern)
	if err := b.Err(); err != nil {
		return nil, err
	}
	ctx := r.Context()
	var m machine
	err = pgx.BeginFunc(ctx, f.Pool, func(tx pgx.Tx) error {
		err := tx.QueryRow(ctx, `INSERT INTO fleet_machines AS m
				(id, hostname, os, os_user, service_version, proof_name, token_id)
				VALUES ($1, $2, $3, $4, $5, $6, $7)
			ON CONFLICT (id) DO NOTHING
			RETURNING `+machineColumns, id, hostname, os, user, version, own.proof, own.token).Scan(m.targets()...)
		if !errors.Is(err, pgx.ErrNoRows) {
			return err
		}
		// The row exists: lock it, then decide whether the caller may write it.
		var row owner
		if err := tx.QueryRow(ctx, `SELECT proof_name, token_id FROM fleet_machines WHERE id = $1 FOR UPDATE`,
			id).Scan(&row.proof, &row.token); err != nil {
			return err
		}
		if err := f.mayTake(ctx, tx, row, own); err != nil {
			return err
		}
		return tx.QueryRow(ctx, `UPDATE fleet_machines AS m SET hostname = $2, os = $3, os_user = $4,
				service_version = $5, proof_name = $6, token_id = $7, last_seen = now(), updated_at = now()
			WHERE id = $1
			RETURNING `+machineColumns, id, hostname, os, user, version, own.proof, own.token).Scan(m.targets()...)
	})
	if err != nil {
		return nil, err
	}
	return m, nil
}

// mayTake is nil when own may write a machine row owns: own is row's owner, or row's owner
// is dead and own may take its place.
func (f *registryWriter) mayTake(ctx context.Context, tx pgx.Tx, row, own owner) error {
	if same(row.proof, own.proof) && same(row.token, own.token) {
		return nil
	}
	if own.token == nil {
		// A proof never takes another owner's machine.
		return anotherProof()
	}
	if row.proof != nil {
		// A proof has no person, so no token can show it is the machine's: fail closed.
		return proofOwner()
	}
	var (
		principal string
		dead      bool
	)
	err := tx.QueryRow(ctx, `SELECT principal, revoked_at IS NOT NULL OR expires_at <= now()
		FROM personal_access_tokens WHERE id = $1`, *row.token).Scan(&principal, &dead)
	if errors.Is(err, pgx.ErrNoRows) {
		return unknownOwner()
	}
	if err != nil {
		return err
	}
	if !dead || principal != own.principal {
		return anotherProof()
	}
	return nil
}

// putSession writes the session's row on the machine whole: an optional field left out is
// cleared. One session id can run on several machines at once, so each machine has a row of
// its own, and a caller writes only the rows of machines registered to its token or proof
// (403). It
// makes the session's fleet agent as touch does, when there is none. A new row is linked to
// the newest row of the same machine, pid and proc_start (the session before a /clear).
func (f *Fleet) putSession(r *http.Request, p crud.Params) (any, error) {
	own, err := ownerOf(r)
	if err != nil {
		return nil, err
	}
	machineID, err := p.Match("machine_id", machinePattern)
	if err != nil {
		return nil, err
	}
	session, err := p.UUID("session_id")
	if err != nil {
		return nil, err
	}
	b, err := crud.ReadBody(r, "pid", "proc_start", "cwd", "config_dir",
		"transcript_path", "git_remote", "git_branch", "worktree_root", "git_head", "cc_version",
		"kind", "entrypoint", "name", "status", "credential_id", "account_uuid",
		"last_rate_limit_at", "last_reset_at")
	if err != nil {
		return nil, err
	}
	pid := b.IntRange("pid", 1, pidMax)
	proc := b.Match("proc_start", 1, 20, procPattern)
	cwd := b.Match("cwd", 1, pathMax, absolutePattern)
	configDir := b.Match("config_dir", 1, pathMax, absolutePattern)
	transcript := b.OptText("transcript_path", 1, pathMax, absolutePattern)
	remote := b.OptText("git_remote", 1, 2048, plainPattern)
	if remote != nil {
		if problem := remoteProblem(*remote); problem != "" {
			b.Fail([]string{"git_remote"}, problem)
		}
	}
	branch := b.OptText("git_branch", 1, 255, plainPattern)
	worktree := b.OptText("worktree_root", 1, pathMax, absolutePattern)
	head := b.OptText("git_head", 40, 64, headPattern)
	ccVersion := b.OptText("cc_version", 1, 64, versionPattern)
	kind := b.OptText("kind", 1, 40, plainPattern)
	entrypoint := b.OptText("entrypoint", 1, 40, plainPattern)
	name := b.OptText("name", 1, 200, plainPattern)
	status := b.OptText("status", 1, 40, plainPattern)
	credential := b.OptText("credential_id", 1, 64, plainPattern)
	account := b.OptUUID("account_uuid")
	rateLimited := b.Datetime("last_rate_limit_at", true)
	reset := b.Datetime("last_reset_at", true)
	if err := b.Err(); err != nil {
		return nil, err
	}
	ctx := r.Context()
	err = pgx.BeginFunc(ctx, f.Pool, func(tx pgx.Tx) error {
		var row owner
		err := tx.QueryRow(ctx, `SELECT proof_name, token_id FROM fleet_machines WHERE id = $1 FOR KEY SHARE`,
			machineID).Scan(&row.proof, &row.token)
		if errors.Is(err, pgx.ErrNoRows) {
			return httpx.WithReason(http.StatusNotFound,
				"no fleet machine has that id: register it first", "unknown-machine")
		}
		if err != nil {
			return err
		}
		if !same(row.proof, own.proof) || !same(row.token, own.token) {
			return anotherProof()
		}
		_, err = tx.Exec(ctx, `INSERT INTO fleet_agents (session_id, handle) VALUES ($1, $2)
			ON CONFLICT (session_id) DO NOTHING`, session, session[:8])
		var pg *pgconn.PgError
		if errors.As(err, &pg) && pg.Code == "23505" {
			return httpx.WithReason(http.StatusConflict,
				fmt.Sprintf(`another session has the handle "%s"`, session[:8]), "handle-taken")
		}
		if err != nil {
			return err
		}
		_, err = tx.Exec(ctx, `INSERT INTO fleet_session_hosts (session_id, machine_id, pid, proc_start,
				cwd, config_dir, transcript_path, git_remote, git_branch, worktree_root, git_head, cc_version,
				kind, entrypoint, name, status, credential_id, account_uuid, last_rate_limit_at,
				last_reset_at, previous_session_id)
			VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18,
				$19::timestamptz, $20::timestamptz,
				(SELECT h.session_id FROM fleet_session_hosts h
				 WHERE h.machine_id = $2 AND h.pid = $3 AND h.proc_start = $4 AND h.session_id <> $1
				 ORDER BY h.created_at DESC LIMIT 1))
			ON CONFLICT (session_id, machine_id) DO UPDATE SET pid = $3, proc_start = $4, cwd = $5,
				config_dir = $6, transcript_path = $7, git_remote = $8, git_branch = $9,
				worktree_root = $10, git_head = $11, cc_version = $12, kind = $13, entrypoint = $14,
				name = $15, status = $16, credential_id = $17, account_uuid = $18,
				last_rate_limit_at = $19::timestamptz, last_reset_at = $20::timestamptz,
				last_seen = now(), updated_at = now()`,
			session, machineID, pid, proc, cwd, configDir, transcript, remote, branch, worktree, head,
			ccVersion, kind, entrypoint, name, status, credential, account, rateLimited, reset)
		return err
	})
	if err != nil {
		return nil, err
	}
	rows, err := f.Pool.Query(ctx, sessionSelect+` WHERE h.session_id = $1 AND h.machine_id = $2`,
		session, machineID)
	if err != nil {
		return nil, err
	}
	return pgx.CollectExactlyOneRow(rows, scanSession)
}

type credentialInfo struct {
	ID    string  `json:"id"`
	Name  *string `json:"name"`
	Kind  string  `json:"kind"`
	Last4 string  `json:"last4"`
}

// host is a session's row on one machine.
type host struct {
	PID               int64           `json:"pid"`
	ProcStart         string          `json:"proc_start"`
	Cwd               string          `json:"cwd"`
	ConfigDir         string          `json:"config_dir"`
	TranscriptPath    *string         `json:"transcript_path"`
	GitRemote         *string         `json:"git_remote"`
	GitBranch         *string         `json:"git_branch"`
	WorktreeRoot      *string         `json:"worktree_root"`
	GitHead           *string         `json:"git_head"`
	CCVersion         *string         `json:"cc_version"`
	Kind              *string         `json:"kind"`
	Entrypoint        *string         `json:"entrypoint"`
	Name              *string         `json:"name"`
	Status            *string         `json:"status"`
	CredentialID      *string         `json:"credential_id"`
	Credential        *credentialInfo `json:"credential"`
	AccountUUID       *string         `json:"account_uuid"`
	LastRateLimitAt   *crud.Time      `json:"last_rate_limit_at"`
	LastResetAt       *crud.Time      `json:"last_reset_at"`
	LastSeen          wire.Time       `json:"last_seen"`
	PreviousSessionID *string         `json:"previous_session_id"`
	CreatedAt         wire.Time       `json:"created_at"`
	UpdatedAt         wire.Time       `json:"updated_at"`
	Machine           machine         `json:"machine"`
}

// sessionHost is one machine's row of a session, with the session's id and handle: a PUT's
// answer. It names no other machine's row.
type sessionHost struct {
	SessionID string `json:"session_id"`
	Handle    string `json:"handle"`
	host
}

// session is a session with its hosts, one per machine it runs on, the most recently seen
// first. LastSeen is the newest host's.
type session struct {
	SessionID string    `json:"session_id"`
	Handle    string    `json:"handle"`
	LastSeen  wire.Time `json:"last_seen"`
	Hosts     []host    `json:"hosts"`
}

// sessionsOf groups rows ordered by session, then host, into sessions.
func sessionsOf(rows []sessionHost) []session {
	out := []session{}
	for _, row := range rows {
		if n := len(out); n == 0 || out[n-1].SessionID != row.SessionID {
			out = append(out, session{SessionID: row.SessionID, Handle: row.Handle, LastSeen: row.LastSeen})
		}
		last := &out[len(out)-1]
		last.Hosts = append(last.Hosts, row.host)
	}
	return out
}

// hostOrder orders a session's hosts: the most recently seen first.
const hostOrder = `h.last_seen DESC, h.machine_id`

// sessionSelect reads a session row with its agent's handle, its machine and the
// credential's non-secret columns. ai_credentials is joined by id: no secret column is read.
const sessionSelect = `SELECT h.session_id::text, a.handle, h.pid, h.proc_start, h.cwd, h.config_dir,
		h.transcript_path, h.git_remote, h.git_branch, h.worktree_root, h.git_head, h.cc_version,
		h.kind, h.entrypoint, h.name, h.status, h.credential_id, c.id, c.name, c.kind, c.last4,
		h.account_uuid::text, h.last_rate_limit_at, h.last_reset_at, h.last_seen,
		h.previous_session_id::text, h.created_at, h.updated_at, ` + machineColumns + `
	FROM fleet_session_hosts h
	JOIN fleet_agents a USING (session_id)
	JOIN fleet_machines m ON m.id = h.machine_id
	LEFT JOIN ai_credentials c ON c.id = h.credential_id`

func scanSession(row pgx.CollectableRow) (sessionHost, error) {
	var (
		s                       sessionHost
		credID, credKind, last4 *string
		credName                *string
		rateLimited, reset      *time.Time
	)
	targets := []any{&s.SessionID, &s.Handle, &s.PID, &s.ProcStart, &s.Cwd, &s.ConfigDir,
		&s.TranscriptPath, &s.GitRemote, &s.GitBranch, &s.WorktreeRoot, &s.GitHead, &s.CCVersion,
		&s.Kind, &s.Entrypoint, &s.Name, &s.Status, &s.CredentialID, &credID, &credName, &credKind,
		&last4, &s.AccountUUID, &rateLimited, &reset, &s.LastSeen, &s.PreviousSessionID,
		&s.CreatedAt, &s.UpdatedAt}
	if err := row.Scan(append(targets, s.Machine.targets()...)...); err != nil {
		return s, err
	}
	if credID != nil {
		s.Credential = &credentialInfo{ID: *credID, Name: credName, Kind: *credKind, Last4: *last4}
	}
	s.LastRateLimitAt, s.LastResetAt = crud.TimeOf(rateLimited), crud.TimeOf(reset)
	return s, nil
}

// session is one session with all its hosts, each with its machine and credential.
func (f *Fleet) session(r *http.Request, p crud.Params) (any, error) {
	id, err := p.UUID("session_id")
	if err != nil {
		return nil, err
	}
	rows, err := f.Pool.Query(r.Context(), sessionSelect+` WHERE h.session_id = $1 ORDER BY `+hostOrder, id)
	if err != nil {
		return nil, err
	}
	found, err := pgx.CollectRows(rows, scanSession)
	if err != nil {
		return nil, err
	}
	if len(found) == 0 {
		return nil, httpx.WithReason(http.StatusNotFound, "no fleet session has that id", "unknown-session")
	}
	return sessionsOf(found)[0], nil
}

// sessions is up to limit sessions, each with all its hosts, the most recently seen session
// first (by its newest host), and whether more wait.
func (f *Fleet) sessions(r *http.Request, _ crud.Params) (any, error) {
	limit, err := pageLimit(r)
	if err != nil {
		return nil, err
	}
	rows, err := f.Pool.Query(r.Context(), `WITH page AS (
			SELECT session_id, max(last_seen) AS seen FROM fleet_session_hosts
			GROUP BY session_id ORDER BY seen DESC, session_id LIMIT $1)
		`+sessionSelect+`
		JOIN page p ON p.session_id = h.session_id
		ORDER BY p.seen DESC, h.session_id, `+hostOrder, limit+1)
	if err != nil {
		return nil, err
	}
	found, err := pgx.CollectRows(rows, scanSession)
	if err != nil {
		return nil, err
	}
	list := sessionsOf(found)
	more := int64(len(list)) > limit
	if more {
		list = list[:limit]
	}
	return map[string]any{"sessions": list, "more": more}, nil
}
