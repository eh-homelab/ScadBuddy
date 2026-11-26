package store

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"regexp"
	"slices"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/inventory/handlers"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/inventory/model"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/inventory/wire"
)

// Items (design §4.2; phase 2 inventory/items.py): the catalog entry of a thing, of one
// kind. A save resolves the item's steps over its kind's (Resolve: the class must be
// complete); its attributes are its entity properties (item:<id>), which it may still lack:
// those are read, never refused (R-K1-8).

// refusedSpecial is the special types whose items are refused until their phase builds
// their handlers (phase 2 Ruling 9; filament items wait for K3).
var refusedSpecial = map[string]*Refused{
	"filament_spool": {Status: 409, Code: "filament_item_refused",
		Detail: "filament items and their offers wait for the filament phase (#7068)"},
}

// editable is phase 2's _EDITABLE: the fields an import owns until a person changes them.
var editable = []string{"name", "manufacturer", "compatible_with", "low_stock", "life_hours", "life_months", "saleable", "note"}

var uuidText = regexp.MustCompile(`^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$`)

// Items is the item store.
type Items struct {
	Cat Catalog
}

// ItemFilter is phase 2's list filters. Kind takes its descendants too unless Descendants
// is false; Q matches the name, the manufacturer or a property value, in any case; Low only
// items at or below their low_stock; Compatible those related to the model kind of that
// code (by its name or the §8 id rule); Archived the archived ones too. Limit is 1 to 5000;
// 0 is 1000.
type ItemFilter struct {
	Kind        string
	Descendants *bool
	Q           string
	Low         bool
	Archived    bool
	Compatible  string
	Limit       int
}

func itemNotFound(id string) error {
	return &Refused{Status: 404, Code: "item_not_found", Detail: fmt.Sprintf("no item: %s", id)}
}

func itemExists(name string) error {
	return &Refused{Status: 409, Code: "item_exists",
		Detail: "an unarchived item of the kind has that name in any case, or that import ref: " + name}
}

// itemsBase is phase 2's _ITEMS: every item with its kind's figures, its stock and its
// unarchived offers' count (R-P2-37). A lot counts at its on_hand, its value and unknown
// cost only while it holds stock; a unit counts while in_stock.
const itemsBase = `
SELECT i.id, i.kind_id, k.tracking, k.uom, i.name, i.manufacturer, i.low_stock, i.saleable,
    coalesce(i.saleable, k.saleable) AS effective_saleable, i.source, i.source_ref,
    i.edited_fields, i.note, i.archived_at, i.steps,
    CASE k.tracking WHEN 'lot' THEN l.on_hand WHEN 'unit' THEN u.on_hand ELSE 0 END AS on_hand,
    CASE k.tracking WHEN 'lot' THEN l.value WHEN 'unit' THEN u.value ELSE 0 END AS stock_value_minor,
    CASE k.tracking WHEN 'lot' THEN l.unknown WHEN 'unit' THEN u.unknown ELSE false END AS cost_unknown,
    o.offer_count
FROM items i
JOIN item_kinds k ON k.id = i.kind_id
CROSS JOIN LATERAL (
    SELECT coalesce(sum(on_hand), 0) AS on_hand,
        coalesce(sum(on_hand * unit_cost_minor) FILTER (WHERE on_hand > 0), 0) AS value,
        coalesce(bool_or(cost_unknown) FILTER (WHERE on_hand > 0), false) AS unknown
    FROM stock_lots WHERE item_id = i.id
) l
CROSS JOIN LATERAL (
    SELECT count(*) AS on_hand, coalesce(sum(cost_minor), 0) AS value,
        coalesce(bool_or(cost_unknown), false) AS unknown
    FROM stock_units WHERE item_id = i.id AND status = 'in_stock'
) u
CROSS JOIN LATERAL (
    SELECT count(*) AS offer_count FROM supplier_offers
    WHERE item_id = i.id AND archived_at IS NULL
) o`

