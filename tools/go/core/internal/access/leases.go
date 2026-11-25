package access

import (
	"context"
	"errors"
	"fmt"
	"math"
	"net/http"
	"regexp"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
)

// The agent's leases (#5299 D3): agent_data/leases.py. Its header says what each key's
// advisory lock or row lock stands for during a rolling deploy.
const (
	leaseTTL = 120.0
	maxTTL   = 300.0
	// maxWait is the longest wait an acquire asks for; acquirePoll how often it tries again.
	maxWait     = 25.0
	acquirePoll = 250 * time.Millisecond
	// maxRowWaits is how many acquires of this process may wait on a grant's row lock at
	// once. Each holds a pool connection while it waits; past the cap an acquire does not
	// wait on the row and polls as for any busy lease.
	maxRowWaits       = 2
	leaseKeyMax       = sourceMax + 20
	advisoryPrefix    = "scadbuddy:ai_tool_listings:test:"
	attentionRateLock = "scadbuddy:attention-rate"
	orderCostPrefix   = "ai_session_order_costs:"
	leaseBusy         = "busy"
)

var keyPattern = regexp.MustCompile(`^(tool-listing:(remote:[a-z][a-z0-9-]{0,30}[a-z0-9]|package:[^\x00-\x1f]{1,400})` +
	`|plugin-oauth:[a-z][a-z0-9-]{0,30}[a-z0-9]|attention-rate` +
	`|order-cost:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$`)

// leaseKey is the path's key, checked as the route checks it: its bounds with the body's
// issues, then its pattern.
func leaseKey(f *form, p crud.Params) string {
	key := p["key"]
	if msg := lengthIssue(key, 1, leaseKeyMax); msg != "" {
		f.fail([]string{"path", "key"}, msg)
	}
	return key
}

func checkedKey(key string) error {
	if !keyPattern.MatchString(key) {
		return &httpx.Problem{Status: http.StatusUnprocessableEntity, Detail: "That is not a lease key."}
	}
	return nil
}

// sqlLock is the advisory lock key the SQL path takes for key, "" for none.
func sqlLock(key string) string {
	switch {
	case strings.HasPrefix(key, "tool-listing:"):
		source := strings.TrimPrefix(key, "tool-listing:")
		return advisoryPrefix + strings.TrimPrefix(source, "remote:")
	case key == "attention-rate":
		return attentionRateLock
	case strings.HasPrefix(key, "order-cost:"):
		return orderCostPrefix + strings.TrimPrefix(key, "order-cost:")
	}
	return ""
}

// acquire is leases.py acquire in the caller's transaction: the new expiry, or nil when
// another holder keeps key. wait bounds the wait for an SQL pod's grant row lock.
func acquire(ctx context.Context, tx pgx.Tx, key, token string, ttl, wait float64) (*time.Time, error) {
	if lock := sqlLock(key); lock != "" {
		var held bool
		if err := tx.QueryRow(ctx, `SELECT pg_try_advisory_xact_lock(hashtextextended($1, 0)) AS held`,
			lock).Scan(&held); err != nil {
			return nil, err
		}
		if !held {
			return nil, nil
		}
	}
	if plugin, ok := strings.CutPrefix(key, "plugin-oauth:"); ok {
		// Waits while an SQL pod holds the grant's row across a refresh, but no longer than
		// the caller asked: a lock_timeout of 0 waits for ever, so at least 1 ms. The setting
		// in force before is put back after, by the restore once the row is locked, and by
		// the savepoint's rollback when the wait ends in lock_not_available.
		var before string
		if err := tx.QueryRow(ctx, `SELECT current_setting('lock_timeout') AS t`).Scan(&before); err != nil {
			return nil, err
		}
		got, err := rowLocked(ctx, tx, plugin, wait, before)
		if err != nil || !got {
			return nil, err
		}
	}
	var expires time.Time
	err := tx.QueryRow(ctx, `INSERT INTO agent_leases (key, token, expires_at)
		VALUES ($1, $2, now() + make_interval(secs => $3))
		ON CONFLICT (key) DO UPDATE SET token = EXCLUDED.token, expires_at = EXCLUDED.expires_at
		WHERE agent_leases.expires_at <= now() OR agent_leases.token = EXCLUDED.token
		RETURNING expires_at`, key, token, ttl).Scan(&expires)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return &expires, nil
}

