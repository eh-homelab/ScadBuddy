package store

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"slices"
	"sort"
	"strings"
	"unicode"
	"unicode/utf8"

	"github.com/jackc/pgx/v5"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/inventory/model"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/inventory/wire"
)

// An item's properties (R-K1-33; spec §3 "Required properties"): an item create or save
// writes its property patch in its own transaction, then refuses the item when it is not
// complete. The value rules are ports of entities/properties.py, one function per Python
// function. Go ports them and does not call Python: an activity has no client of the API,
// and the API calls Go, not the reverse (§6.1). testdata/property_values.json holds the
// vectors that both languages run, so a port that drifts fails a test.

const (
	// upsertPropertySQL and deletePropertySQL are entities/shared/entity_property_upsert.sql
	// and entity_property_delete.sql, which entities/properties.py apply_on runs (#7667 §5.1).
	upsertPropertySQL = `INSERT INTO entity_properties (entity_id, key, value, source, updated_by)
VALUES ($1, $2, $3, 'manual', $4) ON CONFLICT (entity_id, key) DO UPDATE
SET value = EXCLUDED.value, source = 'manual',
updated_at = clock_timestamp(), updated_by = EXCLUDED.updated_by`
	deletePropertySQL = `DELETE FROM entity_properties WHERE entity_id = $1 AND key = $2`

	// maxKeys is entities/properties.py MAX_KEYS: the keys an entity may have.
	maxKeys = 200
	// nulProblem is entities/properties.py NUL_PROBLEM.
	nulProblem = "a string may not hold U+0000"
)

// Invalid is a key whose stored value does not fit its subschema, with why:
// does not fit '<keyword>'.
type Invalid struct {
	Key    string `json:"key"`
	Reason string `json:"reason"`
}

func badValue(key, reason string) error {
	return &Refused{Status: 422, Code: "bad_property_value", Detail: fmt.Sprintf("property '%s': %s", key, reason),
		Type: entityProblems + "bad_property_value"}
}

// shapeProblem is _shape_problem: why v is none of the four shapes (a finite number that
// fits a double, a string, a boolean, a list of strings), or "". A number is a
// json.Number.
func shapeProblem(v any) string {
	switch x := v.(type) {
	case bool:
		return ""
	case json.Number:
		if _, ok := number(x); !ok {
			return "a number must be finite and fit a double"
		}
		return ""
	case string:
		if strings.ContainsRune(x, 0) {
			return nulProblem
		}
		if utf8.RuneCountInString(x) > maxString {
			return fmt.Sprintf("at most %d characters", maxString)
		}
		return ""
	case []any:
		if len(x) > maxList {
			return fmt.Sprintf("a list holds at most %d strings", maxList)
		}
		for _, e := range x {
			s, ok := e.(string)
			if !ok {
				return "a list holds strings only"
			}
			if strings.ContainsRune(s, 0) {
				return nulProblem
			}
			if utf8.RuneCountInString(s) > maxListItem {
				return fmt.Sprintf("a list's string is at most %d characters", maxListItem)
			}
		}
		return ""
	}
	return "a value is a string, a number, a boolean or a list of strings"
}

// hasValue is has_value: false and 0 are values; null, a string that is empty or only
// white space, and an empty list are not.
func hasValue(v any) bool {
	switch x := v.(type) {
	case nil:
		return false
	case string:
		return strings.TrimFunc(x, pySpace) != ""
	case []any:
		return len(x) > 0
	}
	return true
}

// pySpace is Python's str.isspace for one character: Unicode white space, and also the
// separators U+001C to U+001F, which Python's str.strip removes too.
func pySpace(r rune) bool {
	return unicode.IsSpace(r) || r >= 0x1c && r <= 0x1f
}

// subschemas is each key's subschema of props, decoded with its numbers as json.Number.
// A subschema that is not a JSON object is left out: checkSchema takes none.
func subschemas(props map[string]json.RawMessage) map[string]map[string]any {
	out := map[string]map[string]any{}
	for key, raw := range props {
		v, err := decodeJSON(raw)
		if sub, ok := v.(map[string]any); err == nil && ok {
			out[key] = sub
		}
	}
	return out
}

// completeness is completeness: the required keys with no value, and each key that
// schema defines whose value is not null and does not fit. A key that no schema defines
// never makes an item incomplete. Both lists are sorted by key and never nil.
func completeness(schema map[string]json.RawMessage, required []string, values map[string]any) (missing []string, invalid []Invalid) {
	missing, invalid = []string{}, []Invalid{}
	for _, key := range required {
		if !hasValue(values[key]) && !slices.Contains(missing, key) {
			missing = append(missing, key)
		}
	}
	sort.Strings(missing)
	subs := subschemas(schema)
	for _, key := range sortedKeys(values) {
		sub, known := subs[key]
		if !known || values[key] == nil {
			continue
		}
		if keyword, ok := fits(values[key], sub); !ok {
			invalid = append(invalid, Invalid{Key: key, Reason: "does not fit '" + keyword + "'"})
		}
	}
	return missing, invalid
}

// itemSchema is what an item of the chain is checked against: its kinds' merged
// properties, and their merged required keys with its special type's
// requires_properties.
func itemSchema(cat Catalog, chain model.Chain) (props map[string]json.RawMessage, required []string) {
	props, required = mergedSchema(chain)
	if _, special, err := chain.Class(); err == nil && special != "" {
		if sp, ok := cat.SpecialType(special); ok {
			required = append(slices.Clone(required), sp.RequiresProperties...)
		}
	}
	return props, required
}

