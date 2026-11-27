// Package routes is the inventory's read routes under /api/v1/internal/crud/inventory (#6949
// K1, design §6.2): the public paths without /inventory, answered as the public JSON
// (R-K1-6), with pydantic-shaped 422s for a query or a path parameter. There is no write
// route: writes are activities. A route holds only parsing and problem mapping; every SQL
// statement is in the store.
package routes

import (
	"fmt"
	"net/http"
	"regexp"
	"strings"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/inventory/flow"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/inventory/store"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/inventory/wire"
	corewire "github.com/eh-homelab/ScadBuddy/tools/go/core/internal/wire"
)

const (
	// operationIDMax is inventory_operations.id's bound.
	operationIDMax = 300
	qMax           = 200
	compatibleMax  = 100
	orderItemMax   = 100
	itemsMax       = 5000
	itemsDefault   = 1000
	movesMax       = 1000
	movesDefault   = 200
)

var (
	// kindID is models.py KIND_ID.
	kindID = regexp.MustCompile(`^[a-z][a-z0-9_]{0,39}$`)
	// unitStatus is the statuses a unit list filters by (phase 2's, and K1's ended).
	unitStatus = regexp.MustCompile(`^(in_stock|in_service|retired|scrapped|sold|ended)$`)
	// noNUL is api/inventory.py NO_NUL.
	noNUL = regexp.MustCompile(`^[^\x00]*$`)
)

// Reads are the read routes over pool. temporal, when not nil, is asked about an operation
// that has run for more than a minute (store.ReadOperation).
func Reads(pool *pgxpool.Pool, temporal store.Temporal) []crud.Route {
	f := flow.New(pool)
	r := &reads{pool: pool, flow: f, kinds: &store.Kinds{Cat: f.Reg.Catalog}, items: &store.Items{Cat: f.Reg.Catalog}, temporal: temporal}
	return []crud.Route{
		{Method: "GET", Path: "/operations/one", Handle: wrap(r.operation)},
		{Method: "GET", Path: "/kinds", Handle: wrap(r.kindList)},
		{Method: "GET", Path: "/kinds/{kind_id}", Handle: wrap(r.kind)},
		{Method: "GET", Path: "/kinds/{kind_id}/schema", Handle: wrap(r.kindSchema)},
		{Method: "GET", Path: "/items", Handle: wrap(r.itemList)},
		{Method: "GET", Path: "/items/{item_id}", Handle: wrap(r.item)},
		{Method: "GET", Path: "/items/{item_id}/lots", Handle: wrap(r.lotList)},
		{Method: "GET", Path: "/items/{item_id}/units", Handle: wrap(r.unitList)},
		{Method: "GET", Path: "/items/{item_id}/estimate", Handle: wrap(r.estimate)},
		{Method: "GET", Path: "/lots/{lot_id}", Handle: wrap(r.lot)},
		{Method: "GET", Path: "/units/{unit_id}", Handle: wrap(r.unit)},
		{Method: "GET", Path: "/moves", Handle: wrap(r.moveList)},
	}
}

type reads struct {
	pool     *pgxpool.Pool
	flow     *flow.Flow
	kinds    *store.Kinds
	items    *store.Items
	temporal store.Temporal
}

// wrap answers a refusal as its problem.
func wrap(h crud.Handler) crud.Handler {
	return func(r *http.Request, p crud.Params) (any, error) {
		out, err := h(r, p)
		if err != nil {
			return nil, problemOf(err)
		}
		return out, nil
	}
}

// pathUUID is the path parameter name as Python's UUID reads one (any case, with or
// without hyphens), in the lower-case hyphenated form; else the 422 pydantic writes.
func pathUUID(p crud.Params, name string) (string, error) {
	if id, ok := corewire.PyUUID(p[name]); ok {
		return id, nil
	}
	return "", httpx.Invalid(httpx.Issue{Loc: []string{"path", name},
		Msg: "Input should be a valid UUID, invalid character: expected an optional prefix of `urn:uuid:` followed by [0-9a-fA-F-]"})
}

