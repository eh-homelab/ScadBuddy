package store

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"regexp"
	"slices"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/inventory/handlers"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/inventory/model"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/inventory/wire"
)

// Item kinds (design §3, §4.2; phase 2 inventory/kinds.py): a tree of rows. A kind answers
// the class keys its parents leave open and never changes one they answered; its steps set
// handlers and parameters over its parents'. Its attribute_schema documents its items'
// properties, merged down the tree, a child's key winning.

// Querier reads: a pool, or the transaction of a write.
type Querier interface {
	Query(ctx context.Context, sql string, args ...any) (pgx.Rows, error)
	QueryRow(ctx context.Context, sql string, args ...any) pgx.Row
}

// Catalog is what a save resolves with: handlers.Builtin() in the service.
type Catalog interface {
	Resolve(chain model.Chain, item, unit model.Steps) (handlers.Resolution, []handlers.Problem, error)
	ResolveKind(chain model.Chain) (handlers.Resolution, []handlers.Problem, error)
	SpecialType(id string) (handlers.SpecialType, bool)
}

// StepProblem is one handlers.Problem in a steps_invalid refusal.
type StepProblem struct {
	// Owner is the kind (item_kind:<id>) or item (item:<uuid>) the problem is in, when a
	// kind save breaks a descendant kind's or an item's steps; empty for the saved owner.
	Owner   string   `json:"owner,omitempty"`
	Step    string   `json:"step,omitempty"`
	Handler string   `json:"handler,omitempty"`
	Reason  string   `json:"reason"`
	Missing []string `json:"missing,omitempty"`
}

// ClassProblem is the class key a class_changed refusal names; null is a key left open.
type ClassProblem struct {
	// Owner is the kind below the saved one whose class would change (item_kind:<id>);
	// empty for the saved kind.
	Owner string  `json:"owner,omitempty"`
	Key   string  `json:"key"`
	From  *string `json:"from"`
	To    *string `json:"to"`
}

// The phase 2 tracking rules (§8, R-K1-6): the class a tracking stands for, and the
// special type for which a kind reads tracking `filament`.
var (
	trackingClass = map[string]model.Class{
		"lot":      {Nature: "physical", Role: "consumable", Counting: "lot"},
		"unit":     {Nature: "physical", Role: "durable", Counting: "unit"},
		"none":     {Nature: "service", Billing: "flat"},
		"filament": {Nature: "physical", Role: "consumable", Counting: "unit"},
	}
	trackingSpecial = map[string]string{"filament": "filament_spool"}
	lifeBasis       = map[string]string{"life.hours": "hours", "life.months": "months"}
	classKeys       = []string{"nature", "role", "counting", "billing"}
)

// Kinds is the item kind store.
type Kinds struct {
	Cat Catalog
}

func notFound(kind string) error {
	return &Refused{Status: 404, Code: "kind_not_found", Detail: "no item kind: " + kind}
}

func kindArchived(kind string) error {
	return &Refused{Status: 409, Code: "kind_archived", Detail: "the item kind is archived; restore it first: " + kind}
}

func kindInUse(why string) error {
	return &Refused{Status: 409, Code: "kind_in_use", Detail: "the kind's items, child kinds or built-in status forbid it: " + why}
}

func opt(s string) *string {
	if s == "" {
		return nil
	}
	return &s
}

// classRefusal is a class that does not resolve: class_changed for a key a child changes,
// class_open for an item's open class, else class_invalid.
func classRefusal(err error) error {
	var changed *model.ClassChanged
	switch {
	case errors.As(err, &changed):
		return &Refused{Status: 422, Code: "class_changed", Detail: err.Error(),
			Problems: []ClassProblem{{Key: changed.Key, From: opt(changed.From), To: opt(changed.To)}}}
	case errors.Is(err, model.ErrClassOpen):
		return &Refused{Status: 422, Code: "class_open", Detail: "the item's kind has not answered every class key: " + err.Error()}
	}
	return &Refused{Status: 422, Code: "class_invalid", Detail: err.Error()}
}

// stepsRefusal is the problems that refuse a save (handlers.Blocking), or nil (R-K1-8).
func stepsRefusal(problems []handlers.Problem) error {
	return ownedRefusal("", problems)
}

// ownedRefusal is stepsRefusal with each problem naming owner.
func ownedRefusal(owner string, problems []handlers.Problem) error {
	blocking := handlers.Blocking(problems)
	if len(blocking) == 0 {
		return nil
	}
	out := make([]StepProblem, 0, len(blocking))
	for _, p := range blocking {
		out = append(out, StepProblem{Owner: owner, Step: string(p.Step), Handler: p.Handler, Reason: p.Reason, Missing: p.Missing})
	}
	return &Refused{Status: 422, Code: "steps_invalid", Detail: "a step does not resolve to a handler it can use", Problems: out}
}

