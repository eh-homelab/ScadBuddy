package store

import (
	"context"

	"github.com/jackc/pgx/v5"
)

// ensureEntitySQL is entities/shared/ensure_entity.sql, which entities/registry.py
// EntityStore.ensure runs (#7667 §5.1).
const ensureEntitySQL = `INSERT INTO entities (kind, ref) VALUES ($1, $2)
ON CONFLICT (kind, ref) DO UPDATE SET kind = EXCLUDED.kind RETURNING id`

// EnsureEntity is the id of the entities row of kind and ref, made on first use. Unlike
// EntityStore.ensure it does not check that the owner's row exists; the caller has read it.
func EnsureEntity(ctx context.Context, tx pgx.Tx, kind, ref string) (string, error) {
	var id string
	err := tx.QueryRow(ctx, ensureEntitySQL, kind, ref).Scan(&id)
	return id, err
}