func queryUUID(r *http.Request, name string) (string, error) {
	values := r.URL.Query()[name]
	if len(values) == 0 {
		return "", nil
	}
	if id, ok := corewire.PyUUID(values[len(values)-1]); ok {
		return id, nil
	}
	return "", httpx.Invalid(httpx.Issue{Loc: []string{"query", name},
		Msg: "Input should be a valid UUID, invalid character: expected an optional prefix of `urn:uuid:` followed by [0-9a-fA-F-]"})
}

// queryText is the optional query parameter name, at most most characters, with no NUL;
// "" when the request does not name it.
func queryText(r *http.Request, name string, most int, pattern *regexp.Regexp) (string, error) {
	v, err := crud.QueryOptText(r, name, 0, most)
	if err != nil || v == nil {
		return "", err
	}
	if !pattern.MatchString(*v) {
		return "", httpx.Invalid(httpx.Issue{Loc: []string{"query", name},
			Msg: fmt.Sprintf("String should match pattern '%s'", pattern)})
	}
	return *v, nil
}

func (s *reads) operation(r *http.Request, _ crud.Params) (any, error) {
	id, err := crud.QueryText(r, "id", operationIDMax)
	if err != nil {
		return nil, err
	}
	op, found, err := store.ReadOperation(r.Context(), s.pool, id, s.temporal)
	if err != nil {
		return nil, err
	}
	if !found {
		return nil, refuse("operation_not_found", fmt.Sprintf("no inventory operation %q", id))
	}
	return op, nil
}

type kindList struct {
	Kinds []wire.ItemKind `json:"kinds"`
}

func (s *reads) kindList(r *http.Request, _ crud.Params) (any, error) {
	archived, err := crud.QueryBool(r, "archived")
	if err != nil {
		return nil, err
	}
	kinds, err := s.kinds.List(r.Context(), s.pool, archived)
	if kinds == nil {
		kinds = []wire.ItemKind{}
	}
	return kindList{kinds}, err
}

func (s *reads) kind(r *http.Request, p crud.Params) (any, error) {
	id, err := p.Match("kind_id", kindID)
	if err != nil {
		return nil, err
	}
	return s.kinds.Get(r.Context(), s.pool, id)
}

type keyInUse struct {
	Key   string `json:"key"`
	Count int64  `json:"count"`
}

// kindSchema is models.py KindSchema. ItemRequired is the keys an item of the kind must
// have (store.Kinds.ItemRequired): the schema's required keys and the special type's.
type kindSchema struct {
	KindID       string     `json:"kind_id"`
	Schema       any        `json:"schema"`
	OwnSchema    any        `json:"own_schema"`
	KeysInUse    []keyInUse `json:"keys_in_use"`
	ItemRequired []string   `json:"item_required"`
}

func (s *reads) kindSchema(r *http.Request, p crud.Params) (any, error) {
	id, err := p.Match("kind_id", kindID)
	if err != nil {
		return nil, err
	}
	merged, own, keys, err := s.kinds.Schema(r.Context(), s.pool, id)
	if err != nil {
		return nil, err
	}
	required, err := s.kinds.ItemRequired(r.Context(), s.pool, id)
	if err != nil {
		return nil, err
	}
	out := kindSchema{KindID: id, Schema: merged, OwnSchema: own, KeysInUse: []keyInUse{}, ItemRequired: required}
	if len(own) == 0 || string(own) == "null" {
		out.OwnSchema = map[string]any{}
	}
	for _, k := range keys {
		out.KeysInUse = append(out.KeysInUse, keyInUse{Key: k[0].(string), Count: k[1].(int64)})
	}
	return out, nil
}

type itemList struct {
	Items []wire.Item `json:"items"`
}

func (s *reads) itemList(r *http.Request, _ crud.Params) (any, error) {
	var f store.ItemFilter
	var err error
	if f.Kind, err = queryText(r, "kind", 40, kindID); err != nil {
		return nil, err
	}
	if _, given := r.URL.Query()["descendants"]; given {
		d, err := crud.QueryBool(r, "descendants")
		if err != nil {
			return nil, err
		}
		f.Descendants = &d
	}
	if f.Q, err = queryText(r, "q", qMax, noNUL); err != nil {
		return nil, err
	}
	if f.Low, err = crud.QueryBool(r, "low"); err != nil {
		return nil, err
	}
	if f.Archived, err = crud.QueryBool(r, "archived"); err != nil {
		return nil, err
	}
	if f.Compatible, err = queryText(r, "compatible", compatibleMax, noNUL); err != nil {
		return nil, err
	}
	limit, given, err := crud.QueryInt(r, "limit", 1, itemsMax)
	if err != nil {
		return nil, err
	}
	f.Limit = itemsDefault
	if given {
		f.Limit = int(limit)
	}
	items, err := s.items.List(r.Context(), s.pool, f)
	if items == nil {
		items = []wire.Item{}
	}
	return itemList{items}, err
}