// incomplete is the steps whose handler lacks a required parameter, in step order.
func incomplete(problems []handlers.Problem) []string {
	out := []string{}
	for _, p := range handlers.Incomplete(problems) {
		if !slices.Contains(out, string(p.Step)) {
			out = append(out, string(p.Step))
		}
	}
	slices.SortFunc(out, func(a, b string) int {
		return slices.Index(handlers.Steps, handlers.Step(a)) - slices.Index(handlers.Steps, handlers.Step(b))
	})
	return out
}

func isUnique(err error) bool {
	var pg *pgconn.PgError
	return errors.As(err, &pg) && pg.Code == "23505"
}

// kindSet is every kind, by id.
type kindSet map[string]model.Kind

const kindsSQL = `SELECT id, name, coalesce(parent_id, ''), coalesce(nature, ''), coalesce(role, ''),
	coalesce(counting, ''), coalesce(billing, ''), coalesce(special_type, ''), uom, steps, attribute_schema,
	saleable, builtin, archived_at FROM item_kinds`

func loadKinds(ctx context.Context, q Querier) (kindSet, error) {
	rows, err := q.Query(ctx, kindsSQL)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	set := kindSet{}
	for rows.Next() {
		var k model.Kind
		var steps []byte
		if err := rows.Scan(&k.ID, &k.Name, &k.ParentID, &k.Class.Nature, &k.Class.Role, &k.Class.Counting,
			&k.Class.Billing, &k.Special, &k.UOM, &steps, &k.Schema, &k.Saleable, &k.Builtin, &k.ArchivedAt); err != nil {
			return nil, err
		}
		if err := json.Unmarshal(steps, &k.Steps); err != nil {
			return nil, err
		}
		set[k.ID] = k
	}
	return set, rows.Err()
}

// chain is the kind and its ancestors, root first (at most 100 deep, as phase 2's
// _ANCESTORS). An unknown id is an empty chain.
func (s kindSet) chain(id string) model.Chain {
	var out model.Chain
	for at := id; at != "" && len(out) <= 100; {
		k, ok := s[at]
		if !ok || slices.ContainsFunc(out, func(c model.Kind) bool { return c.ID == at }) {
			break
		}
		out = append(out, k)
		at = k.ParentID
	}
	slices.Reverse(out)
	return out
}

func (s kindSet) path(id string) []string {
	out := []string{}
	for _, k := range s.chain(id) {
		out = append(out, k.ID)
	}
	if len(out) == 0 {
		out = append(out, id)
	}
	return out
}

// subtree is the kind and its descendants, the kind first.
func (s kindSet) subtree(id string) []string {
	out := []string{id}
	for i := 0; i < len(out); i++ {
		for _, k := range sortedKeys(s) {
			if s[k].ParentID == out[i] && !slices.Contains(out, k) {
				out = append(out, k)
			}
		}
	}
	return out
}

// mergedSchema is the chain's schema merged root to leaf (phase 2 merged_schema): each
// kind's properties, a child's key winning, and every required key once.
func mergedSchema(chain model.Chain) (props map[string]json.RawMessage, required []string) {
	props = map[string]json.RawMessage{}
	for _, k := range chain {
		var s struct {
			Properties map[string]json.RawMessage `json:"properties"`
			Required   []string                   `json:"required"`
		}
		if json.Unmarshal(k.Schema, &s) != nil {
			continue
		}
		for key, v := range s.Properties {
			props[key] = v
		}
		for _, r := range s.Required {
			if !slices.Contains(required, r) {
				required = append(required, r)
			}
		}
	}
	return props, required
}

func schemaJSON(props map[string]json.RawMessage, required []string) (json.RawMessage, error) {
	m := map[string]any{"type": "object", "properties": props}
	if len(required) > 0 {
		m["required"] = required
	}
	return json.Marshal(m)
}

// derived is a kind's phase 2 tracking and life_basis (R-K1-6): tracking follows the special
// type, then the nature (none for a service), then the counting (lot when open); life_basis
// follows the life handler.
func derived(cat Catalog, chain model.Chain) (tracking, basis string, res handlers.Resolution, problems []handlers.Problem) {
	cls, special, err := chain.Class()
	res, problems, rerr := cat.ResolveKind(chain)
	if err != nil || rerr != nil {
		return "", "", handlers.Resolution{}, nil
	}
	tracking = cls.Counting
	if tracking == "" {
		tracking = "lot"
	}
	if cls.Nature == "service" {
		tracking = "none"
	}
	for t, sp := range trackingSpecial {
		if special == sp {
			tracking = t
		}
	}
	basis = lifeBasis[res.Steps["life"].Handler]
	if basis == "" {
		basis = "none"
	}
	return tracking, basis, res, problems
}