// itemColumns are an item as the wire reads it: decimals as plain text (trim_scale, as
// Python's format(value.normalize(), "f")), life_hours and life_months from the item's own
// steps.life (R-K1-6), and low (never for a kind that is not counted).
const itemColumns = `s.id::text, s.kind_id, s.tracking, s.uom, s.name, s.manufacturer,
    trim_scale(s.low_stock)::text, s.saleable, s.effective_saleable, s.source, s.source_ref,
    s.edited_fields, s.note, s.archived_at, s.steps, trim_scale(s.on_hand)::text,
    trim_scale(s.stock_value_minor)::text, s.cost_unknown, s.offer_count,
    CASE WHEN jsonb_typeof(s.steps #> '{life,params,hours}') = 'number'
        THEN trim_scale((s.steps #>> '{life,params,hours}')::numeric)::text END,
    CASE WHEN jsonb_typeof(s.steps #> '{life,params,months}') = 'number'
        THEN (s.steps #>> '{life,params,months}')::numeric::bigint END,
    ` + lowSQL

// lowSQL is phase 2's _LOW: low_stock > 0 and on_hand <= low_stock, for a counted kind.
const lowSQL = `(s.tracking IN ('lot', 'unit') AND s.low_stock > 0 AND s.on_hand <= s.low_stock)`

