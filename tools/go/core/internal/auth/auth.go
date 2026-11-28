// Package auth resolves who a request is from, as backend/scadbuddy/auth does: a service
// credential (service_credentials.py `resolve`), a person's session cookie (store.py) or
// access token (personal_tokens.py), as gate.py picks one. check.go applies
// SCADBUDDY_AUTH_MODE to a public route as check.py does. The shared vectors
// (backend/tests/fixtures/auth-vectors.json) hold the two equal.
package auth

import (
	"context"
	"crypto/sha256"
	"errors"
	"log/slog"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"strings"
	"sync"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

// touchInterval is how old a credential's last use may be before it is written again.
const touchInterval = time.Minute

// timeout bounds one lookup, as the backend's scadbuddy-auth pool does.
const timeout = 5 * time.Second

var tokenPattern = regexp.MustCompile(`^sbsvc_[0-9a-f]{16}\.[A-Za-z0-9_-]{43}$`)

// perMachineCallers have one bootstrap secret per machine, each in <caller>.<machine>
// (exchange.py PER_MACHINE_CALLERS).
var perMachineCallers = []string{"fleet-rotator"}

// machinePattern is a machine's name in such a file (exchange.py MACHINE_NAME).
var machinePattern = regexp.MustCompile(`^[a-z0-9][a-z0-9-]{0,62}$`)

// Service is the caller a live service credential names.
type Service struct {
	// ID is the credential's id (models.py Service.credential_id).
	ID     string
	Caller string
	Scopes []string
	// Machine is the <machine> of the bootstrap proof file fleet-rotator.<machine> the
	// credential was exchanged for; empty for every other credential.
	Machine string
}

// Has says whether the credential's scopes hold class.
func (s *Service) Has(class string) bool { return slices.Contains(s.Scopes, class) }

type serviceKey struct{}

// WithService is ctx with the service a route's guard let in.
func WithService(ctx context.Context, s *Service) context.Context {
	return context.WithValue(ctx, serviceKey{}, s)
}

// ServiceOf is the service the guard let in, nil when none did.
func ServiceOf(ctx context.Context) *Service {
	s, _ := ctx.Value(serviceKey{}).(*Service)
	return s
}

// Resolver looks credentials up in Postgres. It keeps no cache: a revocation holds at once.
type Resolver struct {
	Pool *pgxpool.Pool
	Now  func() time.Time
	// BootstrapDir is SCADBUDDY_SERVICE_BOOTSTRAP_DIR, where a per-machine caller's proof
	// files are (exchange.py BootstrapVerifier). A credential that names a machine passes
	// only while its file is there; with no directory, none passes.
	BootstrapDir string

	// mu guards the failed lookups' log (caller.go failed).
	mu       sync.Mutex
	loggedAt time.Time
	unlogged int
}

// Resolve is the service the Authorization header names, or nil. A database that does not
// answer is no service, as gate.py's anonymous caller: the route then answers 401.
func (r *Resolver) Resolve(ctx context.Context, authorization string) *Service {
	scheme, bearer, _ := strings.Cut(authorization, " ")
	token := strings.TrimSpace(bearer)
	if !strings.EqualFold(scheme, "bearer") || !tokenPattern.MatchString(token) {
		return nil
	}
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	now := time.Now
	if r.Now != nil {
		now = r.Now
	}
	service, err := r.lookup(ctx, sha256.Sum256([]byte(token)), now().UTC())
	if err != nil {
		slog.WarnContext(ctx, "a service credential could not be looked up", "error", err.Error())
		return nil
	}
	return service
}

func (r *Resolver) lookup(ctx context.Context, hash [32]byte, now time.Time) (*Service, error) {
	var (
		id   string
		s    Service
		last *time.Time
	)
	err := r.Pool.QueryRow(ctx,
		`SELECT id, caller, scopes, coalesce(machine, ''), last_used_at FROM service_credentials c
		 WHERE secret_hash = $1 AND revoked_at IS NULL AND expires_at > $2
		   AND NOT EXISTS (SELECT 1 FROM service_caller_suspensions s WHERE s.caller = c.caller)`,
		hash[:], now).Scan(&id, &s.Caller, &s.Scopes, &s.Machine, &last)
	s.ID = id
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	if s.Machine != "" && !r.HasProof(s.Caller, s.Machine) {
		// The machine was revoked (its proof file went): refused now and revoked, as
		// service_credentials.py resolve does.
		if _, err := r.Pool.Exec(ctx,
			`UPDATE service_credentials SET revoked_at = $1 WHERE id = $2 AND revoked_at IS NULL`,
			now, id); err != nil {
			return nil, err
		}
		return nil, nil
	}
	if last == nil || now.Sub(*last) >= touchInterval {
		if _, err := r.Pool.Exec(ctx,
			`UPDATE service_credentials SET last_used_at = $1 WHERE id = $2`, now, id); err != nil {
			return nil, err
		}
	}
	return &s, nil
}

// proofName is the bootstrap proof file of caller's machine, <caller>.<machine>; false when
// caller has no secret per machine or machine is no machine's name (exchange.py
// MACHINE_NAME), so the name never leaves the bootstrap directory.
func proofName(caller, machine string) (string, bool) {
	if !slices.Contains(perMachineCallers, caller) || !machinePattern.MatchString(machine) {
		return "", false
	}
	return caller + "." + machine, true
}

// HasProof says whether the proof file of caller's machine is a regular file in the
// bootstrap directory now (one stat), as exchange.py BootstrapVerifier.has_machine does.
// With no bootstrap directory, no machine has its proof.
func (r *Resolver) HasProof(caller, machine string) bool {
	name, ok := proofName(caller, machine)
	if !ok || r.BootstrapDir == "" {
		return false
	}
	info, err := os.Stat(filepath.Join(r.BootstrapDir, name))
	return err == nil && info.Mode().IsRegular()
}