// rewriteDerived writes the tracking and life_basis of each kind, so the SQL readers of
// phase 2 stay right.
func rewriteDerived(ctx context.Context, tx pgx.Tx, cat Catalog, set kindSet, ids []string) error {
	for _, id := range ids {
		tracking, basis, _, _ := derived(cat, set.chain(id))
		if tracking == "" {
			continue
		}
		if _, err := tx.Exec(ctx, `UPDATE item_kinds SET tracking = $2, life_basis = $3
			WHERE id = $1 AND (tracking, life_basis) IS DISTINCT FROM ($2, $3)`, id, tracking, basis); err != nil {
			return err
		}
	}
	return nil
}

// madeKindID is the id of a kind that ScadBuddy makes: k and the kind's number
// (item_kinds_made_id_check, the owner's decision of 2026-11-27).
var madeKindID = regexp.MustCompile(`^k[0-9]+$`)

const kindSQL = `SELECT k.id, k.number, k.tracking, k.life_basis,
	(SELECT count(*) FROM items i WHERE i.kind_id = k.id AND i.archived_at IS NULL) FROM item_kinds k`

func (k *Kinds) read(ctx context.Context, q Querier, where string, args ...any) ([]wire.ItemKind, error) {
	set, err := loadKinds(ctx, q)
	if err != nil {
		return nil, err
	}
	rows, err := q.Query(ctx, kindSQL+where+" ORDER BY k.id", args...)
	if err != nil {
		return nil, err
	}
	out := []wire.ItemKind{}
	for rows.Next() {
		var id, tracking, basis string
		var count int
		var number int64
		if err := rows.Scan(&id, &number, &tracking, &basis, &count); err != nil {
			rows.Close()
			return nil, err
		}
		out = append(out, k.wire(set, id, number, tracking, basis, count))
	}
	return out, rows.Err()
}

func (k *Kinds) wire(set kindSet, id string, number int64, tracking, basis string, count int) wire.ItemKind {
	kind := set[id]
	_, problems, err := k.Cat.ResolveKind(set.chain(id))
	steps := kind.Steps
	if steps == nil {
		steps = model.Steps{}
	}
	out := wire.ItemKind{
		ID: id, Name: kind.Name, ParentID: opt(kind.ParentID), Tracking: tracking, UOM: kind.UOM,
		AttributeSchema: kind.Schema, LifeBasis: basis, Saleable: kind.Saleable, Builtin: kind.Builtin,
		ArchivedAt: kind.ArchivedAt, ItemCount: count, Number: number, Code: fmt.Sprintf("K-%d", number), Nature: opt(kind.Class.Nature), Role: opt(kind.Class.Role),
		Counting: opt(kind.Class.Counting), Billing: opt(kind.Class.Billing), SpecialType: opt(kind.Special),
		Steps: steps, IncompleteSteps: []string{},
	}
	if err == nil {
		out.IncompleteSteps = incomplete(problems)
	}
	return out
}

// List is every kind by id; the archived ones too with archived.
func (k *Kinds) List(ctx context.Context, q Querier, archived bool) ([]wire.ItemKind, error) {
	where := " WHERE k.archived_at IS NULL"
	if archived {
		where = ""
	}
	return k.read(ctx, q, where)
}

// Get is the kind, or kind_not_found.
func (k *Kinds) Get(ctx context.Context, q Querier, id string) (wire.ItemKind, error) {
	out, err := k.read(ctx, q, " WHERE k.id = $1", id)
	if err != nil {
		return wire.ItemKind{}, err
	}
	if len(out) == 0 {
		return wire.ItemKind{}, notFound(id)
	}
	return out[0], nil
}

// KindSchema is what Schema answers for a kind.
type KindSchema struct {
	Merged    json.RawMessage
	Own       json.RawMessage
	KeysInUse [][2]any
	// ItemRequired is the keys an item of the kind must have a value for (R-K1-33): its
	// kinds' merged required keys, ancestor first, then its special type's
	// requires_properties, each once.
	ItemRequired []string
}

