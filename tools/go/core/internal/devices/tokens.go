package devices

import (
	"context"
	"errors"
	"regexp"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
)

// tokenPattern is a device token: the prefix and 32 random bytes, base64url.
var tokenPattern = regexp.MustCompile(`^sbdev_[A-Za-z0-9_-]{43}$`)

// Device is a paired device as a device route sees it.
type Device struct {
	ID           string
	Owner        string
	Name         string
	Capabilities []string
}

// bearer is the token an Authorization header carries, "" when it carries none.
func bearer(authorization string) string {
	scheme, token, _ := strings.Cut(authorization, " ")
	token = strings.TrimSpace(token)
	if !strings.EqualFold(scheme, "bearer") || !tokenPattern.MatchString(token) {
		return ""
	}
	return token
}

// Authenticate is the device an Authorization header's token names. It refuses a missing,
// bad, revoked or expired token with ErrUnauthorized. On success it moves the token's expiry
// to now + Idle, never past its hard cap, and marks the device seen.
func (s *Store) Authenticate(ctx context.Context, authorization string) (Device, error) {
	token := bearer(authorization)
	if token == "" {
		return Device{}, ErrUnauthorized
	}
	now := s.now()
	var d Device
	err := pgx.BeginFunc(ctx, s.Pool, func(tx pgx.Tx) error {
		err := tx.QueryRow(ctx, `UPDATE device_tokens t SET expires_at = least($2::timestamptz + $3::interval, t.hard_cap_at)
			FROM devices d WHERE t.token_hash = $1 AND d.id = t.device_id AND t.revoked_at IS NULL
			AND d.revoked_at IS NULL AND t.expires_at > $2 AND t.hard_cap_at > $2
			RETURNING d.id::text, d.owner, d.name, d.capabilities`, hash(token), now, s.Idle).
			Scan(&d.ID, &d.Owner, &d.Name, &d.Capabilities)
		if errors.Is(err, pgx.ErrNoRows) {
			return ErrUnauthorized
		}
		if err != nil {
			return err
		}
		_, err = tx.Exec(ctx, `UPDATE devices SET last_seen_at = $2 WHERE id = $1`, d.ID, now)
		return err
	})
	return d, err
}

// Live says whether the token is live now, and whose device it is. It does not move the
// expiry: an open socket alone keeps no token alive.
func (s *Store) Live(ctx context.Context, authorization string) (string, bool, error) {
	token := bearer(authorization)
	if token == "" {
		return "", false, nil
	}
	var id string
	err := s.Pool.QueryRow(ctx, `SELECT d.id::text FROM device_tokens t JOIN devices d ON d.id = t.device_id
		WHERE t.token_hash = $1 AND t.revoked_at IS NULL AND d.revoked_at IS NULL
		AND t.expires_at > $2 AND t.hard_cap_at > $2`, hash(token), s.now()).Scan(&id)
	if errors.Is(err, pgx.ErrNoRows) {
		return "", false, nil
	}
	if err != nil {
		return "", false, err
	}
	return id, true, nil
}

// Online says whether the device made a request in the last OnlineWindow and is not revoked.
func (s *Store) Online(ctx context.Context, id string) (bool, error) {
	var online bool
	err := s.Pool.QueryRow(ctx, `SELECT EXISTS (SELECT 1 FROM devices WHERE id = $1::uuid
		AND revoked_at IS NULL AND last_seen_at > $2)`, id, s.now().Add(-OnlineWindow)).Scan(&online)
	return online, err
}

func (s *Store) online(lastSeen *time.Time) bool {
	return lastSeen != nil && lastSeen.After(s.now().Add(-OnlineWindow))
}
