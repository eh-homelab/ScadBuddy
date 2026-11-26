package store

import (
	"context"
	"strings"

	"github.com/jackc/pgx/v5"
)

// An item's compatible_with codes are relations to model kinds (design §5, R-K1-11): each
// code names a kind under printer, made when it is missing, and the item has one open
// compatible_with relation to each.

// modelKindID is the §8 id rule, as the migration's model_codes step writes it: printer_ and
// the code in lower case, runs of other characters as _, at most 32.
const modelKindID = `'printer_' || left(regexp_replace(lower(btrim($1)), '[^a-z0-9]+', '_', 'g'), 32)`

// modelKind is the id of the code's model kind, made under printer (named the code) when
// it is missing. A new kind publishes its handler_config.changed events, as a kind save does.
func modelKind(ctx context.Context, tx pgx.Tx, cat Catalog, code string, by Actor) (string, error) {
	var id string
	var made bool
	err := tx.QueryRow(ctx, `WITH made AS (
			INSERT INTO item_kinds (id, name, parent_id, tracking, life_basis)
			VALUES (`+modelKindID+`, left(btrim($1), 100), 'printer', 'unit', 'none')
			ON CONFLICT (id) DO NOTHING RETURNING id)
		SELECT `+modelKindID+`, EXISTS (SELECT 1 FROM made)`, code).Scan(&id, &made)
	if err != nil || !made {
		return id, err
	}
	set, err := loadKinds(ctx, tx)
	if err != nil {
		return "", err
	}
	if err := rewriteDerived(ctx, tx, cat, set, []string{id}); err != nil {
		return "", err
	}
	_, _, res, _ := derived(cat, set.chain(id))
	return id, publishConfig(ctx, tx, "item_kind:"+id, nil, res.Steps, by)
}

// setCompatible makes the item's open compatible_with relations exactly those to its codes'
// model kinds, in the codes' order; a relation to another kind ends. A blank code is
// skipped.
func setCompatible(ctx context.Context, tx pgx.Tx, cat Catalog, itemID string, codes []string, by Actor) error {
	targets := []string{} // not nil: = ANY(NULL) would end nothing
	for _, code := range codes {
		if strings.TrimSpace(code) == "" {
			continue
		}
		kind, err := modelKind(ctx, tx, cat, code, by)
		if err != nil {
			return err
		}
		entity, err := EnsureEntity(ctx, tx, "item_kind", kind)
		if err != nil {
			return err
		}
		targets = append(targets, entity)
	}
	if _, err := tx.Exec(ctx, `UPDATE relations r SET ended_at = clock_timestamp()
		FROM entities e WHERE e.id = r.from_entity AND e.kind = 'item' AND e.ref = $1
		AND r.type_id = 'compatible_with' AND r.ended_at IS NULL AND NOT (r.to_entity = ANY($2::uuid[]))`,
		itemID, targets); err != nil {
		return err
	}
	if len(targets) == 0 {
		return nil
	}
	from, err := EnsureEntity(ctx, tx, "item", itemID)
	if err != nil {
		return err
	}
	actor, err := ReadActor(by)
	if err != nil {
		return err
	}
	for _, to := range targets {
		if _, err := tx.Exec(ctx, `INSERT INTO relations (type_id, from_entity, to_entity, created_by)
			VALUES ('compatible_with', $1, $2, $3)
			ON CONFLICT (type_id, from_entity, to_entity) WHERE ended_at IS NULL DO NOTHING`,
			from, to, string(actor)); err != nil {
			return err
		}
	}
	return nil
}

// compatibleWith is each item's model kinds' names, by item id, in the order the relations
// were made.
func compatibleWith(ctx context.Context, q Querier, itemIDs []string) (map[string][]string, error) {
	rows, err := q.Query(ctx, `SELECT f.ref, k.name FROM relations r
		JOIN entities f ON f.id = r.from_entity AND f.kind = 'item'
		JOIN entities t ON t.id = r.to_entity AND t.kind = 'item_kind'
		JOIN item_kinds k ON k.id = t.ref
		WHERE r.type_id = 'compatible_with' AND r.ended_at IS NULL AND f.ref = ANY($1)
		ORDER BY r.created_at, k.name`, itemIDs)
	if err != nil {
		return nil, err
	}
	out := map[string][]string{}
	for rows.Next() {
		var item, name string
		if err := rows.Scan(&item, &name); err != nil {
			rows.Close()
			return nil, err
		}
		out[item] = append(out[item], name)
	}
	return out, rows.Err()
}