// Schema is the kind's merged schema, its own, and each property key its items and its
// descendants' items have (archived ones too), with how many, by key (phase 2 schema_of).
// It loads the kind set once.
func (k *Kinds) Schema(ctx context.Context, q Querier, id string) (KindSchema, error) {
	var out KindSchema
	set, err := loadKinds(ctx, q)
	if err != nil {
		return out, err
	}
	kind, ok := set[id]
	if !ok {
		return out, notFound(id)
	}
	chain := set.chain(id)
	if out.Merged, err = schemaJSON(mergedSchema(chain)); err != nil {
		return out, err
	}
	out.Own = kind.Schema
	var required []string
	if k.Cat != nil {
		_, required = itemSchema(k.Cat, chain)
	} else {
		_, required = mergedSchema(chain)
	}
	out.ItemRequired = []string{}
	for _, key := range required {
		if !slices.Contains(out.ItemRequired, key) {
			out.ItemRequired = append(out.ItemRequired, key)
		}
	}
	rows, err := q.Query(ctx, `SELECT p.key, count(*) FROM entity_properties p
		JOIN entities e ON e.id = p.entity_id AND e.kind = 'item'
		JOIN items i ON i.id::text = e.ref
		WHERE i.kind_id = ANY($1) GROUP BY p.key ORDER BY p.key COLLATE "C"`, set.subtree(id))
	if err != nil {
		return out, err
	}
	out.KeysInUse = [][2]any{}
	for rows.Next() {
		var key string
		var n int64
		if err := rows.Scan(&key, &n); err != nil {
			rows.Close()
			return out, err
		}
		out.KeysInUse = append(out.KeysInUse, [2]any{key, n})
	}
	return out, rows.Err()
}