// ApplyProperties is apply_on for the item id (the entity item:<id>), in the caller's
// transaction, which store.Tx began: the event log's lock and the actor are already held,
// so the layer 1 triggers publish one entity_property.changed per key that changes. props
// is the item's merged schema. Every key and value is checked before anything is written:
// a key outside ^[a-z][a-z0-9_]{0,63}$, a value of no allowed shape, or a value of a
// defined key that does not fit is 422 bad_property_value. More than maxKeys keys after
// the patch, and more than before it, is bad_property_value with key '*'. As apply_on, a
// patch that only removes keys makes no entities row. The keys are written in key order.
func ApplyProperties(ctx context.Context, tx pgx.Tx, id string, props map[string]json.RawMessage, patch wire.PropertyPatch, by Actor) error {
	if len(patch) == 0 {
		return nil
	}
	subs := subschemas(props)
	keys := sortedKeys(patch)
	removeOnly := true
	for _, key := range keys {
		if !propertyKey.MatchString(key) {
			return badValue(key, "a key is ^[a-z][a-z0-9_]{0,63}$")
		}
		v, err := decodeJSON(patch[key])
		if err != nil {
			return badValue(key, "a value is a string, a number, a boolean or a list of strings")
		}
		if v == nil {
			continue
		}
		removeOnly = false
		if problem := shapeProblem(v); problem != "" {
			return badValue(key, problem)
		}
		if sub, ok := subs[key]; ok {
			if keyword, ok := fits(v, sub); !ok {
				return badValue(key, "does not fit '"+keyword+"'")
			}
		}
	}
	var entity string
	if removeOnly {
		err := tx.QueryRow(ctx, `SELECT id FROM entities WHERE kind = 'item' AND ref = $1`, id).Scan(&entity)
		if errors.Is(err, pgx.ErrNoRows) {
			return nil
		}
		if err != nil {
			return err
		}
	} else {
		var err error
		if entity, err = EnsureEntity(ctx, tx, "item", id); err != nil {
			return err
		}
	}
	// One writer at a time per entity, so the key count holds.
	if _, err := tx.Exec(ctx, `SELECT 1 FROM entities WHERE id = $1 FOR UPDATE`, entity); err != nil {
		return err
	}
	keyCount := func() (int, error) {
		var n int
		err := tx.QueryRow(ctx, `SELECT count(*) FROM entity_properties WHERE entity_id = $1`, entity).Scan(&n)
		return n, err
	}
	before, err := keyCount()
	if err != nil {
		return err
	}
	for _, key := range keys {
		if v, _ := decodeJSON(patch[key]); v == nil {
			_, err = tx.Exec(ctx, deletePropertySQL, entity, key)
		} else {
			_, err = tx.Exec(ctx, upsertPropertySQL, entity, key, patch[key], by)
		}
		if err != nil {
			return err
		}
	}
	after, err := keyCount()
	if err != nil {
		return err
	}
	if after > max(maxKeys, before) {
		return badValue("*", fmt.Sprintf("an entity has at most %d keys", maxKeys))
	}
	return nil
}

// checkItem is check_row for the item id: its stored values against its chain's merged
// schema and its special type's requires_properties (completeness). An item that is not
// complete is 422 missing_required_properties, with missing (the keys) and invalid (each
// key with its reason), as the spool routes answer.
func checkItem(ctx context.Context, tx pgx.Tx, cat Catalog, set kindSet, id string) error {
	var kind string
	if err := tx.QueryRow(ctx, `SELECT kind_id FROM items WHERE id = $1::uuid`, id).Scan(&kind); err != nil {
		return err
	}
	values, err := propertyValues(ctx, tx, []string{id})
	if err != nil {
		return err
	}
	props, required := itemSchema(cat, set.chain(kind))
	missing, invalid := completeness(props, required, values[id])
	if len(missing) == 0 && len(invalid) == 0 {
		return nil
	}
	detail := "missing required properties: " + strings.Join(missing, ", ")
	if len(missing) == 0 {
		keys := make([]string, len(invalid))
		for i, v := range invalid {
			keys[i] = v.Key
		}
		detail = "properties that do not fit: " + strings.Join(keys, ", ")
	}
	return &Refused{Status: 422, Code: "missing_required_properties", Detail: detail,
		Type: entityProblems + "missing_required_properties", Fields: map[string]any{"missing": missing, "invalid": invalid}}
}

// propertyValues is the stored values of each item, by item id and key, with numbers as
// json.Number.
func propertyValues(ctx context.Context, q Querier, itemIDs []string) (map[string]map[string]any, error) {
	rows, err := q.Query(ctx, `SELECT e.ref, p.key, p.value FROM entity_properties p
		JOIN entities e ON e.id = p.entity_id AND e.kind = 'item' WHERE e.ref = ANY($1)`, itemIDs)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := map[string]map[string]any{}
	for rows.Next() {
		var item, key string
		var raw []byte
		if err := rows.Scan(&item, &key, &raw); err != nil {
			return nil, err
		}
		v, err := decodeJSON(raw)
		if err != nil {
			return nil, err
		}
		if out[item] == nil {
			out[item] = map[string]any{}
		}
		out[item][key] = v
	}
	return out, rows.Err()
}
