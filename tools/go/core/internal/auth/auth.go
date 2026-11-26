// Package auth resolves a service credential, as backend/scadbuddy/auth does
// (service_auth.py, service_credentials.py `resolve`, gate.py).
package auth

import (
	"context"
	"crypto/sha256"
	"errors"
	"log/slog"
	"regexp"
	"slices"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

// touchInterval is how old a credential's last use may be before it is written again.
const touchInterval = time.Minute

// timeout bounds one lookup, as the backend's scadbuddy-auth pool does.
const timeout = 5 * time.Second

var tokenPattern = regexp.MustCompile(`^sbsvc_[0-9a-f]{16}\.[A-Za-z0-9_-]{43}$`)

// Service is the caller a live service credential names.
type Service struct {
	// ID is the credential's id (models.py Service.credential_id).
	ID     string
	Caller string
	Scopes []string
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
		`SELECT id, caller, scopes, last_used_at FROM service_credentials c
		 WHERE secret_hash = $1 AND revoked_at IS NULL AND expires_at > $2
		   AND NOT EXISTS (SELECT 1 FROM service_caller_suspensions s WHERE s.caller = c.caller)`,
		hash[:], now).Scan(&id, &s.Caller, &s.Scopes, &last)
	s.ID = id
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	if last == nil || now.Sub(*last) >= touchInterval {
		if _, err := r.Pool.Exec(ctx,
			`UPDATE service_credentials SET last_used_at = $1 WHERE id = $2`, now, id); err != nil {
			return nil, err
		}
	}
	return &s, nil
}