// lockParent is phase 2's _lock_parent: one tree change at a time, so two moves cannot each
// pass the cycle check and make a cycle together; then the parent, held until the commit so
// it cannot be archived meanwhile.
func lockParent(ctx context.Context, tx pgx.Tx, parent string) error {
	if _, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock(hashtext('item_kinds_tree'))`); err != nil {
		return err
	}
	var archived *time.Time
	err := tx.QueryRow(ctx, `SELECT archived_at FROM item_kinds WHERE id = $1 FOR SHARE`, parent).Scan(&archived)
	if errors.Is(err, pgx.ErrNoRows) {
		return notFound(parent)
	}
	if err != nil {
		return err
	}
	if archived != nil {
		return kindArchived(parent)
	}
	return nil
}

// withTracking is the class keys and special type a phase 2 tracking gives a kind under
// parent (§8): the keys of its rule that parent leaves open. A key parent answers another
// way is class_changed.
func withTracking(parent model.Chain, tracking string) (model.Class, string, error) {
	rule, ok := trackingClass[tracking]
	if !ok {
		return model.Class{}, "", &Refused{Status: 422, Code: "class_invalid", Detail: "no tracking " + tracking}
	}
	pcls, pspecial, err := parent.Class()
	if err != nil {
		return model.Class{}, "", classRefusal(err)
	}
	var own model.Class
	for _, key := range classKeys {
		want, got := rule.Get(key), pcls.Get(key)
		switch {
		case want == "" || got == want:
		case got == "":
			setKey(&own, key, want)
		default:
			return model.Class{}, "", classRefusal(&model.ClassChanged{Key: key, From: got, To: want})
		}
	}
	special := ""
	if want := trackingSpecial[tracking]; want != "" && pspecial != want {
		if pspecial != "" {
			return model.Class{}, "", classRefusal(&model.ClassChanged{Key: "special_type", From: pspecial, To: want})
		}
		special = want
	}
	return own, special, nil
}

func setKey(c *model.Class, key, v string) {
	switch key {
	case "nature":
		c.Nature = v
	case "role":
		c.Role = v
	case "counting":
		c.Counting = v
	case "billing":
		c.Billing = v
	}
}

// withLifeBasis is a phase 2 life_basis as the kind's own steps.life (§8 kind_life):
// hours names life.hours (source own_run_hours), months life.months, none life.none. current
// is the life basis the kind reads now; the same basis changes nothing.
func withLifeBasis(steps model.Steps, basis, current string) model.Steps {
	if basis == current {
		return steps
	}
	out := model.Steps{}
	for k, v := range steps {
		out[k] = v
	}
	switch basis {
	case "hours":
		out["life"] = model.StepConf{Handler: "life.hours", Params: map[string]any{"source": "own_run_hours"}}
	case "months":
		out["life"] = model.StepConf{Handler: "life.months"}
	default:
		out["life"] = model.StepConf{Handler: "life.none"}
	}
	return out
}

func defined(chain model.Chain) map[string]bool {
	props, _ := mergedSchema(chain)
	out := map[string]bool{}
	for k := range props {
		out[k] = true
	}
	return out
}

func stepsJSON(steps model.Steps) ([]byte, error) {
	if steps == nil {
		steps = model.Steps{}
	}
	return json.Marshal(steps)
}

// Create makes a kind: kind_not_found or kind_archived for its parent, bad_property_schema,
// class_changed for a class key it changes, steps_invalid for a step that does not resolve,
// kind_exists for a taken id. It publishes one handler_config.changed per step it resolves.
func (k *Kinds) Create(ctx context.Context, tx pgx.Tx, in wire.KindInput, by Actor) (wire.ItemKind, error) {
	if madeKindID.MatchString(in.ID) {
		// KindInput's _not_a_made_id refuses it before the start; the column CHECK would fail
		// the write.
		return wire.ItemKind{}, &Refused{Status: 422, Code: "bad_input",
			Detail: "an id of k and digits is a made kind's id: k and its number"}
	}
	parent := ""
	if in.ParentID != nil {
		parent = *in.ParentID
		if err := lockParent(ctx, tx, parent); err != nil {
			return wire.ItemKind{}, err
		}
	}
	set, err := loadKinds(ctx, tx)
	if err != nil {
		return wire.ItemKind{}, err
	}
	up := set.chain(parent)
	kind := model.Kind{ID: in.ID, Name: in.Name, ParentID: parent, UOM: in.UOM, Saleable: in.Saleable,
		Steps: in.Steps, Schema: in.AttributeSchema}
	if kind.UOM == "" {
		kind.UOM = "each"
	}
	if kind.Schema == nil {
		kind.Schema = json.RawMessage(`{"type": "object", "properties": {}}`)
	}
	if in.Tracking != nil {
		if kind.Class, kind.Special, err = withTracking(up, *in.Tracking); err != nil {
			return wire.ItemKind{}, err
		}
	}
	for key, v := range map[string]*string{"nature": in.Nature, "role": in.Role, "counting": in.Counting, "billing": in.Billing} {
		if v != nil {
			setKey(&kind.Class, key, *v)
		}
	}
	if in.SpecialType != nil {
		kind.Special = *in.SpecialType
	}
	if in.LifeBasis != nil {
		_, current, _, _ := derived(k.Cat, up)
		if current == "" {
			current = "none"
		}
		kind.Steps = withLifeBasis(kind.Steps, *in.LifeBasis, current)
	}
	if err := checkSchema(kind.Schema, defined(up)); err != nil {
		return wire.ItemKind{}, err
	}
	chain := append(slices.Clone(up), kind)
	tracking, basis, res, err := k.resolve(chain)
	if err != nil {
		return wire.ItemKind{}, err
	}
	steps, err := stepsJSON(kind.Steps)
	if err != nil {
		return wire.ItemKind{}, err
	}
	// With no id, the kind is k<number> (item_kinds_made_id_check); with one, it keeps it.
	err = tx.QueryRow(ctx, `INSERT INTO item_kinds (id, number, name, parent_id, tracking, uom,
		attribute_schema, life_basis, saleable, nature, role, counting, billing, special_type, steps)
		SELECT coalesce(nullif($1, ''), 'k' || n), n, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14
		FROM nextval('item_kinds_number_seq') AS n RETURNING id`,
		kind.ID, kind.Name, opt(parent), tracking, kind.UOM, kind.Schema, basis, kind.Saleable,
		opt(kind.Class.Nature), opt(kind.Class.Role), opt(kind.Class.Counting), opt(kind.Class.Billing),
		opt(kind.Special), steps).Scan(&kind.ID)
	if isUnique(err) {
		return wire.ItemKind{}, &Refused{Status: 409, Code: "kind_exists", Detail: "an item kind has that id: " + kind.ID}
	}
	if err != nil {
		return wire.ItemKind{}, err
	}
	if err := publishConfig(ctx, tx, "item_kind:"+kind.ID, nil, res.Steps, by); err != nil {
		return wire.ItemKind{}, err
	}
	return k.Get(ctx, tx, kind.ID)
}

// resolve is a kind save's resolution (ResolveKind: a kind may leave class keys open, C3),
// refused as class_changed, class_invalid or steps_invalid, and the tracking and life_basis
// it gives.
func (k *Kinds) resolve(chain model.Chain) (tracking, basis string, res handlers.Resolution, err error) {
	if _, _, err := chain.Class(); err != nil {
		return "", "", res, classRefusal(err)
	}
	res, problems, err := k.Cat.ResolveKind(chain)
	if err != nil {
		return "", "", res, classRefusal(err)
	}
	if err := stepsRefusal(problems); err != nil {
		return "", "", res, err
	}
	tracking, basis, _, _ = derived(k.Cat, chain)
	return tracking, basis, res, nil
}

// kindsWithItems is which of ids have items, archived ones too.
func kindsWithItems(ctx context.Context, tx pgx.Tx, ids []string) (map[string]bool, error) {
	rows, err := tx.Query(ctx, `SELECT DISTINCT kind_id FROM items WHERE kind_id = ANY($1)`, ids)
	if err != nil {
		return nil, err
	}
	ks, err := pgx.CollectRows(rows, pgx.RowTo[string])
	out := map[string]bool{}
	for _, id := range ks {
		out[id] = true
	}
	return out, err
}

// classChange is the first key (a class key, then special_type) whose value differs.
func classChange(oldCls model.Class, oldSpecial string, newCls model.Class, newSpecial string) *ClassProblem {
	for _, key := range classKeys {
		if a, b := oldCls.Get(key), newCls.Get(key); a != b {
			return &ClassProblem{Key: key, From: opt(a), To: opt(b)}
		}
	}
	if oldSpecial != newSpecial {
		return &ClassProblem{Key: "special_type", From: opt(oldSpecial), To: opt(newSpecial)}
	}
	return nil
}

// Update changes the fields the patch names. kind_not_found; kind_in_use for a tracking,
// class or special type change of a built-in kind, or a tracking or uom change of a kind
// with items; kind_cycle for a parent at or below the kind; kind_archived for an archived parent;
// bad_property_schema, also when a kind below it would require a key that its merged
// schema no longer defines (R-K1-33); class_changed for a change that changes the class of a kind with
// items (its own or a descendant's) or that a descendant contradicts; steps_invalid when a
// step of the kind, of a kind below it, or of an item of those that sets its own steps no
// longer resolves (the problem names that kind or item). It
// publishes one handler_config.changed per step whose handler or parameters changed, and
// rewrites the tracking and life_basis of the kind and its descendants.
func (k *Kinds) Update(ctx context.Context, tx pgx.Tx, id string, p wire.KindPatch, by Actor) (wire.ItemKind, error) {
	var tracking, basis string
	var builtin bool
	err := tx.QueryRow(ctx, `SELECT tracking, life_basis, builtin FROM item_kinds WHERE id = $1 FOR UPDATE`, id).
		Scan(&tracking, &basis, &builtin)
	if errors.Is(err, pgx.ErrNoRows) {
		return wire.ItemKind{}, notFound(id)
	}
	if err != nil {
		return wire.ItemKind{}, err
	}
	set, err := loadKinds(ctx, tx)
	if err != nil {
		return wire.ItemKind{}, err
	}
	old := set[id]
	kind := old
	if p.Name != nil {
		kind.Name = *p.Name
	}
	if p.UOM != nil && *p.UOM != old.UOM {
		has, err := kindsWithItems(ctx, tx, []string{id})
		if err != nil {
			return wire.ItemKind{}, err
		}
		if has[id] {
			return wire.ItemKind{}, kindInUse(id + " has items; its unit stays")
		}
		kind.UOM = *p.UOM
	}
	if p.Saleable != nil {
		kind.Saleable = *p.Saleable
	}
	if p.AttributeSchema != nil && string(p.AttributeSchema) != "null" {
		kind.Schema = p.AttributeSchema
	}
	if p.Steps != nil {
		kind.Steps = *p.Steps
	}
	if p.ParentID.Set {
		kind.ParentID = ""
		if p.ParentID.Value != nil {
			kind.ParentID = *p.ParentID.Value
		}
	}
	if kind.ParentID != "" && p.ParentID.Set {
		if err := lockParent(ctx, tx, kind.ParentID); err != nil {
			return wire.ItemKind{}, err
		}
		if slices.ContainsFunc(set.chain(kind.ParentID), func(c model.Kind) bool { return c.ID == id }) {
			return wire.ItemKind{}, &Refused{Status: 422, Code: "kind_cycle",
				Detail: fmt.Sprintf("the parent is the kind or below it: %s is %s or below it", kind.ParentID, id)}
		}
	}
	up := set.chain(kind.ParentID)
	if p.Tracking != nil && *p.Tracking != tracking {
		if builtin {
			return wire.ItemKind{}, kindInUse(id + " is built in; its tracking stays")
		}
		has, err := kindsWithItems(ctx, tx, []string{id})
		if err != nil {
			return wire.ItemKind{}, err
		}
		if has[id] {
			return wire.ItemKind{}, kindInUse(id + " has items; its tracking stays")
		}
		if kind.Class, kind.Special, err = withTracking(up, *p.Tracking); err != nil {
			return wire.ItemKind{}, err
		}
	}
	for key, o := range map[string]wire.Opt[string]{"nature": p.Nature, "role": p.Role, "counting": p.Counting, "billing": p.Billing} {
		if o.Set {
			setKey(&kind.Class, key, deref(o.Value))
		}
	}
	if p.SpecialType.Set {
		kind.Special = deref(p.SpecialType.Value)
	}
	if p.LifeBasis != nil {
		kind.Steps = withLifeBasis(kind.Steps, *p.LifeBasis, basis)
	}
	if err := checkSchema(kind.Schema, defined(up)); err != nil {
		return wire.ItemKind{}, err
	}

	next := kindSet{}
	for key, v := range set {
		next[key] = v
	}
	next[id] = kind
	if builtin {
		oldCls, oldSpecial, _ := set.chain(id).Class()
		cls, special, err := next.chain(id).Class()
		if err != nil || classChange(oldCls, oldSpecial, cls, special) != nil {
			return wire.ItemKind{}, kindInUse(id + " is built in; its class stays")
		}
	}
	below := next.subtree(id)
	if err := keepsRequired(next, id, below); err != nil {
		return wire.ItemKind{}, err
	}
	has, err := kindsWithItems(ctx, tx, below)
	if err != nil {
		return wire.ItemKind{}, err
	}
	for _, d := range below {
		cls, special, err := next.chain(d).Class()
		if err != nil {
			return wire.ItemKind{}, classRefusal(err)
		}
		oldCls, oldSpecial, _ := set.chain(d).Class()
		change := classChange(oldCls, oldSpecial, cls, special)
		if change != nil && d != id && next[d].Builtin {
			owned := *change
			owned.Owner = "item_kind:" + d
			return wire.ItemKind{}, &Refused{Status: 409, Code: "kind_in_use",
				Detail:   "the kind's items, child kinds or built-in status forbid it: item_kind:" + d + " is built in; its class stays",
				Problems: []ClassProblem{owned}}
		}
		if change != nil && has[d] {
			return wire.ItemKind{}, &Refused{Status: 422, Code: "class_changed",
				Detail: "the change would change the class of " + d + ", which has items", Problems: []ClassProblem{*change}}
		}
		if d == id {
			continue
		}
		// Design §4.2: every step of every kind below must still resolve.
		_, problems, err := k.Cat.ResolveKind(next.chain(d))
		if err != nil {
			return wire.ItemKind{}, classRefusal(err)
		}
		if err := ownedRefusal("item_kind:"+d, problems); err != nil {
			return wire.ItemKind{}, err
		}
	}
	if err := k.checkItems(ctx, tx, next, below); err != nil {
		return wire.ItemKind{}, err
	}
	_, _, res, err := k.resolve(next.chain(id))
	if err != nil {
		return wire.ItemKind{}, err
	}
	steps, err := stepsJSON(kind.Steps)
	if err != nil {
		return wire.ItemKind{}, err
	}
	if _, err := tx.Exec(ctx, `UPDATE item_kinds SET name = $2, parent_id = $3, uom = $4, attribute_schema = $5,
		saleable = $6, nature = $7, role = $8, counting = $9, billing = $10, special_type = $11, steps = $12,
		updated_at = now() WHERE id = $1`,
		id, kind.Name, opt(kind.ParentID), kind.UOM, kind.Schema, kind.Saleable, opt(kind.Class.Nature),
		opt(kind.Class.Role), opt(kind.Class.Counting), opt(kind.Class.Billing), opt(kind.Special), steps); err != nil {
		return wire.ItemKind{}, err
	}
	if err := rewriteDerived(ctx, tx, k.Cat, next, below); err != nil {
		return wire.ItemKind{}, err
	}
	before, _, _ := k.Cat.ResolveKind(set.chain(id))
	if err := publishConfig(ctx, tx, "item_kind:"+id, before.Steps, res.Steps, by); err != nil {
		return wire.ItemKind{}, err
	}
	return k.Get(ctx, tx, id)
}

// keepsRequired is bad_property_schema for the first kind below id (in subtree order)
// that requires a key of its own required list that its merged schema in next no longer
// defines: an ancestor dropped the key, or a kind moved (R-K1-33). The kind id itself is
// checked by checkSchema.
func keepsRequired(next kindSet, id string, below []string) error {
	for _, d := range below {
		if d == id {
			continue
		}
		var own struct {
			Required []string `json:"required"`
		}
		if json.Unmarshal(next[d].Schema, &own) != nil {
			continue
		}
		have := defined(next.chain(d))
		for _, key := range own.Required {
			if !have[key] {
				return badSchema("kind '%s' would require '%s', which it no longer defines", d, key)
			}
		}
	}
	return nil
}

// checkItems re-resolves each item of the kinds that sets its own steps: a kind save that
// leaves one with a blocking problem is steps_invalid, naming the item (design §4.2).
func (k *Kinds) checkItems(ctx context.Context, tx pgx.Tx, set kindSet, kinds []string) error {
	rows, err := tx.Query(ctx, `SELECT id::text, kind_id, steps FROM items
		WHERE kind_id = ANY($1) AND steps <> '{}' ORDER BY id`, kinds)
	if err != nil {
		return err
	}
	type item struct {
		id, kind string
		steps    model.Steps
	}
	var items []item
	for rows.Next() {
		var it item
		var raw []byte
		if err := rows.Scan(&it.id, &it.kind, &raw); err != nil {
			rows.Close()
			return err
		}
		if err := json.Unmarshal(raw, &it.steps); err != nil {
			rows.Close()
			return err
		}
		items = append(items, it)
	}
	if err := rows.Err(); err != nil {
		return err
	}
	for _, it := range items {
		_, problems, err := k.Cat.Resolve(set.chain(it.kind), it.steps, nil)
		if err != nil {
			return classRefusal(err)
		}
		if err := ownedRefusal("item:"+it.id, problems); err != nil {
			return err
		}
	}
	return nil
}

func deref(s *string) string {
	if s == nil {
		return ""
	}
	return *s
}

// Archive archives the kind: kind_in_use while it has unarchived items or unarchived child
// kinds. The row lock waits for a create, move or restore under the kind, which holds the
// kind FOR SHARE until it commits (lockParent).
func (k *Kinds) Archive(ctx context.Context, tx pgx.Tx, id string) (wire.ItemKind, error) {
	err := tx.QueryRow(ctx, `SELECT id FROM item_kinds WHERE id = $1 FOR UPDATE`, id).Scan(new(string))
	if errors.Is(err, pgx.ErrNoRows) {
		return wire.ItemKind{}, notFound(id)
	}
	if err != nil {
		return wire.ItemKind{}, err
	}
	var live, children bool
	if err := tx.QueryRow(ctx, `SELECT EXISTS (SELECT 1 FROM items WHERE kind_id = $1 AND archived_at IS NULL),
		EXISTS (SELECT 1 FROM item_kinds WHERE parent_id = $1 AND archived_at IS NULL)`, id).
		Scan(&live, &children); err != nil {
		return wire.ItemKind{}, err
	}
	if live {
		return wire.ItemKind{}, kindInUse(id + " has items; archive them first")
	}
	if children {
		return wire.ItemKind{}, kindInUse(id + " has child kinds; archive them first")
	}
	if _, err := tx.Exec(ctx, `UPDATE item_kinds SET archived_at = coalesce(archived_at, now()), updated_at = now()
		WHERE id = $1`, id); err != nil {
		return wire.ItemKind{}, err
	}
	return k.Get(ctx, tx, id)
}

// Restore unarchives the kind: kind_archived while its parent is archived, as a create or
// a move under that parent is.
func (k *Kinds) Restore(ctx context.Context, tx pgx.Tx, id string) (wire.ItemKind, error) {
	// The tree lock before the kind's row, as a create or a move takes them (lockParent):
	// the other order could deadlock with a create under this kind.
	if _, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock(hashtext('item_kinds_tree'))`); err != nil {
		return wire.ItemKind{}, err
	}
	var parent *string
	err := tx.QueryRow(ctx, `SELECT parent_id FROM item_kinds WHERE id = $1 FOR UPDATE`, id).Scan(&parent)
	if errors.Is(err, pgx.ErrNoRows) {
		return wire.ItemKind{}, notFound(id)
	}
	if err != nil {
		return wire.ItemKind{}, err
	}
	if parent != nil {
		if err := lockParent(ctx, tx, *parent); err != nil {
			return wire.ItemKind{}, err
		}
	}
	if _, err := tx.Exec(ctx, `UPDATE item_kinds SET archived_at = NULL, updated_at = now() WHERE id = $1`, id); err != nil {
		return wire.ItemKind{}, err
	}
	return k.Get(ctx, tx, id)
}
