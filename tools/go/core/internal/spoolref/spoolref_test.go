package spoolref

import (
	"context"
	"errors"
	"testing"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/testdb"
)

func TestMain(m *testing.M) { testdb.Main(m) }

func TestSpoolrefFind(t *testing.T) {
	pool := testdb.New(t)
	ctx := context.Background()
	// Numbers are identity values, so insert 12 spools to reach S-12 and read each id back.
	var ids []string
	for range 13 {
		var id string
		if err := pool.QueryRow(ctx, `INSERT INTO filament_spools (material_id)
			VALUES ((SELECT id FROM filament_materials WHERE name = 'PLA')) RETURNING id::text`).Scan(&id); err != nil {
			t.Fatal(err)
		}
		ids = append(ids, id)
	}
	twelve, thirteen := ids[11], ids[12]
	if _, err := pool.Exec(ctx, `UPDATE filament_spools SET archived_at = now() WHERE id = $1`, thirteen); err != nil {
		t.Fatal(err)
	}

	for _, c := range []string{"s-12", "S12", " S-12 ", "S-012"} {
		got, err := Find(ctx, pool, Ref{Code: c})
		if err != nil || got.ID != twelve || got.Number != 12 {
			t.Errorf("code %q: %+v %v", c, got, err)
		}
	}
	if got, err := Find(ctx, pool, Ref{ID: twelve}); err != nil || got.Number != 12 {
		t.Errorf("by id: %+v %v", got, err)
	}
	for _, c := range []string{"L-3", "x", "S-999999", "", "S-0", "S-99999999999"} {
		if _, err := Find(ctx, pool, Ref{Code: c}); !errors.Is(err, ErrCodeUnknown) {
			t.Errorf("code %q: %v", c, err)
		}
	}
	for _, id := range []string{"5d0c8f1e-0000-4000-8000-000000000000", "not-a-uuid"} {
		if _, err := Find(ctx, pool, Ref{ID: id}); !errors.Is(err, ErrNotFound) {
			t.Errorf("id %q: %v", id, err)
		}
	}
	if _, err := Find(ctx, pool, Ref{ID: thirteen}); !errors.Is(err, ErrArchived) {
		t.Errorf("archived by id: %v", err)
	}
	if _, err := Find(ctx, pool, Ref{Code: "S-13"}); !errors.Is(err, ErrArchived) {
		t.Errorf("archived by code: %v", err)
	}
}
