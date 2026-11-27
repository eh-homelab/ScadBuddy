package devices

import (
	"net/http"
	"slices"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/bus"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
)

// Listed is one device in GET /api/v1/devices. ExpiresAt and HardCapAt are its live token's.
type Listed struct {
	ID           string     `json:"id"`
	Name         string     `json:"name"`
	Model        string     `json:"model"`
	AppVersion   string     `json:"app_version"`
	Capabilities []string   `json:"capabilities"`
	CreatedAt    crud.Time  `json:"created_at"`
	LastSeenAt   *crud.Time `json:"last_seen_at"`
	Online       bool       `json:"online"`
	ExpiresAt    *crud.Time `json:"expires_at"`
	HardCapAt    *crud.Time `json:"hard_cap_at"`
}

const listMax = 100

// list is the caller's own devices that are not revoked, the newest first.
func (s *Store) list(r *http.Request) (any, error) {
	rows, err := s.Pool.Query(r.Context(), `SELECT d.id::text, d.name, d.model, d.app_version, d.capabilities,
		d.created_at, d.last_seen_at, t.expires_at, t.hard_cap_at
		FROM devices d LEFT JOIN LATERAL (SELECT expires_at, hard_cap_at FROM device_tokens
			WHERE device_id = d.id AND revoked_at IS NULL AND expires_at > $2 AND hard_cap_at > $2
			ORDER BY expires_at DESC LIMIT 1) t ON true
		WHERE d.owner = $1 AND d.revoked_at IS NULL ORDER BY d.created_at DESC, d.id LIMIT $3`,
		principal(r.Context()), s.now(), listMax)
	if err != nil {
		return nil, err
	}
	out := []Listed{}
	var (
		l                      Listed
		created                time.Time
		seen, expires, hardCap *time.Time
	)
	_, err = pgx.ForEachRow(rows, []any{&l.ID, &l.Name, &l.Model, &l.AppVersion, &l.Capabilities,
		&created, &seen, &expires, &hardCap}, func() error {
		l.CreatedAt, l.LastSeenAt, l.ExpiresAt, l.HardCapAt = crud.Time(created), crud.TimeOf(seen),
			crud.TimeOf(expires), crud.TimeOf(hardCap)
		l.Online = expires != nil && s.online(seen)
		l.Capabilities = slices.Clone(l.Capabilities)
		out = append(out, l)
		return nil
	})
	if err != nil {
		return nil, err
	}
	return map[string]any{"devices": out}, nil
}

// revoke ends a pairing at once: the device and its tokens are revoked, its open calls end
// offline, and device.revoked closes its realtime socket on every pod.
func (s *Store) revoke(r *http.Request) (any, error) {
	id := r.PathValue("id")
	if !crud.IsUUID(id) {
		return nil, notFound("device")
	}
	ctx := r.Context()
	now := s.now()
	var ended []string
	err := pgx.BeginFunc(ctx, s.Pool, func(tx pgx.Tx) error {
		tag, err := tx.Exec(ctx, `UPDATE devices SET revoked_at = $3 WHERE id = $1::uuid AND owner = $2
			AND revoked_at IS NULL`, id, principal(ctx), now)
		if err != nil {
			return err
		}
		if tag.RowsAffected() == 0 {
			return notFound("device")
		}
		if _, err := tx.Exec(ctx, `UPDATE device_tokens SET revoked_at = $2 WHERE device_id = $1::uuid
			AND revoked_at IS NULL`, id, now); err != nil {
			return err
		}
		rows, err := tx.Query(ctx, `UPDATE device_calls SET outcome = 'offline', ended_at = $2
			WHERE device_id = $1::uuid AND outcome IS NULL RETURNING id::text`, id, now)
		if err != nil {
			return err
		}
		if ended, err = pgx.CollectRows(rows, pgx.RowTo[string]); err != nil {
			return err
		}
		// An ended call's approval can do nothing now: it leaves the pending actions.
		if _, err := tx.Exec(ctx, `UPDATE ai_approvals SET decision = 'cancelled', decided_at = $2
			WHERE decision IS NULL AND id IN (SELECT approval_id FROM device_calls
				WHERE id = ANY($1::uuid[]) AND approval_id IS NOT NULL)`, ended, now); err != nil {
			return err
		}
		_, err = tx.Exec(ctx, `UPDATE ai_approvals SET revoked_at = $2
			WHERE decision = 'approved' AND consumed_at IS NULL AND revoked_at IS NULL AND id IN (
				SELECT approval_id FROM device_calls WHERE id = ANY($1::uuid[]) AND approval_id IS NOT NULL)`, ended, now)
		return err
	})
	if err != nil {
		return nil, err
	}
	bus.Publish(ctx, s.Pool, "device.revoked", httpx.Field{Key: "device", Value: id},
		httpx.Field{Key: "topics", Value: []string{"device:" + id}})
	for _, call := range ended {
		s.publishFailed(ctx, call, id, "offline", "the device was revoked")
	}
	return &crud.Reply{Status: http.StatusNoContent}, nil
}