func (s *reads) item(r *http.Request, p crud.Params) (any, error) {
	id, err := pathUUID(p, "item_id")
	if err != nil {
		return nil, err
	}
	return s.items.Get(r.Context(), s.pool, id)
}

type lotList struct {
	Lots []wire.Lot `json:"lots"`
}

func (s *reads) lotList(r *http.Request, p crud.Params) (any, error) {
	id, err := pathUUID(p, "item_id")
	if err != nil {
		return nil, err
	}
	lots, err := store.ItemLots(r.Context(), s.pool, id)
	if lots == nil {
		lots = []wire.Lot{}
	}
	return lotList{lots}, err
}

type unitList struct {
	Units []wire.Unit `json:"units"`
}

func (s *reads) unitList(r *http.Request, p crud.Params) (any, error) {
	id, err := pathUUID(p, "item_id")
	if err != nil {
		return nil, err
	}
	status, err := queryText(r, "status", 20, unitStatus)
	if err != nil {
		return nil, err
	}
	units, err := store.ItemUnits(r.Context(), s.pool, id, status)
	if units == nil {
		units = []wire.Unit{}
	}
	return unitList{units}, err
}

func (s *reads) estimate(r *http.Request, p crud.Params) (any, error) {
	id, err := pathUUID(p, "item_id")
	if err != nil {
		return nil, err
	}
	text, err := crud.QueryText(r, "qty", 40)
	if err != nil {
		return nil, err
	}
	qty, err := store.Quantity(strings.TrimSpace(text))
	if err != nil {
		return nil, err
	}
	unit, unknown, err := s.flow.Estimate(r.Context(), id, qty)
	if err != nil {
		return nil, err
	}
	return wire.Estimate{UnitCostMinor: store.Text(unit), CostUnknown: unknown}, nil
}

func (s *reads) lot(r *http.Request, p crud.Params) (any, error) {
	id, err := pathUUID(p, "lot_id")
	if err != nil {
		return nil, err
	}
	l, found, err := store.Lot(r.Context(), s.pool, id)
	if err == nil && !found {
		err = refuse("lot_not_found", fmt.Sprintf("no lot '%s'", id))
	}
	return l, err
}

func (s *reads) unit(r *http.Request, p crud.Params) (any, error) {
	id, err := pathUUID(p, "unit_id")
	if err != nil {
		return nil, err
	}
	u, found, err := store.Unit(r.Context(), s.pool, id)
	if err == nil && !found {
		err = refuse("unit_not_found", fmt.Sprintf("no unit '%s'", id))
	}
	return u, err
}

type moveList struct {
	Moves []wire.Move `json:"moves"`
}

func (s *reads) moveList(r *http.Request, _ crud.Params) (any, error) {
	var f store.MoveFilter
	var err error
	if f.ItemID, err = queryUUID(r, "item_id"); err != nil {
		return nil, err
	}
	if f.LotID, err = queryUUID(r, "lot_id"); err != nil {
		return nil, err
	}
	if f.UnitID, err = queryUUID(r, "unit_id"); err != nil {
		return nil, err
	}
	if f.OrderItemID, err = queryText(r, "order_item_id", orderItemMax, noNUL); err != nil {
		return nil, err
	}
	limit, given, err := crud.QueryInt(r, "limit", 1, movesMax)
	if err != nil {
		return nil, err
	}
	f.Limit = movesDefault
	if given {
		f.Limit = int(limit)
	}
	ms, err := store.Moves(r.Context(), s.pool, f)
	if ms == nil {
		ms = []wire.Move{}
	}
	return moveList{ms}, err
}
