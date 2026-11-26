// Package flowgrants holds the one check of a run grant's chain that core's routes share,
// the Go copy of backend flows/store.py chain_in_force (#5685).
package flowgrants

import (
	"context"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

// Querier is a pool, a connection or a transaction.
type Querier interface {
	QueryRow(ctx context.Context, sql string, args ...any) pgx.Row
}

// links is the ids of grant $1, every grant it inherits from, and the standing grants they
// were seeded from.
const links = `WITH RECURSIVE chain AS (
	SELECT id, parent_grant_id, standing_grant_id, 0 AS depth FROM flow_grants WHERE id = $1
	UNION ALL SELECT g.id, g.parent_grant_id, g.standing_grant_id, c.depth + 1
	FROM flow_grants g JOIN chain c ON g.id = c.parent_grant_id WHERE c.depth < 64)
SELECT coalesce(array_agg(id), '{}'),
	coalesce(array_agg(standing_grant_id) FILTER (WHERE standing_grant_id IS NOT NULL), '{}')
FROM chain`

// chain is grant $1 and every grant it inherits from (parent_grant_id, at most 64 deep),
// each with whether it is in force and whether it reached its root. Both expiries are
// read by the database's clock (#7890).
const chain = `WITH RECURSIVE chain AS (
	SELECT id, parent_grant_id, status, revoked_at, expires_at, standing_grant_id,
		standing_approval_id, 0 AS depth FROM flow_grants WHERE id = $1
	UNION ALL SELECT g.id, g.parent_grant_id, g.status, g.revoked_at, g.expires_at,
		g.standing_grant_id, g.standing_approval_id, c.depth + 1
	FROM flow_grants g JOIN chain c ON g.id = c.parent_grant_id WHERE c.depth < 64)
SELECT coalesce(bool_and(c.status = 'approved' AND c.revoked_at IS NULL
		AND c.expires_at IS NOT NULL AND c.expires_at > now()
		AND (c.standing_grant_id IS NULL OR (t.status = 'approved' AND t.revoked_at IS NULL
			AND t.expires_at > now() AND t.approval_id = c.standing_approval_id) IS TRUE)), false)
	AND coalesce(bool_or(c.parent_grant_id IS NULL), false)
FROM chain c LEFT JOIN flow_trigger_grants t ON t.id = c.standing_grant_id`

// ChainInForce reports whether grant id and every grant it inherits from is in force, and
// the standing grant any of them was seeded from is still in force under the approval
// that seeded it (#4252). A grant that does not exist is not in force.
func ChainInForce(ctx context.Context, q Querier, id string) (bool, error) {
	var held bool
	err := q.QueryRow(ctx, chain, id).Scan(&held)
	return held, err
}

// Locker is a transaction.
type Locker interface {
	Querier
	Exec(ctx context.Context, sql string, args ...any) (pgconn.CommandTag, error)
}

// LockChainInForce is ChainInForce inside a transaction, with a share lock on every row the
// answer reads: the grant, the grants it inherits from and the standing grants they were
// seeded from. A revocation of any of them lands wholly before or after the caller's
// transaction (#5685).
func LockChainInForce(ctx context.Context, tx Locker, id string) (bool, error) {
	var grants, standing []string
	if err := tx.QueryRow(ctx, links, id).Scan(&grants, &standing); err != nil {
		return false, err
	}
	if _, err := tx.Exec(ctx, `SELECT 1 FROM flow_grants WHERE id = ANY($1) FOR SHARE`, grants); err != nil {
		return false, err
	}
	if _, err := tx.Exec(ctx, `SELECT 1 FROM flow_trigger_grants WHERE id = ANY($1) FOR SHARE`, standing); err != nil {
		return false, err
	}
	return ChainInForce(ctx, tx, id)
}
