package devices

import (
	"context"
	"net/http"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
)

// heartbeat is the device's POST every 30 s while the app is in front: Authenticate has moved
// its token's expiry and its last_seen_at already. A device is online while its last request
// is under OnlineWindow old.
func (s *Store) heartbeat(_ *http.Request, _ Device) (any, error) {
	return &crud.Reply{Status: http.StatusNoContent}, nil
}

// DeviceOf is the device a realtime handshake's token names (realtime.DeviceTokens): its
// expiry moves, as at every device request.
func (s *Store) DeviceOf(ctx context.Context, authorization string) (string, error) {
	d, err := s.Authenticate(ctx, authorization)
	return d.ID, err
}

// CallVisible says whether principal owns the device of the call, or made the call: who may
// follow the call's events on the realtime socket.
func (s *Store) CallVisible(ctx context.Context, call, principal string) bool {
	if !crud.IsUUID(call) {
		return false
	}
	var visible bool
	err := s.Pool.QueryRow(ctx, `SELECT EXISTS (SELECT 1 FROM device_calls c JOIN devices d ON d.id = c.device_id
		WHERE c.id = $1::uuid AND (d.owner = $2 OR c.caller = $2))`, call, principal).Scan(&visible)
	if err != nil {
		logFailed(ctx, "could not read who may follow a call", err)
		return false
	}
	return visible
}