// rowLocked takes the grant's row lock in a savepoint, waiting at most wait seconds.
func rowLocked(ctx context.Context, tx pgx.Tx, plugin string, wait float64, before string) (bool, error) {
	sp, err := tx.Begin(ctx)
	if err != nil {
		return false, err
	}
	defer func() { _ = sp.Rollback(ctx) }()
	ms := max(1, int64(math.RoundToEven(wait*1000)))
	if _, err := sp.Exec(ctx, `SELECT set_config('lock_timeout', $1, true)`, fmt.Sprintf("%dms", ms)); err != nil {
		return false, err
	}
	_, err = sp.Exec(ctx, `SELECT 1 FROM ai_plugin_oauth WHERE plugin = $1 FOR UPDATE`, plugin)
	var pg *pgconn.PgError
	if errors.As(err, &pg) && pg.Code == "55P03" {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	if _, err := sp.Exec(ctx, `SELECT set_config('lock_timeout', $1, true)`, before); err != nil {
		return false, err
	}
	return true, sp.Commit(ctx)
}

type leaseHeld struct {
	Token     string `json:"token"`
	ExpiresAt string `json:"expires_at"`
}

// leaseAsk reads a lease body: the token, ttl_s and, for an acquire, wait_s.
func leaseAsk(r *http.Request, p crud.Params, waits bool) (key, token string, ttl, wait float64, err error) {
	raw, err := readBody(r)
	if err != nil {
		return "", "", 0, 0, err
	}
	f := &form{}
	key = leaseKey(f, p)
	loc := []string{"body"}
	names := []string{"token", "ttl_s"}
	if waits {
		names = append(names, "wait_s")
	}
	fields := f.object(loc, raw, names...)
	token = f.uuid(at(loc, "token"), f.required(loc, fields, "token"))
	ttl = f.number(at(loc, "ttl_s"), fields["ttl_s"], leaseTTL, 0, false, maxTTL)
	if waits {
		wait = f.number(at(loc, "wait_s"), fields["wait_s"], 0, 0, true, maxWait)
	}
	if err := f.err(); err != nil {
		return "", "", 0, 0, err
	}
	return key, token, ttl, wait, checkedKey(key)
}

// rowWait says whether this acquire may wait on the grant's row, and counts it when so.
func (s *Store) rowWait(key string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	if !strings.HasPrefix(key, "plugin-oauth:") || s.rowWaits >= maxRowWaits {
		return false
	}
	s.rowWaits++
	return true
}

func (s *Store) rowWaitDone() {
	s.mu.Lock()
	s.rowWaits--
	s.mu.Unlock()
}

// acquireLease takes a lease for token, waiting up to wait_s, or answers 409 busy.
func (s *Store) acquireLease(r *http.Request, p crud.Params) (any, error) {
	key, token, ttl, waitS, err := leaseAsk(r, p, true)
	if err != nil {
		return nil, err
	}
	ctx := r.Context()
	deadline := time.Now().Add(time.Duration(waitS * float64(time.Second)))
	for {
		waits := s.rowWait(key)
		wait := 0.0
		if waits {
			wait = max(0, time.Until(deadline).Seconds())
		}
		var expires *time.Time
		err := pgx.BeginFunc(ctx, s.Pool, func(tx pgx.Tx) error {
			var err error
			expires, err = acquire(ctx, tx, key, token, ttl, wait)
			return err
		})
		if waits {
			s.rowWaitDone()
		}
		if err != nil {
			return nil, err
		}
		left := time.Until(deadline)
		if expires != nil {
			return leaseHeld{token, isoTime(*expires)}, nil
		}
		if left <= 0 {
			return nil, httpx.WithReason(http.StatusConflict, "Another holder keeps the lease.", leaseBusy)
		}
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-time.After(min(acquirePoll, left)):
		}
	}
}

func (s *Store) renewLease(r *http.Request, p crud.Params) (any, error) {
	key, token, ttl, _, err := leaseAsk(r, p, false)
	if err != nil {
		return nil, err
	}
	var expires *time.Time
	err = pgx.BeginFunc(r.Context(), s.Pool, func(tx pgx.Tx) error {
		var at time.Time
		err := tx.QueryRow(r.Context(), `UPDATE agent_leases SET expires_at = now() + make_interval(secs => $1)
			WHERE key = $2 AND token = $3 AND expires_at > now() RETURNING expires_at`,
			ttl, key, token).Scan(&at)
		if errors.Is(err, pgx.ErrNoRows) {
			return nil
		}
		expires = &at
		return err
	})
	if err != nil {
		return nil, err
	}
	if expires == nil {
		return nil, leaseLostProblem()
	}
	return leaseHeld{token, isoTime(*expires)}, nil
}

func (s *Store) releaseLease(r *http.Request, p crud.Params) (any, error) {
	f := &form{}
	key := leaseKey(f, p)
	var token string
	if v, ok := f.query(r, "token"); ok {
		token = f.uuid([]string{"query", "token"}, quoted(v))
	}
	if err := f.err(); err != nil {
		return nil, err
	}
	if err := checkedKey(key); err != nil {
		return nil, err
	}
	return nil, pgx.BeginFunc(r.Context(), s.Pool, func(tx pgx.Tx) error {
		_, err := tx.Exec(r.Context(), `DELETE FROM agent_leases WHERE key = $1 AND token = $2`, key, token)
		return err
	})
}