func escapeLike(s string) string {
	return strings.NewReplacer(`\`, `\\`, `%`, `\%`, `_`, `\_`).Replace(s)
}

// List is items by name, up to f.Limit.
func (s *Items) List(ctx context.Context, q Querier, f ItemFilter) ([]wire.Item, error) {
	if f.Limit == 0 {
		f.Limit = 1000
	}
	if f.Limit < 1 || f.Limit > 5000 {
		return nil, &Refused{Status: 422, Code: "bad_limit", Detail: fmt.Sprintf("a limit is 1 to 5000, not %d", f.Limit)}
	}
	var where []string
	var args []any
	arg := func(v any) string {
		args = append(args, v)
		return "$" + strconv.Itoa(len(args))
	}
	if !f.Archived {
		where = append(where, "s.archived_at IS NULL")
	}
	if f.Kind != "" {
		if f.Descendants == nil || *f.Descendants {
			where = append(where, "s.kind_id IN (WITH RECURSIVE down (id, depth) AS ("+
				" SELECT id, 0 FROM item_kinds WHERE id = "+arg(f.Kind)+" UNION ALL"+
				" SELECT k.id, down.depth + 1 FROM item_kinds k"+
				" JOIN down ON k.parent_id = down.id WHERE down.depth < 100) SELECT id FROM down)")
		} else {
			where = append(where, "s.kind_id = "+arg(f.Kind))
		}
	}
	if f.Q != "" {
		like := arg("%" + escapeLike(f.Q) + "%")
		where = append(where, "(s.name ILIKE "+like+" OR s.manufacturer ILIKE "+like+" OR EXISTS (SELECT 1"+
			" FROM entity_properties p JOIN entities e ON e.id = p.entity_id"+
			" WHERE e.kind = 'item' AND e.ref = s.id::text AND CASE jsonb_typeof(p.value)"+
			" WHEN 'array' THEN EXISTS (SELECT 1 FROM jsonb_array_elements_text(p.value) v"+
			" WHERE v ILIKE "+like+") ELSE p.value #>> '{}' ILIKE "+like+" END))")
	}
	if f.Low {
		where = append(where, lowSQL)
	}
	if f.Compatible != "" {
		code := arg(f.Compatible)
		where = append(where, "EXISTS (SELECT 1 FROM relations r"+
			" JOIN entities fe ON fe.id = r.from_entity AND fe.kind = 'item' AND fe.ref = s.id::text"+
			" JOIN entities te ON te.id = r.to_entity AND te.kind = 'item_kind'"+
			" JOIN item_kinds mk ON mk.id = te.ref"+
			" WHERE r.type_id = 'compatible_with' AND r.ended_at IS NULL"+
			" AND (mk.name = "+code+" OR mk.id = "+strings.ReplaceAll(modelKindID, "$1", code)+"))")
	}
	clause := ""
	if len(where) > 0 {
		clause = " WHERE " + strings.Join(where, " AND ")
	}
	return s.read(ctx, q, clause+" ORDER BY lower(s.name), s.id LIMIT "+arg(f.Limit), args...)
}

// Get is the item, or item_not_found.
func (s *Items) Get(ctx context.Context, q Querier, id string) (wire.Item, error) {
	if !uuidText.MatchString(id) {
		return wire.Item{}, itemNotFound(id)
	}
	out, err := s.read(ctx, q, " WHERE s.id = $1::uuid", id)
	if err != nil {
		return wire.Item{}, err
	}
	if len(out) == 0 {
		return wire.Item{}, itemNotFound(id)
	}
	return out[0], nil
}

func (s *Items) read(ctx context.Context, q Querier, clause string, args ...any) ([]wire.Item, error) {
	set, err := loadKinds(ctx, q)
	if err != nil {
		return nil, err
	}
	rows, err := q.Query(ctx, "SELECT "+itemColumns+" FROM ("+itemsBase+") s"+clause, args...)
	if err != nil {
		return nil, err
	}
	out := []wire.Item{}
	for rows.Next() {
		var it wire.Item
		var steps []byte
		if err := rows.Scan(&it.ID, &it.KindID, &it.Tracking, &it.UOM, &it.Name, &it.Manufacturer, &it.LowStock,
			&it.Saleable, &it.EffectiveSaleable, &it.Source, &it.SourceRef, &it.EditedFields, &it.Note,
			&it.ArchivedAt, &steps, &it.OnHand, &it.StockValueMinor, &it.CostUnknown, &it.OfferCount,
			&it.LifeHours, &it.LifeMonths, &it.Low); err != nil {
			rows.Close()
			return nil, err
		}
		if err := json.Unmarshal(steps, &it.Steps); err != nil {
			rows.Close()
			return nil, err
		}
		out = append(out, it)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	ids := make([]string, len(out))
	for i, it := range out {
		ids[i] = it.ID
	}
	compatible, err := compatibleWith(ctx, q, ids)
	if err != nil {
		return nil, err
	}
	present, err := propertiesWithValues(ctx, q, ids)
	if err != nil {
		return nil, err
	}
	for i := range out {
		s.derive(&out[i], set, compatible[out[i].ID], present[out[i].ID])
	}
	return out, nil
}

// derive fills what an item reads but does not store: its kind path, class, special type,
// incomplete steps and missing properties (R-K1-8), and compatible_with (R-K1-11).
func (s *Items) derive(it *wire.Item, set kindSet, compatible []string, present map[string]bool) {
	chain := set.chain(it.KindID)
	it.KindPath = set.path(it.KindID)
	if it.Steps == nil {
		it.Steps = model.Steps{}
	}
	if it.EditedFields == nil {
		it.EditedFields = []string{}
	}
	it.CompatibleWith = compatible
	if it.CompatibleWith == nil {
		it.CompatibleWith = []string{}
	}
	it.IncompleteSteps = []string{}
	cls, special, err := chain.Class()
	if err == nil {
		it.Class = wire.Class{Nature: opt(cls.Nature), Role: opt(cls.Role), Counting: opt(cls.Counting), Billing: opt(cls.Billing)}
		it.SpecialType = opt(special)
	}
	if _, problems, err := s.Cat.Resolve(chain, it.Steps, nil); err == nil {
		it.IncompleteSteps = incomplete(problems)
	}
	_, required := mergedSchema(chain)
	if sp, ok := s.Cat.SpecialType(special); ok && special != "" {
		required = append(required, sp.RequiresProperties...)
	}
	it.MissingProperties = []string{}
	for _, key := range required {
		if !present[key] && !slices.Contains(it.MissingProperties, key) {
			it.MissingProperties = append(it.MissingProperties, key)
		}
	}
	sort.Strings(it.MissingProperties)
}

// propertiesWithValues is the keys each item has a value for, by item id: as
// entities/properties.py has_value, false and 0 count, a blank string or an empty list do
// not.
func propertiesWithValues(ctx context.Context, q Querier, itemIDs []string) (map[string]map[string]bool, error) {
	rows, err := q.Query(ctx, `SELECT e.ref, p.key FROM entity_properties p
		JOIN entities e ON e.id = p.entity_id AND e.kind = 'item'
		WHERE e.ref = ANY($1) AND CASE jsonb_typeof(p.value)
			WHEN 'string' THEN p.value #>> '{}' ~ '[^[:space:]]'
			WHEN 'array' THEN jsonb_array_length(p.value) > 0
			ELSE true END`, itemIDs)
	if err != nil {
		return nil, err
	}
	out := map[string]map[string]bool{}
	for rows.Next() {
		var item, key string
		if err := rows.Scan(&item, &key); err != nil {
			rows.Close()
			return nil, err
		}
		if out[item] == nil {
			out[item] = map[string]bool{}
		}
		out[item][key] = true
	}
	return out, rows.Err()
}

// Schema is the merged schema of the item's kind; the empty object schema for an id that
// names no item (phase 2 item_schema).
func (s *Items) Schema(ctx context.Context, q Querier, itemID string) (json.RawMessage, error) {
	empty := json.RawMessage(`{"type":"object","properties":{}}`)
	if !uuidText.MatchString(itemID) {
		return empty, nil
	}
	var kind string
	err := q.QueryRow(ctx, `SELECT kind_id FROM items WHERE id = $1::uuid`, itemID).Scan(&kind)
	if errors.Is(err, pgx.ErrNoRows) {
		return empty, nil
	}
	if err != nil {
		return nil, err
	}
	set, err := loadKinds(ctx, q)
	if err != nil {
		return nil, err
	}
	return schemaJSON(mergedSchema(set.chain(kind)))
}

// resolve is an item save's resolution (Resolve: the class must be complete, C3), refused
// as class_open, class_changed or class_invalid, as a refused special type's problem, or as
// steps_invalid.
func (s *Items) resolve(chain model.Chain, steps model.Steps) (handlers.Resolution, error) {
	res, problems, err := s.Cat.Resolve(chain, steps, nil)
	if err != nil {
		return res, classRefusal(err)
	}
	if r, ok := refusedSpecial[res.Special]; ok {
		refused := *r
		refused.Detail += ": " + chain[len(chain)-1].ID
		return res, &refused
	}
	return res, stepsRefusal(problems)
}

// withLife is a phase 2 life_hours (key hours) or life_months (key months) in the item's
// own steps.life (R-K1-6, as §8 item_life): the parameter of the life handler the item
// resolves to when it is handler; else handler with the value (and source own_run_hours
// for hours). A nil value removes the parameter.
func (s *Items) withLife(chain model.Chain, steps model.Steps, handler, key string, value any) model.Steps {
	out := model.Steps{}
	for k, v := range steps {
		out[k] = v
	}
	conf := out["life"]
	params := map[string]any{}
	for k, v := range conf.Params {
		params[k] = v
	}
	if value == nil {
		delete(params, key)
	} else if res, _, err := s.Cat.Resolve(chain, steps, nil); err == nil && res.Steps["life"].Handler == handler {
		params[key] = value
	} else {
		conf.Handler = handler
		params = map[string]any{key: value}
		if key == "hours" {
			params["source"] = "own_run_hours"
		}
	}
	conf.Params = params
	if len(params) == 0 {
		conf.Params = nil
	}
	if conf.Handler == "" && conf.Params == nil {
		delete(out, "life")
	} else {
		out["life"] = conf
	}
	return out
}

func lifeHours(d *wire.Decimal) any {
	if d == nil {
		return nil
	}
	f, _ := strconv.ParseFloat(string(*d), 64)
	return f
}

func lifeMonths(n *int64) any {
	if n == nil {
		return nil
	}
	return *n
}

// Create makes an item: kind_not_found, kind_archived, class_open, steps_invalid,
// filament_item_refused (Ruling 9) or item_exists. A blank source_ref is stored as null.
// Each compatible_with code becomes a relation to its model kind. It publishes one
// handler_config.changed per step it resolves.
func (s *Items) Create(ctx context.Context, tx pgx.Tx, in wire.ItemInput, by Actor) (wire.Item, error) {
	var archived *time.Time
	err := tx.QueryRow(ctx, `SELECT archived_at FROM item_kinds WHERE id = $1 FOR SHARE`, in.KindID).Scan(&archived)
	if errors.Is(err, pgx.ErrNoRows) {
		return wire.Item{}, notFound(in.KindID)
	}
	if err != nil {
		return wire.Item{}, err
	}
	if archived != nil {
		return wire.Item{}, kindArchived(in.KindID)
	}
	set, err := loadKinds(ctx, tx)
	if err != nil {
		return wire.Item{}, err
	}
	chain := set.chain(in.KindID)
	steps := in.Steps
	if in.LifeHours != nil {
		steps = s.withLife(chain, steps, "life.hours", "hours", lifeHours(in.LifeHours))
	}
	if in.LifeMonths != nil {
		steps = s.withLife(chain, steps, "life.months", "months", lifeMonths(in.LifeMonths))
	}
	res, err := s.resolve(chain, steps)
	if err != nil {
		return wire.Item{}, err
	}
	stepsText, err := stepsJSON(steps)
	if err != nil {
		return wire.Item{}, err
	}
	low := "0"
	if in.LowStock != nil {
		low = string(*in.LowStock)
	}
	source := in.Source
	if source == "" {
		source = "manual"
	}
	var ref *string
	if in.SourceRef != nil && strings.TrimSpace(*in.SourceRef) != "" {
		ref = in.SourceRef
	}
	var id string
	err = tx.QueryRow(ctx, `INSERT INTO items (kind_id, name, manufacturer, low_stock, saleable, source,
		source_ref, note, steps) VALUES ($1, $2, $3, $4::numeric, $5, $6, $7, $8, $9) RETURNING id::text`,
		in.KindID, in.Name, in.Manufacturer, low, in.Saleable, source, ref, in.Note, stepsText).Scan(&id)
	if isUnique(err) {
		return wire.Item{}, itemExists(in.Name)
	}
	if err != nil {
		return wire.Item{}, err
	}
	if err := setCompatible(ctx, tx, s.Cat, id, in.CompatibleWith, by); err != nil {
		return wire.Item{}, err
	}
	if err := publishConfig(ctx, tx, "item:"+id, nil, res.Steps, by); err != nil {
		return wire.Item{}, err
	}
	return s.Get(ctx, tx, id)
}

// Update changes the fields the patch names: item_not_found, item_exists, class_open,
// steps_invalid. On an imported item (source not manual) each editable field whose value
// changes joins edited_fields, which a later import skips. A steps change publishes one
// handler_config.changed per step whose resolved handler or parameters changed.
func (s *Items) Update(ctx context.Context, tx pgx.Tx, id string, p wire.InventoryItemPatch, by Actor) (wire.Item, error) {
	if !uuidText.MatchString(id) {
		return wire.Item{}, itemNotFound(id)
	}
	var lock string
	err := tx.QueryRow(ctx, `SELECT id::text FROM items WHERE id = $1::uuid FOR UPDATE`, id).Scan(&lock)
	if errors.Is(err, pgx.ErrNoRows) {
		return wire.Item{}, itemNotFound(id)
	}
	if err != nil {
		return wire.Item{}, err
	}
	cur, err := s.Get(ctx, tx, id)
	if err != nil {
		return wire.Item{}, err
	}
	norm := func(d wire.Decimal) (string, error) {
		var out string
		err := tx.QueryRow(ctx, `SELECT trim_scale($1::numeric)::text`, string(d)).Scan(&out)
		return out, err
	}
	next := cur
	changed := map[string]bool{}
	if p.Name != nil {
		next.Name, changed["name"] = *p.Name, *p.Name != cur.Name
	}
	if p.Manufacturer != nil {
		next.Manufacturer, changed["manufacturer"] = *p.Manufacturer, *p.Manufacturer != cur.Manufacturer
	}
	if p.CompatibleWith != nil {
		next.CompatibleWith, changed["compatible_with"] = *p.CompatibleWith, !slices.Equal(*p.CompatibleWith, cur.CompatibleWith)
	}
	if p.LowStock != nil {
		if next.LowStock, err = norm(*p.LowStock); err != nil {
			return wire.Item{}, err
		}
		changed["low_stock"] = next.LowStock != cur.LowStock
	}
	if p.Saleable.Set {
		next.Saleable = p.Saleable.Value
		changed["saleable"] = (cur.Saleable == nil) != (next.Saleable == nil) || cur.Saleable != nil && *cur.Saleable != *next.Saleable
	}
	if p.Note != nil {
		next.Note, changed["note"] = *p.Note, *p.Note != cur.Note
	}
	set, err := loadKinds(ctx, tx)
	if err != nil {
		return wire.Item{}, err
	}
	chain := set.chain(cur.KindID)
	steps := cur.Steps
	if p.Steps != nil {
		steps = *p.Steps
	}
	if p.LifeHours.Set {
		var v *string
		if p.LifeHours.Value != nil {
			n, err := norm(*p.LifeHours.Value)
			if err != nil {
				return wire.Item{}, err
			}
			v = &n
		}
		changed["life_hours"] = deref(v) != deref(cur.LifeHours) || (v == nil) != (cur.LifeHours == nil)
		steps = s.withLife(chain, steps, "life.hours", "hours", lifeHours(p.LifeHours.Value))
	}
	if p.LifeMonths.Set {
		v, c := p.LifeMonths.Value, cur.LifeMonths
		changed["life_months"] = (v == nil) != (c == nil) || v != nil && *v != *c
		steps = s.withLife(chain, steps, "life.months", "months", lifeMonths(v))
	}
	before, err := stepsJSON(cur.Steps)
	if err != nil {
		return wire.Item{}, err
	}
	after, err := stepsJSON(steps)
	if err != nil {
		return wire.Item{}, err
	}
	stepsChanged := string(before) != string(after)
	var edits []string
	for _, f := range editable {
		if changed[f] {
			edits = append(edits, f)
		}
	}
	if len(edits) == 0 && !stepsChanged {
		return cur, nil
	}
	var res, old handlers.Resolution
	if stepsChanged {
		old, _, _ = s.Cat.Resolve(chain, cur.Steps, nil)
		if res, err = s.resolve(chain, steps); err != nil {
			return wire.Item{}, err
		}
	}
	edited := cur.EditedFields
	if cur.Source != "manual" {
		for _, f := range edits {
			if !slices.Contains(edited, f) {
				edited = append(edited, f)
			}
		}
	}
	_, err = tx.Exec(ctx, `UPDATE items SET name = $2, manufacturer = $3, low_stock = $4::numeric, saleable = $5,
		note = $6, steps = $7, edited_fields = $8, updated_at = now() WHERE id = $1::uuid`,
		id, next.Name, next.Manufacturer, next.LowStock, next.Saleable, next.Note, after, edited)
	if isUnique(err) {
		return wire.Item{}, itemExists(next.Name)
	}
	if err != nil {
		return wire.Item{}, err
	}
	if changed["compatible_with"] {
		if err := setCompatible(ctx, tx, s.Cat, id, next.CompatibleWith, by); err != nil {
			return wire.Item{}, err
		}
	}
	if stepsChanged {
		if err := publishConfig(ctx, tx, "item:"+id, old.Steps, res.Steps, by); err != nil {
			return wire.Item{}, err
		}
	}
	return s.Get(ctx, tx, id)
}

// Archive archives the item.
func (s *Items) Archive(ctx context.Context, tx pgx.Tx, id string) (wire.Item, error) {
	if !uuidText.MatchString(id) {
		return wire.Item{}, itemNotFound(id)
	}
	tag, err := tx.Exec(ctx, `UPDATE items SET archived_at = coalesce(archived_at, now()), updated_at = now()
		WHERE id = $1::uuid`, id)
	if err != nil {
		return wire.Item{}, err
	}
	if tag.RowsAffected() == 0 {
		return wire.Item{}, itemNotFound(id)
	}
	return s.Get(ctx, tx, id)
}

// Restore unarchives the item: kind_archived while its kind is archived; item_exists when
// an unarchived item of its kind took its name meanwhile.
func (s *Items) Restore(ctx context.Context, tx pgx.Tx, id string) (wire.Item, error) {
	if !uuidText.MatchString(id) {
		return wire.Item{}, itemNotFound(id)
	}
	var name, kind string
	err := tx.QueryRow(ctx, `SELECT name, kind_id FROM items WHERE id = $1::uuid FOR UPDATE`, id).Scan(&name, &kind)
	if errors.Is(err, pgx.ErrNoRows) {
		return wire.Item{}, itemNotFound(id)
	}
	if err != nil {
		return wire.Item{}, err
	}
	var archived *time.Time
	if err := tx.QueryRow(ctx, `SELECT archived_at FROM item_kinds WHERE id = $1 FOR SHARE`, kind).Scan(&archived); err != nil {
		return wire.Item{}, err
	}
	if archived != nil {
		return wire.Item{}, kindArchived(kind)
	}
	_, err = tx.Exec(ctx, `UPDATE items SET archived_at = NULL, updated_at = now() WHERE id = $1::uuid`, id)
	if isUnique(err) {
		return wire.Item{}, itemExists(name)
	}
	if err != nil {
		return wire.Item{}, err
	}
	return s.Get(ctx, tx, id)
}
