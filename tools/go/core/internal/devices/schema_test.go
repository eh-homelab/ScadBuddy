package devices

import (
	"context"
	"errors"
	"strings"
	"testing"

	"github.com/jackc/pgx/v5/pgconn"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/testdb"
)

// One row in each table, and a call's args past 4 KiB is a check violation.
func TestDevicesSchema(t *testing.T) {
	pool := testdb.New(t)
	ctx := context.Background()
	var device, call string
	steps := []func() error{
		func() error {
			return pool.QueryRow(ctx, `INSERT INTO devices (owner, name, model, app_version, capabilities)
				VALUES ('user:i#s', 'Pixel', 'Pixel 9', '1.4.0', ARRAY['nfc.read']) RETURNING id::text`).Scan(&device)
		},
		func() error {
			_, err := pool.Exec(ctx, `INSERT INTO device_pairings (code_hash, owner, expires_at, device_id)
				VALUES (sha256('c'), 'user:i#s', now() + interval '10 minutes', $1::uuid)`, device)
			return err
		},
		func() error {
			_, err := pool.Exec(ctx, `INSERT INTO device_tokens (token_hash, device_id, expires_at, hard_cap_at)
				VALUES (sha256('t'), $1::uuid, now() + interval '1 hour', now() + interval '30 days')`, device)
			return err
		},
		func() error {
			return pool.QueryRow(ctx, `INSERT INTO device_calls (device_id, caller, caller_kind, method, args, timeout_s, deadline)
				VALUES ($1::uuid, 'user:i#s', 'person', 'nfc.read', '{}', 60, now() + interval '1 minute') RETURNING id::text`,
				device).Scan(&call)
		},
		func() error {
			_, err := pool.Exec(ctx, `INSERT INTO device_reads (device_id, call_id, kind, uid, tech, blob_key, media_type,
				complete, missing_blocks) VALUES ($1::uuid, $2::uuid, 'nfc', '9CC69CE3', 'mifare_classic', 'tag_dump-x',
				'application/json', true, '{}')`, device, call)
			return err
		},
	}
	for i, step := range steps {
		if err := step(); err != nil {
			t.Fatalf("step %d: %v", i, err)
		}
	}
	big := `{"x":"` + strings.Repeat("a", 5<<10) + `"}`
	_, err := pool.Exec(ctx, `INSERT INTO device_calls (device_id, caller, caller_kind, method, args, timeout_s, deadline)
		VALUES ($1::uuid, 'user:i#s', 'person', 'nfc.read', $2::jsonb, 60, now())`, device, big)
	var pg *pgconn.PgError
	if !errors.As(err, &pg) || pg.Code != "23514" {
		t.Fatalf("a 5 KiB args: %v, want a check violation", err)
	}
	_, err = pool.Exec(ctx, `INSERT INTO devices (owner, name, model, app_version, capabilities)
		VALUES ('u', 'n', '', '', ARRAY['location.get'])`)
	if !errors.As(err, &pg) || pg.Code != "23514" {
		t.Fatalf("an unknown capability: %v, want a check violation", err)
	}
}
