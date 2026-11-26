package store

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"math/big"
	"regexp"
	"slices"
	"sort"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/inventory/handlers"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/inventory/model"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/inventory/wire"
)

// Lots, units and moves (phase 2 inventory/stock.py, ported statement for statement with
// its locks, R-K1-1; spec §6.4 and §6.6):
//
//   - A lot item's stock is in lots, each with its own cost (D2). A take uses FIFO by
//     (received_at, id) unless the caller names a lot, one move per lot. Stock may go
//     negative: what no lot holds goes on the newest lot at its cost, flagged needs_review;
//     an item with no lot opens a zero-cost adjustment lot with its cost unknown.
//   - A move's cost is a snapshot (D14): a later lot cost change moves no past move, except
//     through Recost. GiveBack returns each use at the use's own cost, to the same lot.
//   - A unit item's stock is its units in_stock; a status change across that line writes
//     one move of the unit.
//   - Every write keeps stock_lots.on_hand equal to the sum of the lot's moves, and every
//     amount_minor is round(qty * unit_cost_minor, 6) (the stock_moves_amount CHECK).
//
// Locks: the item FOR SHARE (Lock, the base flow's first read), then the rows being changed
// FOR UPDATE. A take locks all of the item's lots in FIFO order, so takes, give-backs and
// counts of one item run one after another. A caller that takes or gives back several
// items in one transaction locks them first with LockItems (in item id order: each item,
// the lock that adds a lot, then its lots), so two such callers cannot deadlock (R-P2-28).
// Every lot insert and every count takes that same open lock before the lots, so no lot
// appears under a caller that holds LockItems (R-P2-32).
//
// Every call here runs in a transaction of store.Tx or store.Write, which takes the event
// log's lock and sets the actor before any row lock.

// recostKinds are the kinds of move a re-cost re-snapshots (phase 2 _RECOST_KINDS).
var recostKinds = []string{"use", "return", "adjust"}

// leaves is a unit's move when it leaves in_stock for the status (phase 2 _LEAVES).
var leaves = map[string]string{"in_service": "use", "sold": "sell", "retired": "retire", "scrapped": "scrap"}

// unitStatuses are the statuses a status change takes (models.py UnitStatus). ended is a
// service term's, which retire.term_end will set (K5).
var unitStatuses = []string{"in_stock", "in_service", "retired", "scrapped", "sold"}

// Target is what a step acts on: an item, or a lot or a unit of one.
type Target struct {
	Item, Lot, Unit string
}

// Subject is what the base flow resolves a step with: the item (the fields its read gives:
// id, kind, kind path, uom, steps, class, special type), its kind chain, and the unit's
// steps when the target is a unit.
type Subject struct {
	Item  wire.Item
	Chain model.Chain
	Unit  model.Steps
}

func lotNotFound(id string) error {
	return &Refused{Status: 404, Code: "lot_not_found", Detail: "no lot: " + id}
}

func unitNotFound(id string) error {
	return &Refused{Status: 404, Code: "unit_not_found", Detail: "no unit: " + id}
}

func locationNotFound(id string) error {
	return &Refused{Status: 404, Code: "location_not_found", Detail: "no location: " + id}
}

func lotEmpty(id string) error {
	return &Refused{Status: 409, Code: "lot_empty", Detail: "the named lot has no stock on hand: " + id}
}

func serialExists(serial string) error {
	return &Refused{Status: 409, Code: "serial_exists", Detail: "another unit of the item has that serial: " + serial}
}

func unitInstalled(id string) error {
	return &Refused{Status: 409, Code: "unit_installed", Detail: "the unit is fitted in another unit: " + id}
}

func wrongCounting(id string) error {
	return &Refused{Status: 409, Code: "wrong_counting",
		Detail: "a lot call on an item counted in units, or a unit call on one counted in lots: " + id}
}

// badInput is an input the public model refuses (a pydantic validation error in Python).
func badInput(format string, args ...any) error {
	return &Refused{Status: 422, Code: "bad_input", Detail: fmt.Sprintf(format, args...)}
}

// ClassRefusal is a class that does not resolve, as a refusal: class_changed, class_open or
// class_invalid.
func ClassRefusal(err error) error { return classRefusal(err) }

// StepsRefusal is 422 steps_invalid for the problems that refuse a save or a call
// (handlers.Blocking), or nil.
func StepsRefusal(problems []handlers.Problem) error { return stepsRefusal(problems) }

func isCode(err error, code string) bool {
	var pg *pgconn.PgError
	return errors.As(err, &pg) && pg.Code == code
}

// checkText checks a text field as models.py Text does: no NUL, at most max characters.
func checkText(field, s string, max int) error {
	if strings.ContainsRune(s, 0) || utf8.RuneCountInString(s) > max {
		return badInput("%s is at most %d characters with no NUL", field, max)
	}
	return nil
}

func checkUUID(field string, s *string) error {
	if s != nil && !uuidText.MatchString(*s) {
		return badInput("%s is not a UUID: %q", field, *s)
	}
	return nil
}

var currencyText = regexp.MustCompile(`^[A-Z]{3}$`)

// Lock reads what a step resolves with: the target's item, FOR SHARE when lock (so its kind
// cannot change meanwhile), its kind chain, and a unit's steps. item_not_found,
// lot_not_found or unit_not_found; a class that does not resolve; a special type whose
// items wait for their phase (filament_item_refused, phase 2 Ruling 9).
func Lock(ctx context.Context, q Querier, at Target, lock bool) (Subject, error) {
	itemID := at.Item
	switch {
	case at.Lot != "":
		if !uuidText.MatchString(at.Lot) {
			return Subject{}, lotNotFound(at.Lot)
		}
		err := q.QueryRow(ctx, `SELECT item_id::text FROM stock_lots WHERE id = $1::uuid`, at.Lot).Scan(&itemID)
		if errors.Is(err, pgx.ErrNoRows) {
			return Subject{}, lotNotFound(at.Lot)
		}
		if err != nil {
			return Subject{}, err
		}
	case at.Unit != "":
		if !uuidText.MatchString(at.Unit) {
			return Subject{}, unitNotFound(at.Unit)
		}
		err := q.QueryRow(ctx, `SELECT item_id::text FROM stock_units WHERE id = $1::uuid`, at.Unit).Scan(&itemID)
		if errors.Is(err, pgx.ErrNoRows) {
			return Subject{}, unitNotFound(at.Unit)
		}
		if err != nil {
			return Subject{}, err
		}
	}
	if !uuidText.MatchString(itemID) {
		return Subject{}, itemNotFound(itemID)
	}
	sql := `SELECT i.id::text, i.kind_id, k.uom, i.steps FROM items i JOIN item_kinds k ON k.id = i.kind_id
		WHERE i.id = $1::uuid`
	if lock {
		sql += ` FOR SHARE OF i`
	}
	var sub Subject
	var steps []byte
	err := q.QueryRow(ctx, sql, itemID).Scan(&sub.Item.ID, &sub.Item.KindID, &sub.Item.UOM, &steps)
	if errors.Is(err, pgx.ErrNoRows) {
		return Subject{}, itemNotFound(itemID)
	}
	if err != nil {
		return Subject{}, err
	}
	if err := json.Unmarshal(steps, &sub.Item.Steps); err != nil {
		return Subject{}, err
	}
	if at.Unit != "" {
		var unit []byte
		if err := q.QueryRow(ctx, `SELECT steps FROM stock_units WHERE id = $1::uuid`, at.Unit).Scan(&unit); err != nil {
			return Subject{}, err
		}
		if err := json.Unmarshal(unit, &sub.Unit); err != nil {
			return Subject{}, err
		}
	}
	set, err := loadKinds(ctx, q)
	if err != nil {
		return Subject{}, err
	}
	sub.Chain = set.chain(sub.Item.KindID)
	sub.Item.KindPath = set.path(sub.Item.KindID)
	cls, special, err := sub.Chain.Class()
	if err != nil {
		return Subject{}, classRefusal(err)
	}
	if r, ok := refusedSpecial[special]; ok {
		refused := *r
		refused.Detail += ": " + sub.Item.ID
		return Subject{}, &refused
	}
	sub.Item.Class = wire.Class{Nature: opt(cls.Nature), Role: opt(cls.Role), Counting: opt(cls.Counting), Billing: opt(cls.Billing)}
	sub.Item.SpecialType = opt(special)
	return sub, nil
}

// lotItem is phase 2 _item(conn, item_id, "lot", lock): the item, refused unless it is
// counted in lots (step_not_applicable for one that is not counted, R-K1-23).
func lotItem(ctx context.Context, tx pgx.Tx, itemID string, lock bool) (wire.Item, error) {
	return countedItem(ctx, tx, itemID, "lot", lock)
}

func countedItem(ctx context.Context, tx pgx.Tx, itemID, counting string, lock bool) (wire.Item, error) {
	sub, err := Lock(ctx, tx, Target{Item: itemID}, lock)
	if err != nil {
		return wire.Item{}, err
	}
	switch got := deref(sub.Item.Class.Counting); {
	case got == "":
		return wire.Item{}, &Refused{Status: 409, Code: "step_not_applicable",
			Detail: "the item's class is not counted: " + sub.Item.ID, Fields: map[string]any{"class": sub.Item.Class}}
	case got != counting:
		return wire.Item{}, wrongCounting(sub.Item.ID)
	}
	return sub.Item, nil
}

// -- reads -----------------------------------------------------------------------------

// The columns of a lot, a unit and a move as the wire reads them: decimals as plain text
// (trim_scale, as Python's format(value.normalize(), "f")).
const (
	lotColumns = `id::text, item_id::text, location_id::text, trim_scale(received_qty)::text,
	trim_scale(on_hand)::text, trim_scale(unit_cost_minor)::text, cost_unknown, cost_currency,
	trim_scale(foreign_unit_cost_minor)::text, origin, invoice_line_id::text, production_run_id::text, note,
	received_at, created_by`
	unitColumns = `id::text, item_id::text, serial, name, location_id::text, status, installed_in_unit_id::text,
	trim_scale(cost_minor)::text, cost_unknown, origin, invoice_line_id::text, production_run_id::text,
	in_service_at, retired_at, trim_scale(life_hours)::text, life_months::bigint, printer_id::text,
	location_ref_id::text, received_at, created_by, steps, starts_at, ends_at`
	moveColumns = `id::text, at, item_id::text, lot_id::text, unit_id::text, kind, trim_scale(qty)::text,
	trim_scale(unit_cost_minor)::text, trim_scale(amount_minor)::text, cost_unknown, needs_review,
	reverses_move_id::text, reason, order_id, order_item_id, production_run_id::text, print_run_id,
	entity_id::text, actor`
)

func scanLot(row pgx.Row) (wire.Lot, error) {
	var l wire.Lot
	err := row.Scan(&l.ID, &l.ItemID, &l.LocationID, &l.ReceivedQty, &l.OnHand, &l.UnitCostMinor, &l.CostUnknown,
		&l.CostCurrency, &l.ForeignUnitCostMinor, &l.Origin, &l.InvoiceLineID, &l.ProductionRunID, &l.Note,
		&l.ReceivedAt, &l.CreatedBy)
	return l, err
}

func scanUnit(row pgx.Row) (wire.Unit, error) {
	var u wire.Unit
	var steps []byte
	err := row.Scan(&u.ID, &u.ItemID, &u.Serial, &u.Name, &u.LocationID, &u.Status, &u.InstalledInUnitID,
		&u.CostMinor, &u.CostUnknown, &u.Origin, &u.InvoiceLineID, &u.ProductionRunID, &u.InServiceAt,
		&u.RetiredAt, &u.LifeHours, &u.LifeMonths, &u.PrinterID, &u.LocationRefID, &u.ReceivedAt, &u.CreatedBy,
		&steps, &u.StartsAt, &u.EndsAt)
	if err != nil {
		return u, err
	}
	return u, json.Unmarshal(steps, &u.Steps)
}

func scanMove(row pgx.Row) (wire.Move, error) {
	var m wire.Move
	err := row.Scan(&m.ID, &m.At, &m.ItemID, &m.LotID, &m.UnitID, &m.Kind, &m.Qty, &m.UnitCostMinor, &m.AmountMinor,
		&m.CostUnknown, &m.NeedsReview, &m.ReversesMoveID, &m.Reason, &m.OrderID, &m.OrderItemID,
		&m.ProductionRunID, &m.PrintRunID, &m.EntityID, &m.Actor)
	return m, err
}

func collect[T any](ctx context.Context, q Querier, scan func(pgx.Row) (T, error), sql string, args ...any) ([]T, error) {
	rows, err := q.Query(ctx, sql, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []T{}
	for rows.Next() {
		v, err := scan(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, v)
	}
	return out, rows.Err()
}

// Lot is the lot, and whether there is one.
func Lot(ctx context.Context, q Querier, id string) (wire.Lot, bool, error) {
	if !uuidText.MatchString(id) {
		return wire.Lot{}, false, nil
	}
	l, err := scanLot(q.QueryRow(ctx, `SELECT `+lotColumns+` FROM stock_lots WHERE id = $1::uuid`, id))
	if errors.Is(err, pgx.ErrNoRows) {
		return wire.Lot{}, false, nil
	}
	return l, err == nil, err
}

// Lots is the item's lots in FIFO order.
func Lots(ctx context.Context, q Querier, itemID string) ([]wire.Lot, error) {
	if !uuidText.MatchString(itemID) {
		return []wire.Lot{}, nil
	}
	return collect(ctx, q, scanLot, `SELECT `+lotColumns+` FROM stock_lots WHERE item_id = $1::uuid
		ORDER BY received_at, id`, itemID)
}

// Unit is the unit, and whether there is one.
func Unit(ctx context.Context, q Querier, id string) (wire.Unit, bool, error) {
	if !uuidText.MatchString(id) {
		return wire.Unit{}, false, nil
	}
	u, err := scanUnit(q.QueryRow(ctx, `SELECT `+unitColumns+` FROM stock_units WHERE id = $1::uuid`, id))
	if errors.Is(err, pgx.ErrNoRows) {
		return wire.Unit{}, false, nil
	}
	return u, err == nil, err
}

// Units is the item's units, oldest first; only those of status when it is not empty.
func Units(ctx context.Context, q Querier, itemID, status string) ([]wire.Unit, error) {
	if !uuidText.MatchString(itemID) {
		return []wire.Unit{}, nil
	}
	return collect(ctx, q, scanUnit, `SELECT `+unitColumns+` FROM stock_units WHERE item_id = $1::uuid
		AND ($2 = '' OR status = $2) ORDER BY received_at, id`, itemID, status)
}

// MoveFilter is the moves to read: those matching every filter given, up to Limit (200 when
// 0).
type MoveFilter struct {
	ItemID, LotID, UnitID, OrderItemID string
	Limit                              int
}

// Moves is the moves matching f, newest first.
func Moves(ctx context.Context, q Querier, f MoveFilter) ([]wire.Move, error) {
	if f.Limit == 0 {
		f.Limit = 200
	}
	var where []string
	var args []any
	for _, c := range []struct{ column, value string }{{"item_id", f.ItemID}, {"lot_id", f.LotID}, {"unit_id", f.UnitID}} {
		if c.value == "" {
			continue
		}
		if !uuidText.MatchString(c.value) {
			return []wire.Move{}, nil
		}
		args = append(args, c.value)
		where = append(where, fmt.Sprintf("%s = $%d::uuid", c.column, len(args)))
	}
	if f.OrderItemID != "" {
		args = append(args, f.OrderItemID)
		where = append(where, fmt.Sprintf("order_item_id = $%d", len(args)))
	}
	clause := ""
	if len(where) > 0 {
		clause = " WHERE " + strings.Join(where, " AND ")
	}
	args = append(args, f.Limit)
	return collect(ctx, q, scanMove, `SELECT `+moveColumns+` FROM stock_moves`+clause+
		fmt.Sprintf(` ORDER BY at DESC, id DESC LIMIT $%d`, len(args)), args...)
}

// -- locks and moves -------------------------------------------------------------------

// lotRow is a lot as a take, a count or a re-cost computes with it.
type lotRow struct {
	ID, ItemID                string
	OnHand, Cost, ReceivedQty *big.Rat
	Unknown                   bool
}

const lotCalc = `id::text, item_id::text, on_hand::text, unit_cost_minor::text, received_qty::text, cost_unknown`

func scanLotRow(row pgx.Row) (lotRow, error) {
	var l lotRow
	var onHand, cost, received string
	err := row.Scan(&l.ID, &l.ItemID, &onHand, &cost, &received, &l.Unknown)
	l.OnHand, l.Cost, l.ReceivedQty = numeric(onHand), numeric(cost), numeric(received)
	return l, err
}

// lockLots is phase 2 _lock_lots: the item's lots FOR UPDATE, in FIFO order (one lock order
// for every caller).
func lockLots(ctx context.Context, tx pgx.Tx, itemID string) ([]lotRow, error) {
	return collect(ctx, tx, scanLotRow, `SELECT `+lotCalc+` FROM stock_lots WHERE item_id = $1::uuid
		ORDER BY received_at, id FOR UPDATE`, itemID)
}

// openLock is phase 2 _open_lock: the transaction's lock on adding a lot to the item. Every
// lot insert takes it first, and so do LockItems and a count.
func openLock(ctx context.Context, tx pgx.Tx, itemID string) error {
	_, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock(hashtext('stock_lots_open'), hashtext($1))`, itemID)
	return err
}

// openLots is phase 2 _open_lots: for an item that had no lot, a zero-cost adjustment lot of
// unknown cost. Two callers that both found none wait on one lock, and the second takes
// the lot the first opened.
func openLots(ctx context.Context, tx pgx.Tx, itemID string, by json.RawMessage) ([]lotRow, error) {
	if err := openLock(ctx, tx, itemID); err != nil {
		return nil, err
	}
	lots, err := lockLots(ctx, tx, itemID)
	if err != nil || len(lots) > 0 {
		return lots, err
	}
	l, err := scanLotRow(tx.QueryRow(ctx, `INSERT INTO stock_lots (item_id, received_qty, on_hand, unit_cost_minor,
		cost_unknown, origin, created_by) VALUES ($1::uuid, 0, 0, 0, true, 'adjustment', $2::jsonb) RETURNING `+lotCalc,
		itemID, string(by)))
	if err != nil {
		return nil, err
	}
	return []lotRow{l}, nil
}

// lockLot is phase 2 _lock_lot: the lot FOR UPDATE, after its item FOR SHARE.
func lockLot(ctx context.Context, tx pgx.Tx, lotID string) (lotRow, wire.Item, error) {
	if !uuidText.MatchString(lotID) {
		return lotRow{}, wire.Item{}, lotNotFound(lotID)
	}
	var itemID string
	err := tx.QueryRow(ctx, `SELECT item_id::text FROM stock_lots WHERE id = $1::uuid`, lotID).Scan(&itemID)
	if errors.Is(err, pgx.ErrNoRows) {
		return lotRow{}, wire.Item{}, lotNotFound(lotID)
	}
	if err != nil {
		return lotRow{}, wire.Item{}, err
	}
	item, err := lotItem(ctx, tx, itemID, true)
	if err != nil {
		return lotRow{}, wire.Item{}, err
	}
	l, err := scanLotRow(tx.QueryRow(ctx, `SELECT `+lotCalc+` FROM stock_lots WHERE id = $1::uuid FOR UPDATE`, lotID))
	return l, item, err
}

// lockedLot is the lot FOR UPDATE, when the base flow has locked its item.
func lockedLot(ctx context.Context, tx pgx.Tx, lotID string) (lotRow, error) {
	l, err := scanLotRow(tx.QueryRow(ctx, `SELECT `+lotCalc+` FROM stock_lots WHERE id = $1::uuid FOR UPDATE`, lotID))
	if errors.Is(err, pgx.ErrNoRows) {
		return lotRow{}, lotNotFound(lotID)
	}
	return l, err
}

// lockUnit is phase 2 _lock_unit: the unit FOR UPDATE, after its item FOR SHARE.
func lockUnit(ctx context.Context, tx pgx.Tx, unitID string) error {
	if !uuidText.MatchString(unitID) {
		return unitNotFound(unitID)
	}
	var itemID string
	err := tx.QueryRow(ctx, `SELECT item_id::text FROM stock_units WHERE id = $1::uuid`, unitID).Scan(&itemID)
	if errors.Is(err, pgx.ErrNoRows) {
		return unitNotFound(unitID)
	}
	if err != nil {
		return err
	}
	if _, err := countedItem(ctx, tx, itemID, "unit", true); err != nil {
		return err
	}
	_, err = tx.Exec(ctx, `SELECT 1 FROM stock_units WHERE id = $1::uuid FOR UPDATE`, unitID)
	return err
}

// move is one move, written as phase 2 _move writes it, and its lot's on_hand moved by its
// qty.
type move struct {
	Item, Kind                     string
	Lot, Unit                      *string
	Qty, Cost                      *big.Rat
	Unknown, Review                bool
	Reverses                       *string
	Reason                         string
	OrderID, OrderItemID, EntityID *string
	At                             *time.Time
	By                             json.RawMessage
}

func (m move) write(ctx context.Context, tx pgx.Tx) (wire.Move, error) {
	amt, err := amount(m.Qty, m.Cost)
	if err != nil {
		return wire.Move{}, err
	}
	out, err := scanMove(tx.QueryRow(ctx, `INSERT INTO stock_moves (at, item_id, lot_id, unit_id, kind, qty,
		unit_cost_minor, amount_minor, cost_unknown, needs_review, reverses_move_id, reason, order_id, order_item_id,
		entity_id, actor) VALUES (coalesce($1::timestamptz, clock_timestamp()), $2::uuid, $3::uuid, $4::uuid, $5,
		$6::numeric, $7::numeric, $8::numeric, $9, $10, $11::uuid, $12, $13, $14, $15::uuid, $16::jsonb)
		RETURNING `+moveColumns,
		m.At, m.Item, m.Lot, m.Unit, m.Kind, arg(m.Qty), arg(m.Cost), arg(amt), m.Unknown, m.Review, m.Reverses,
		m.Reason, m.OrderID, m.OrderItemID, m.EntityID, string(m.By)))
	if err != nil {
		return wire.Move{}, err
	}
	if m.Lot != nil {
		var id string
		err := tx.QueryRow(ctx, `UPDATE stock_lots SET on_hand = on_hand + $1::numeric WHERE id = $2::uuid
			AND abs(on_hand + $1::numeric) < $3::numeric RETURNING id::text`, arg(m.Qty), *m.Lot, arg(limit)).Scan(&id)
		if errors.Is(err, pgx.ErrNoRows) {
			return wire.Move{}, badQuantity(text(m.Qty))
		}
		if err != nil {
			return wire.Move{}, err
		}
	}
	return out, nil
}

// part is what a take takes from one lot (phase 2 _fifo's tuples).
type part struct {
	lot   lotRow
	qty   *big.Rat
	short bool
}

// fifo is phase 2 _fifo: what a take of qty takes from each lot, the named lot first, then
// FIFO over the lots with stock; the rest on the newest lot. lots are in FIFO order and not
// empty. A named lot with no stock on hand is lot_empty (R-P2-14).
func fifo(lots []lotRow, qty *big.Rat, lotID string) ([]part, error) {
	order := lots
	if lotID != "" {
		i := slices.IndexFunc(lots, func(l lotRow) bool { return strings.EqualFold(l.ID, lotID) })
		if i < 0 {
			return nil, lotNotFound(lotID)
		}
		if lots[i].OnHand.Sign() <= 0 {
			return nil, lotEmpty(lotID)
		}
		order = append([]lotRow{lots[i]}, slices.Delete(slices.Clone(lots), i, i+1)...)
	}
	left := new(big.Rat).Set(qty)
	var out []part
	for _, l := range order {
		if left.Sign() <= 0 {
			break
		}
		if l.OnHand.Sign() <= 0 {
			continue
		}
		p := l.OnHand
		if left.Cmp(p) < 0 {
			p = left
		}
		out = append(out, part{l, new(big.Rat).Set(p), false})
		left.Sub(left, p)
	}
	if left.Sign() > 0 {
		out = append(out, part{lots[len(lots)-1], left, true})
	}
	return out, nil
}

// -- entities --------------------------------------------------------------------------

// entityKind is entities/registry.py EntityKind: the owner's table of an entity kind, and
// whether its id is a uuid. stockColumn is the stock_units column that names an owner of
// the kind (a printer's units), which Repoint moves too.
type entityKind struct {
	table       string
	uuid        bool
	stockColumn string
}

// entityKinds is entities/registry.py KINDS.
var entityKinds = map[string]entityKind{
	"order": {table: "orders"}, "order_item": {table: "order_items"},
	"order_item_fulfilment": {table: "order_item_fulfilments"}, "order_item_revision": {table: "order_item_revisions"},
	"spool": {table: "filament_spools", uuid: true}, "spool_type": {table: "filament_spool_types", uuid: true},
	"supplier": {table: "filament_suppliers", uuid: true}, "invoice": {table: "filament_invoices", uuid: true},
	"location": {table: "filament_locations", uuid: true},
	"printer":  {table: "printers", uuid: true, stockColumn: "printer_id"},
	"customer": {table: "customers"}, "item": {table: "items", uuid: true},
	"lot": {table: "stock_lots", uuid: true}, "unit": {table: "stock_units", uuid: true},
	"material": {table: "filament_materials", uuid: true}, "subtype": {table: "filament_subtypes", uuid: true},
	"brand": {table: "filament_brands", uuid: true}, "category": {table: "filament_categories", uuid: true},
	"effect": {table: "filament_effects", uuid: true}, "insert_material": {table: "filament_insert_materials", uuid: true},
	"insert_type": {table: "filament_insert_types", uuid: true}, "colour": {table: "filament_colours", uuid: true},
	"location_kind": {table: "filament_location_kinds", uuid: true}, "item_kind": {table: "item_kinds"},
}

// entityRefProblem is entities/problems.py PROBLEMS: an entity reference's codes keep the
// entities' type (inventory/problems.py refused).
const entityRefProblem = "https://scadbuddy.dev/problems/entities/"

var entityRefText = regexp.MustCompile(`(?s)^([a-z_]{1,40}):(.+)$`)

var canonicalUUID = regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`)

func entityRefused(status int, code, detail string) error {
	return &Refused{Status: status, Code: code, Detail: detail, Type: entityRefProblem + code}
}

// entity is phase 2 StockStore._entity: the registry row of kind:ref, made on first use.
// bad_entity_ref (a NUL too), unknown_entity_kind or entity_not_found.
func entity(ctx context.Context, tx pgx.Tx, ref *string) (*string, error) {
	if ref == nil {
		return nil, nil
	}
	found := entityRefText.FindStringSubmatch(*ref)
	if strings.ContainsRune(*ref, 0) || found == nil {
		return nil, entityRefused(422, "bad_entity_ref",
			"an entity is named `kind:ref`, such as `spool:<id>` or `print_archive:<archive id>`")
	}
	kind, owner := found[1], found[2]
	k, ok := entityKinds[kind]
	if !ok {
		return nil, entityRefused(422, "unknown_entity_kind", fmt.Sprintf("no entity kind %q", kind))
	}
	exists := !k.uuid || canonicalUUID.MatchString(owner)
	if exists {
		err := tx.QueryRow(ctx, `SELECT 1 FROM `+k.table+` WHERE id = $1`, owner).Scan(new(int))
		if errors.Is(err, pgx.ErrNoRows) {
			exists = false
		} else if err != nil {
			return nil, err
		}
	}
	if !exists {
		return nil, entityRefused(404, "entity_not_found", fmt.Sprintf("no %s %q", kind, owner))
	}
	id, err := EnsureEntity(ctx, tx, kind, owner)
	return &id, err
}

// -- the steps (handlers.Stock) --------------------------------------------------------

// Stock is the stock store the K1 default handlers write through (handlers.Stock). The
// base flow has locked the item (Lock) before any of its calls.
type Stock struct{}

var _ handlers.Stock = Stock{}

func checkReceiveLot(in wire.ReceiveLot) error {
	if in.UnitCostMinor != nil && in.TotalCostMinor != nil {
		return badInput("give unit_cost_minor or total_cost_minor, not both")
	}
	if in.Currency != nil && !currencyText.MatchString(*in.Currency) {
		return badInput("a currency is three capital letters: %q", *in.Currency)
	}
	if err := checkUUID("location_id", in.LocationID); err != nil {
		return err
	}
	return checkText("note", in.Note, 4000)
}

// ReceiveLot is phase 2 receive_in: a new lot (origin manual) and its receive move. A cost
// in a currency other than the shop's opens it at 0 with its cost unknown, cost_currency
// set and the cost given kept as foreign_unit_cost_minor (R-7067-11, D11, R-P2-11); a
// receive with no cost opens at 0 with its cost unknown too.
func (Stock) ReceiveLot(ctx context.Context, tx pgx.Tx, item wire.Item, in wire.ReceiveLot, shop string, by json.RawMessage) (wire.Lot, error) {
	if err := checkReceiveLot(in); err != nil {
		return wire.Lot{}, err
	}
	qty, err := decimal(in.Qty)
	if err != nil {
		return wire.Lot{}, err
	}
	if err := positive(item.UOM, qty); err != nil {
		return wire.Lot{}, err
	}
	currency := shop
	if in.Currency != nil {
		currency = *in.Currency
	}
	var given *big.Rat
	switch {
	case in.UnitCostMinor != nil:
		if given, err = cost(*in.UnitCostMinor); err != nil {
			return wire.Lot{}, err
		}
	case in.TotalCostMinor != nil:
		total, err := cost(*in.TotalCostMinor)
		if err != nil {
			return wire.Lot{}, err
		}
		given = six(new(big.Rat).Quo(total, qty))
	}
	unitCost, unknown := new(big.Rat), true
	var costCurrency, foreign *string
	switch {
	case currency != shop:
		costCurrency = &currency
		if given != nil {
			foreign = ptrTo(arg(given))
		}
	case given != nil:
		unitCost, unknown = given, false
	}
	// Every lot insert takes the item's open lock first, so no lot appears under a caller
	// that holds LockItems (R-P2-32).
	if err := openLock(ctx, tx, item.ID); err != nil {
		return wire.Lot{}, err
	}
	var id string
	var at time.Time
	err = tx.QueryRow(ctx, `INSERT INTO stock_lots (item_id, location_id, received_qty, on_hand, unit_cost_minor,
		cost_unknown, cost_currency, foreign_unit_cost_minor, origin, note, received_at, created_by)
		VALUES ($1::uuid, $2::uuid, $3::numeric, 0, $4::numeric, $5, $6, $7::numeric, 'manual', $8,
		coalesce($9::timestamptz, clock_timestamp()), $10::jsonb) RETURNING id::text, received_at`,
		item.ID, in.LocationID, arg(qty), arg(unitCost), unknown, costCurrency, foreign, in.Note, in.ReceivedAt,
		string(by)).Scan(&id, &at)
	if isCode(err, "23503") {
		return wire.Lot{}, locationNotFound(deref(in.LocationID))
	}
	if err != nil {
		return wire.Lot{}, err
	}
	if _, err := (move{Item: item.ID, Lot: &id, Kind: "receive", Qty: qty, Cost: unitCost, Unknown: unknown,
		Reason: in.Note, By: by, At: &at}).write(ctx, tx); err != nil {
		return wire.Lot{}, err
	}
	l, _, err := Lot(ctx, tx, id)
	return l, err
}

func ptrTo(s string) *string { return &s }

func checkReceiveUnits(in wire.ReceiveUnits) error {
	if in.Count < 1 || in.Count > 100 {
		return badInput("count is 1 to 100, not %d", in.Count)
	}
	if in.Serials != nil && len(in.Serials) != in.Count {
		return badInput("serials has one entry per unit")
	}
	for _, s := range in.Serials {
		if err := checkText("a serial", s, 200); err != nil {
			return err
		}
	}
	if err := checkUUID("location_id", in.LocationID); err != nil {
		return err
	}
	return checkText("name", in.Name, 200)
}

// ReceiveUnits is phase 2 receive_units: count units in_stock, each with a receive move.
// With no cost_minor their cost is unknown. A blank serial is stored as null.
func (Stock) ReceiveUnits(ctx context.Context, tx pgx.Tx, item wire.Item, in wire.ReceiveUnits, by json.RawMessage) ([]wire.Unit, error) {
	if err := checkReceiveUnits(in); err != nil {
		return nil, err
	}
	unitCost, unknown := new(big.Rat), true
	if in.CostMinor != nil {
		c, err := cost(*in.CostMinor)
		if err != nil {
			return nil, err
		}
		unitCost, unknown = c, false
	}
	serials := in.Serials
	if len(serials) == 0 {
		serials = make([]string, in.Count)
	}
	made := []string{}
	for _, raw := range serials {
		var serial *string
		if s := strings.TrimSpace(raw); s != "" {
			serial = &s
		}
		var id string
		var at time.Time
		err := tx.QueryRow(ctx, `INSERT INTO stock_units (item_id, serial, name, location_id, cost_minor, cost_unknown,
			origin, created_by) VALUES ($1::uuid, $2, $3, $4::uuid, $5::numeric, $6, 'manual', $7::jsonb)
			RETURNING id::text, received_at`,
			item.ID, serial, in.Name, in.LocationID, arg(unitCost), unknown, string(by)).Scan(&id, &at)
		switch {
		case isCode(err, "23505"):
			return nil, serialExists(deref(serial))
		case isCode(err, "23503"):
			return nil, locationNotFound(deref(in.LocationID))
		case err != nil:
			return nil, err
		}
		if _, err := (move{Item: item.ID, Unit: &id, Kind: "receive", Qty: big.NewRat(1, 1), Cost: unitCost,
			Unknown: unknown, By: by, At: &at}).write(ctx, tx); err != nil {
			return nil, err
		}
		made = append(made, id)
	}
	return collect(ctx, tx, scanUnit, `SELECT `+unitColumns+` FROM stock_units WHERE id = ANY($1::uuid[])
		ORDER BY received_at, id`, made)
}

func checkUse(in wire.UseInput) error {
	if err := checkUUID("lot_id", in.LotID); err != nil {
		return err
	}
	if in.Entity != nil && utf8.RuneCountInString(*in.Entity) > 200 {
		return badInput("an entity is at most 200 characters")
	}
	for field, v := range map[string]*string{"order_id": in.OrderID, "order_item_id": in.OrderItemID} {
		if v != nil {
			if err := checkText(field, *v, 200); err != nil {
				return err
			}
		}
	}
	return checkText("reason", in.Reason, 4000)
}

// Take is phase 2 use and take: qty out of the item's lots, the named lot first, then FIFO;
// what no lot holds goes on the newest lot at its cost with needs_review, and an item with
// no lot opens a zero-cost adjustment lot of unknown cost (spec §6.6). One move per lot, at
// the lot's cost now (cost.lot_fifo's snapshot), on the entity in.Entity names.
func (Stock) Take(ctx context.Context, tx pgx.Tx, item wire.Item, in wire.UseInput, by json.RawMessage) ([]wire.Move, error) {
	if err := checkUse(in); err != nil {
		return nil, err
	}
	qty, err := decimal(in.Qty)
	if err != nil {
		return nil, err
	}
	if err := positive(item.UOM, qty); err != nil {
		return nil, err
	}
	entityID, err := entity(ctx, tx, in.Entity)
	if err != nil {
		return nil, err
	}
	lots, err := lockLots(ctx, tx, item.ID)
	if err != nil {
		return nil, err
	}
	named := deref(in.LotID)
	// Before a lot is opened, so a caller that goes on in its transaction keeps none.
	if named != "" && !slices.ContainsFunc(lots, func(l lotRow) bool { return strings.EqualFold(l.ID, named) }) {
		return nil, lotNotFound(named)
	}
	if len(lots) == 0 {
		if lots, err = openLots(ctx, tx, item.ID, by); err != nil {
			return nil, err
		}
	}
	plan, err := fifo(lots, qty, named)
	if err != nil {
		return nil, err
	}
	out := []wire.Move{}
	for _, p := range plan {
		m, err := (move{Item: item.ID, Lot: &p.lot.ID, Kind: "use", Qty: new(big.Rat).Neg(p.qty), Cost: p.lot.Cost,
			Unknown: p.lot.Unknown, Review: p.short, Reason: in.Reason, OrderID: in.OrderID,
			OrderItemID: in.OrderItemID, EntityID: entityID, By: by}).write(ctx, tx)
		if err != nil {
			return nil, err
		}
		out = append(out, m)
	}
	return out, nil
}

// counted is a count's on_hand and note as models.py CountInput and AdjustInput take them:
// 0 or more, whole for an each item, and a note that is not blank (stripped).
func counted(uom string, onHand wire.Decimal, note string) (*big.Rat, string, error) {
	note = strings.TrimSpace(note)
	if note == "" {
		return nil, "", badInput("a count needs a note")
	}
	if err := checkText("note", note, 4000); err != nil {
		return nil, "", err
	}
	qty, err := decimal(onHand)
	if err != nil {
		return nil, "", err
	}
	if qty.Sign() < 0 {
		return nil, "", badQuantity(string(onHand))
	}
	return qty, note, whole(uom, qty)
}

// CountItem is phase 2 count_item: a count of the whole item, as adjust moves, one per lot
// it changes, in FIFO order. No lot keeps an offsetting negative balance (R-P2-15): each
// negative lot is brought to 0, then a lower count takes the difference FIFO from the lots
// with stock, and a higher one adds it to the newest lot at its cost (a new adjustment lot
// of unknown cost when there is none).
func (Stock) CountItem(ctx context.Context, tx pgx.Tx, item wire.Item, in wire.CountInput, by json.RawMessage) ([]wire.Move, error) {
	onHand, note, err := counted(item.UOM, in.OnHand, in.Note)
	if err != nil {
		return nil, err
	}
	// The lock order of LockItems: the open lock, then the lots (R-P2-32).
	if err := openLock(ctx, tx, item.ID); err != nil {
		return nil, err
	}
	lots, err := lockLots(ctx, tx, item.ID)
	if err != nil {
		return nil, err
	}
	change := map[string]*big.Rat{}
	stocked := new(big.Rat)
	for _, l := range lots {
		if l.OnHand.Sign() < 0 {
			change[l.ID] = new(big.Rat).Neg(l.OnHand)
		} else {
			stocked.Add(stocked, l.OnHand)
		}
	}
	add := func(id string, by *big.Rat) {
		if change[id] == nil {
			change[id] = new(big.Rat)
		}
		change[id].Add(change[id], by)
	}
	delta := new(big.Rat).Sub(onHand, stocked)
	switch delta.Sign() {
	case -1:
		plan, err := fifo(lots, new(big.Rat).Neg(delta), "")
		if err != nil {
			return nil, err
		}
		for _, p := range plan {
			add(p.lot.ID, new(big.Rat).Neg(p.qty))
		}
	case 1:
		if len(lots) == 0 {
			if lots, err = openLots(ctx, tx, item.ID, by); err != nil {
				return nil, err
			}
		}
		add(lots[len(lots)-1].ID, delta)
	}
	out := []wire.Move{}
	for _, l := range lots {
		c := change[l.ID]
		if c == nil || c.Sign() == 0 {
			continue
		}
		m, err := (move{Item: item.ID, Lot: &l.ID, Kind: "adjust", Qty: c, Cost: l.Cost, Unknown: l.Unknown,
			Reason: note, By: by}).write(ctx, tx)
		if err != nil {
			return nil, err
		}
		out = append(out, m)
	}
	return out, nil
}

// AdjustLot is phase 2 adjust_lot: a count of one lot, one adjust move of the difference at
// the lot's cost, none when the count matches.
func (Stock) AdjustLot(ctx context.Context, tx pgx.Tx, item wire.Item, lotID string, in wire.AdjustInput, by json.RawMessage) ([]wire.Move, error) {
	l, err := lockedLot(ctx, tx, lotID)
	if err != nil {
		return nil, err
	}
	onHand, note, err := counted(item.UOM, in.OnHand, in.Note)
	if err != nil {
		return nil, err
	}
	delta := new(big.Rat).Sub(onHand, l.OnHand)
	if delta.Sign() == 0 {
		return []wire.Move{}, nil
	}
	m, err := (move{Item: l.ItemID, Lot: &l.ID, Kind: "adjust", Qty: delta, Cost: l.Cost, Unknown: l.Unknown,
		Reason: note, By: by}).write(ctx, tx)
	if err != nil {
		return nil, err
	}
	return []wire.Move{m}, nil
}

// PlaceLot is phase 2 update_lot's location_id: the lot's place (nil clears it), no move.
func (Stock) PlaceLot(ctx context.Context, tx pgx.Tx, lotID string, location *string) error {
	if err := checkUUID("location_id", location); err != nil {
		return err
	}
	if _, err := lockedLot(ctx, tx, lotID); err != nil {
		return err
	}
	_, err := tx.Exec(ctx, `UPDATE stock_lots SET location_id = $1::uuid WHERE id = $2::uuid`, location, lotID)
	if isCode(err, "23503") {
		return locationNotFound(deref(location))
	}
	return err
}

// PlaceUnit is phase 2 update_unit's location_id: the unit's place (nil clears it), no
// move.
func (Stock) PlaceUnit(ctx context.Context, tx pgx.Tx, unitID string, location *string) error {
	if err := checkUUID("location_id", location); err != nil {
		return err
	}
	tag, err := tx.Exec(ctx, `UPDATE stock_units SET location_id = $1::uuid WHERE id = $2::uuid`, location, unitID)
	if isCode(err, "23503") {
		return locationNotFound(deref(location))
	}
	if err == nil && tag.RowsAffected() == 0 {
		return unitNotFound(unitID)
	}
	return err
}

// SetUnitStatus is phase 2 set_unit_status: a new status. Leaving in_stock writes a move of
// -1 (in_service a use, sold a sell, retired a retire, scrapped a scrap), coming back to it
// a return of +1, at the unit's cost; a change between two statuses out of stock moves no
// stock. Sets in_service_at on first service and retired_at on retire or scrap.
// unit_installed for a unit fitted in another (#7278).
func (Stock) SetUnitStatus(ctx context.Context, tx pgx.Tx, unitID string, in wire.UnitStatusInput, by json.RawMessage) (wire.Unit, error) {
	if !slices.Contains(unitStatuses, in.Status) {
		return wire.Unit{}, badInput("a status is one of %s, not %q", strings.Join(unitStatuses, ", "), in.Status)
	}
	if in.Entity != nil && utf8.RuneCountInString(*in.Entity) > 200 {
		return wire.Unit{}, badInput("an entity is at most 200 characters")
	}
	if err := checkText("note", in.Note, 4000); err != nil {
		return wire.Unit{}, err
	}
	var itemID, status, costText string
	var installed *string
	var unknown bool
	err := tx.QueryRow(ctx, `SELECT item_id::text, status, installed_in_unit_id::text, cost_minor::text, cost_unknown
		FROM stock_units WHERE id = $1::uuid FOR UPDATE`, unitID).Scan(&itemID, &status, &installed, &costText, &unknown)
	if errors.Is(err, pgx.ErrNoRows) {
		return wire.Unit{}, unitNotFound(unitID)
	}
	if err != nil {
		return wire.Unit{}, err
	}
	if installed != nil {
		return wire.Unit{}, unitInstalled(unitID)
	}
	if in.Status != status {
		wasIn, nowIn := status == "in_stock", in.Status == "in_stock"
		if wasIn != nowIn {
			entityID, err := entity(ctx, tx, in.Entity)
			if err != nil {
				return wire.Unit{}, err
			}
			kind, qty := leaves[in.Status], big.NewRat(-1, 1)
			if nowIn {
				kind, qty = "return", big.NewRat(1, 1)
			}
			if _, err := (move{Item: itemID, Unit: &unitID, Kind: kind, Qty: qty, Cost: numeric(costText),
				Unknown: unknown, EntityID: entityID, Reason: in.Note, By: by}).write(ctx, tx); err != nil {
				return wire.Unit{}, err
			}
		}
		if _, err := tx.Exec(ctx, `UPDATE stock_units SET status = $1,
			in_service_at = CASE WHEN $1 = 'in_service' THEN coalesce(in_service_at, now()) ELSE in_service_at END,
			retired_at = CASE WHEN $1 IN ('retired', 'scrapped') THEN now()
				WHEN $1 IN ('in_stock', 'in_service') THEN NULL ELSE retired_at END
			WHERE id = $2::uuid`, in.Status, unitID); err != nil {
			return wire.Unit{}, err
		}
	}
	u, _, err := Unit(ctx, tx, unitID)
	return u, err
}

func itemUOM(ctx context.Context, q pgx.Tx, itemID string) (string, error) {
	var uom string
	err := q.QueryRow(ctx, `SELECT k.uom FROM items i JOIN item_kinds k ON k.id = i.kind_id WHERE i.id = $1::uuid`,
		itemID).Scan(&uom)
	if errors.Is(err, pgx.ErrNoRows) {
		return "", itemNotFound(itemID)
	}
	return uom, err
}

// EstimateLots is phase 2 estimate_unit_cost: what a take of qty would charge per unit now
// for the part of it of known cost (the known lots' amounts over qty), and whether any of
// it is of unknown cost (R-P2-29). It reads only. An item with no lot is (0, true), as a
// take would open a zero-cost lot of unknown cost. bad_quantity where a take would refuse
// it (R-P2-16).
func (Stock) EstimateLots(ctx context.Context, q pgx.Tx, itemID string, qty *big.Rat) (*big.Rat, bool, error) {
	uom, err := itemUOM(ctx, q, itemID)
	if err != nil {
		return nil, false, err
	}
	if err := positive(uom, qty); err != nil {
		return nil, false, err
	}
	lots, err := collect(ctx, q, scanLotRow, `SELECT `+lotCalc+` FROM stock_lots WHERE item_id = $1::uuid
		ORDER BY received_at, id`, itemID)
	if err != nil {
		return nil, false, err
	}
	if len(lots) == 0 {
		return new(big.Rat), true, nil
	}
	plan, err := fifo(lots, qty, "")
	if err != nil {
		return nil, false, err
	}
	total, unknown := new(big.Rat), false
	for _, p := range plan {
		amt, err := amount(p.qty, p.lot.Cost)
		if err != nil {
			return nil, false, err
		}
		if p.lot.Unknown {
			unknown = true
		} else {
			total.Add(total, amt)
		}
	}
	return six(total.Quo(total, qty)), unknown, nil
}

// EstimateUnits is cost.unit's estimate: the qty oldest units in stock, each at its own
// cost (the snapshot its moves take). A unit of unknown cost, or fewer units than qty,
// makes it unknown, as a take of a lot item past its stock would.
func (Stock) EstimateUnits(ctx context.Context, q pgx.Tx, itemID string, qty *big.Rat) (*big.Rat, bool, error) {
	if _, err := itemUOM(ctx, q, itemID); err != nil {
		return nil, false, err
	}
	if err := positive("each", qty); err != nil {
		return nil, false, err
	}
	rows, err := q.Query(ctx, `SELECT cost_minor::text, cost_unknown FROM stock_units WHERE item_id = $1::uuid
		AND status = 'in_stock' ORDER BY received_at, id LIMIT $2`, itemID, qty.Num().Int64())
	if err != nil {
		return nil, false, err
	}
	defer rows.Close()
	total, unknown, n := new(big.Rat), false, 0
	for rows.Next() {
		var c string
		var u bool
		if err := rows.Scan(&c, &u); err != nil {
			return nil, false, err
		}
		n++
		if u {
			unknown = true
		} else {
			total.Add(total, numeric(c))
		}
	}
	if err := rows.Err(); err != nil {
		return nil, false, err
	}
	if big.NewRat(int64(n), 1).Cmp(qty) < 0 {
		unknown = true
	}
	return six(total.Quo(total, qty)), unknown, nil
}

// -- what is not a step ----------------------------------------------------------------

// GiveBack is phase 2 give_back: one return per use move of the order item and item not
// yet reversed, the same lot, the same quantity, the use's own cost (D14). A second call
// writes nothing.
func GiveBack(ctx context.Context, tx pgx.Tx, itemID, orderItemID string, by Actor) ([]wire.Move, error) {
	item, err := lotItem(ctx, tx, itemID, true)
	if err != nil {
		return nil, err
	}
	// The lots first, as a take locks them: a second give-back waits here, then reads the
	// returns the first wrote.
	if _, err := lockLots(ctx, tx, item.ID); err != nil {
		return nil, err
	}
	type use struct {
		id, lot, qty, cost string
		unknown            bool
		orderID, entityID  *string
	}
	uses, err := collect(ctx, tx, func(row pgx.Row) (use, error) {
		var u use
		err := row.Scan(&u.id, &u.lot, &u.qty, &u.cost, &u.unknown, &u.orderID, &u.entityID)
		return u, err
	}, `SELECT m.id::text, m.lot_id::text, m.qty::text, m.unit_cost_minor::text, m.cost_unknown, m.order_id,
		m.entity_id::text FROM stock_moves m WHERE m.order_item_id = $1 AND m.item_id = $2::uuid
		AND m.kind = 'use' AND m.lot_id IS NOT NULL AND NOT EXISTS
		(SELECT 1 FROM stock_moves r WHERE r.reverses_move_id = m.id) ORDER BY m.at, m.id`, orderItemID, item.ID)
	if err != nil {
		return nil, err
	}
	out := []wire.Move{}
	for _, u := range uses {
		m, err := (move{Item: item.ID, Lot: &u.lot, Kind: "return", Qty: new(big.Rat).Neg(numeric(u.qty)),
			Cost: numeric(u.cost), Unknown: u.unknown, Reverses: &u.id, OrderID: u.orderID,
			OrderItemID: &orderItemID, EntityID: u.entityID, By: by}).write(ctx, tx)
		if err != nil {
			return nil, err
		}
		out = append(out, m)
	}
	return out, nil
}

// setCost is phase 2 _set_cost: the locked lot's cost now, known. bad_quantity for a cost
// below 0, with more than six places, or at which the lot's stock (received or on hand) is
// worth more than an amount holds.
func setCost(ctx context.Context, tx pgx.Tx, l lotRow, c wire.Decimal) error {
	unitCost, err := cost(c)
	if err != nil {
		return err
	}
	most := abs(l.OnHand)
	if l.ReceivedQty.Cmp(most) > 0 {
		most = l.ReceivedQty
	}
	if _, err := amount(most, unitCost); err != nil {
		return err
	}
	_, err = tx.Exec(ctx, `UPDATE stock_lots SET unit_cost_minor = $1::numeric, cost_unknown = false WHERE id = $2::uuid`,
		arg(unitCost), l.ID)
	return err
}

// SetLotCost is phase 2 set_lot_cost: the lot's cost now, known. No move changes (D14);
// Recost re-snapshots. cost_currency and foreign_unit_cost_minor stay: what it was bought
// in and at (R-P2-12).
func SetLotCost(ctx context.Context, tx pgx.Tx, lotID string, c wire.Decimal) (wire.Lot, error) {
	l, _, err := lockLot(ctx, tx, lotID)
	if err != nil {
		return wire.Lot{}, err
	}
	if err := setCost(ctx, tx, l, c); err != nil {
		return wire.Lot{}, err
	}
	out, _, err := Lot(ctx, tx, l.ID)
	return out, err
}

// UpdateLot is phase 2 update_lot's note (C30): the lot locked as update_lot locks it, and
// its note when note is not nil. lot_not_found.
func UpdateLot(ctx context.Context, tx pgx.Tx, lotID string, note *string) error {
	l, _, err := lockLot(ctx, tx, lotID)
	if err != nil || note == nil {
		return err
	}
	if err := checkText("note", *note, 4000); err != nil {
		return err
	}
	_, err = tx.Exec(ctx, `UPDATE stock_lots SET note = $1 WHERE id = $2::uuid`, *note, l.ID)
	return err
}

// UpdateUnit is phase 2 update_unit's name and serial (C30): the unit locked as
// update_unit locks it; the name when it is not nil; the serial when it is set (null or
// blank clears it). unit_not_found or serial_exists.
func UpdateUnit(ctx context.Context, tx pgx.Tx, unitID string, name *string, serial wire.Opt[string]) error {
	if err := lockUnit(ctx, tx, unitID); err != nil {
		return err
	}
	if name != nil {
		if err := checkText("name", *name, 200); err != nil {
			return err
		}
		if _, err := tx.Exec(ctx, `UPDATE stock_units SET name = $1 WHERE id = $2::uuid`, *name, unitID); err != nil {
			return err
		}
	}
	if serial.Set {
		var value *string
		if serial.Value != nil {
			if err := checkText("serial", *serial.Value, 200); err != nil {
				return err
			}
			if s := strings.TrimSpace(*serial.Value); s != "" {
				value = &s
			}
		}
		_, err := tx.Exec(ctx, `UPDATE stock_units SET serial = $1 WHERE id = $2::uuid`, value, unitID)
		if isCode(err, "23505") {
			return serialExists(deref(value))
		}
		return err
	}
	return nil
}

// partsMove is an order item's use or return move as job cost reads it (phase 2
// parts_moves): open is a use that no return names.
type partsMove struct {
	id, orderItemID string
	orderID         *string
	amount          *big.Rat
	unknown, open   bool
}

// partsMoved is phase 2 PartsMoved: an order item's parts as its moves say.
type partsMoved struct {
	cost          *big.Rat
	unknown, open bool
}

func readPartsMoves(ctx context.Context, tx pgx.Tx, orderItemIDs []string) ([]partsMove, error) {
	return collect(ctx, tx, func(row pgx.Row) (partsMove, error) {
		var m partsMove
		var amt string
		err := row.Scan(&m.id, &m.orderID, &m.orderItemID, &amt, &m.unknown, &m.open)
		m.amount = numeric(amt)
		return m, err
	}, `SELECT m.id::text, m.order_id, m.order_item_id, m.amount_minor::text, m.cost_unknown,
		m.kind = 'use' AND NOT EXISTS (SELECT 1 FROM stock_moves r WHERE r.reverses_move_id = m.id)
		FROM stock_moves m WHERE m.order_item_id = ANY($1) AND m.kind IN ('use', 'return')`, orderItemIDs)
}

// sumParts is phase 2 parts_moved: each order item's -sum(amount_minor) of its known-cost
// moves, whether a use not given back is of unknown cost, and whether one is not given
// back.
func sumParts(moves []partsMove) map[string]partsMoved {
	out := map[string]partsMoved{}
	for _, m := range moves {
		was, ok := out[m.orderItemID]
		if !ok {
			was = partsMoved{cost: new(big.Rat)}
		}
		c := new(big.Rat).Set(was.cost)
		if !m.unknown {
			c.Sub(c, m.amount)
		}
		out[m.orderItemID] = partsMoved{cost: c, unknown: was.unknown || (m.open && m.unknown), open: was.open || m.open}
	}
	return out
}

// Recost is phase 2 recost_lot ("Re-cost past uses", spec §6.6): every use, return and
// adjust move of the lot takes the lot's cost now. It names each order item whose parts
// cost or unknown flag changed, as job cost reads them, with both before and after
// (R-P2-31). orders are the orders the caller locked before the lot: a move of the lot that
// names another order is 409 recost_orders_moved, and nothing is written (phase 2's
// _OrdersMoved, checked here under the lot's lock).
func Recost(ctx context.Context, tx pgx.Tx, lotID string, orders []string) (wire.RecostResult, error) {
	l, _, err := lockLot(ctx, tx, lotID)
	if err != nil {
		return wire.RecostResult{}, err
	}
	named, err := collect(ctx, tx, func(row pgx.Row) (string, error) {
		var s string
		return s, row.Scan(&s)
	}, `SELECT DISTINCT order_id FROM stock_moves WHERE lot_id = $1::uuid AND order_id IS NOT NULL ORDER BY order_id`, l.ID)
	if err != nil {
		return wire.RecostResult{}, err
	}
	var other []string
	for _, o := range named {
		if !slices.Contains(orders, o) {
			other = append(other, o)
		}
	}
	if len(other) > 0 {
		return wire.RecostResult{}, &Refused{Status: 409, Code: "recost_orders_moved",
			Detail: "a move of the lot names an order the re-cost did not lock: " + strings.Join(other, ", "),
			Fields: map[string]any{"orders": other}}
	}
	var largest *string
	if err := tx.QueryRow(ctx, `SELECT max(abs(qty))::text FROM stock_moves WHERE lot_id = $1::uuid AND kind = ANY($2)`,
		l.ID, recostKinds).Scan(&largest); err != nil {
		return wire.RecostResult{}, err
	}
	if largest != nil {
		if _, err := amount(numeric(*largest), l.Cost); err != nil {
			return wire.RecostResult{}, err
		}
	}
	touched, err := collect(ctx, tx, func(row pgx.Row) (string, error) {
		var s string
		return s, row.Scan(&s)
	}, `SELECT DISTINCT order_item_id FROM stock_moves WHERE lot_id = $1::uuid AND kind = ANY($2)
		AND order_item_id IS NOT NULL`, l.ID, recostKinds)
	if err != nil {
		return wire.RecostResult{}, err
	}
	moves, err := readPartsMoves(ctx, tx, touched)
	if err != nil {
		return wire.RecostResult{}, err
	}
	type changed struct {
		id, oldCost, amount string
		unknown             bool
	}
	rows, err := collect(ctx, tx, func(row pgx.Row) (changed, error) {
		var c changed
		return c, row.Scan(&c.id, &c.oldCost, &c.amount, &c.unknown)
	}, `UPDATE stock_moves SET unit_cost_minor = $1::numeric, amount_minor = round(qty * $1::numeric, 6),
		cost_unknown = $2 WHERE lot_id = $3::uuid AND kind = ANY($4)
		AND (unit_cost_minor <> $1::numeric OR cost_unknown <> $2)
		RETURNING id::text, old.unit_cost_minor::text, new.amount_minor::text, new.cost_unknown`,
		arg(l.Cost), l.Unknown, l.ID, recostKinds)
	if err != nil {
		return wire.RecostResult{}, err
	}
	byID := map[string]changed{}
	for _, r := range rows {
		byID[r.id] = r
	}
	after := make([]partsMove, len(moves))
	for i, m := range moves {
		after[i] = m
		if c, ok := byID[m.id]; ok {
			after[i].amount, after[i].unknown = numeric(c.amount), c.unknown
		}
	}
	before, now := sumParts(moves), sumParts(after)
	orderOf := map[string]*string{}
	for _, m := range moves {
		if orderOf[m.orderItemID] == nil {
			orderOf[m.orderItemID] = m.orderID
		}
	}
	items := make([]string, 0, len(before))
	for oi := range before {
		items = append(items, oi)
	}
	sort.Slice(items, func(i, j int) bool {
		a, b := deref(orderOf[items[i]]), deref(orderOf[items[j]])
		if a != b {
			return a < b
		}
		return items[i] < items[j]
	})
	result := wire.RecostResult{ItemID: l.ItemID, UnitCostMinor: text(l.Cost), CostUnknown: l.Unknown,
		BeforeUnitCostsMinor: []string{}, Moves: len(rows), Orders: []wire.RecostedItem{}}
	for _, oi := range items {
		b, a := before[oi], now[oi]
		if b.cost.Cmp(a.cost) == 0 && b.unknown == a.unknown {
			continue
		}
		result.Orders = append(result.Orders, wire.RecostedItem{OrderID: orderOf[oi], OrderItemID: oi,
			BeforeMinor: text(b.cost), AfterMinor: text(a.cost), BeforeUnknown: b.unknown, AfterUnknown: a.unknown})
	}
	var costs []*big.Rat
	for _, r := range rows {
		c := numeric(r.oldCost)
		if !slices.ContainsFunc(costs, func(x *big.Rat) bool { return x.Cmp(c) == 0 }) {
			costs = append(costs, c)
		}
	}
	slices.SortFunc(costs, func(a, b *big.Rat) int { return a.Cmp(b) })
	for _, c := range costs {
		result.BeforeUnitCostsMinor = append(result.BeforeUnitCostsMinor, text(c))
	}
	return result, nil
}

// LockItems is phase 2 lock_items: every item's locks a take of it takes, in the caller's
// transaction, item by item in item id order: the item FOR SHARE, the lock that every lot
// insert takes, then its lots FOR UPDATE in FIFO order. A caller that then takes several
// items takes no lock out of this order (R-P2-28), and no receive or count adds a lot to an
// item it holds until it ends (R-P2-32).
func LockItems(ctx context.Context, tx pgx.Tx, itemIDs []string) error {
	ids := make([]string, 0, len(itemIDs))
	for _, id := range itemIDs {
		if id = strings.ToLower(id); !slices.Contains(ids, id) {
			ids = append(ids, id)
		}
	}
	slices.Sort(ids)
	for _, id := range ids {
		item, err := lotItem(ctx, tx, id, true)
		if err != nil {
			return err
		}
		if err := openLock(ctx, tx, item.ID); err != nil {
			return err
		}
		if _, err := lockLots(ctx, tx, item.ID); err != nil {
			return err
		}
	}
	return nil
}

// Moved is what a Repoint moved: the ids of the moves and the units.
type Moved struct {
	Moves []string `json:"moves"`
	Units []string `json:"units"`
}

func uuids(ids []string) []string {
	out := []string{}
	for _, id := range ids {
		if uuidText.MatchString(id) {
			out = append(out, id)
		}
	}
	return out
}

func returningIDs(ctx context.Context, tx pgx.Tx, sql string, args ...any) ([]string, error) {
	ids, err := collect(ctx, tx, func(row pgx.Row) (string, error) {
		var s string
		return s, row.Scan(&s)
	}, sql, args...)
	slices.Sort(ids)
	return ids, err
}

// Repoint moves the stock rows that name the entity kind:gone to kind:kept, for a merge
// that deletes gone's row and keeps kept's (the printer merge, R-K1-15): stock_moves.
// entity_id, and the stock_units column that names an owner of the kind (a printer's
// printer_id). It answers the ids it moved; a repeat finds nothing to move. A non-nil
// only limits it to those ids, and of them to the rows that still name gone: an id whose
// row was deleted, or names another entity now, is skipped, not refused, and is left out of
// the answer. So a back Repoint of a refused merge moves exactly what the forward one
// moved, and never kept's own stock.
func Repoint(ctx context.Context, tx pgx.Tx, kind, gone, kept string, only *Moved) (Moved, error) {
	out := Moved{Moves: []string{}, Units: []string{}}
	k, ok := entityKinds[kind]
	if !ok {
		return out, entityRefused(422, "unknown_entity_kind", fmt.Sprintf("no entity kind %q", kind))
	}
	var moveIDs, unitIDs []string
	if only != nil {
		moveIDs, unitIDs = uuids(only.Moves), uuids(only.Units)
	}
	var goneID string
	err := tx.QueryRow(ctx, `SELECT id::text FROM entities WHERE kind = $1 AND ref = $2`, kind, gone).Scan(&goneID)
	if err != nil && !errors.Is(err, pgx.ErrNoRows) {
		return out, err
	}
	if goneID != "" && (only == nil || len(moveIDs) > 0) {
		var named bool
		if err := tx.QueryRow(ctx, `SELECT EXISTS (SELECT 1 FROM stock_moves WHERE entity_id = $1::uuid
			AND ($2::uuid[] IS NULL OR id = ANY($2::uuid[])))`, goneID, nullable(only, moveIDs)).Scan(&named); err != nil {
			return out, err
		}
		if named {
			keptID, err := EnsureEntity(ctx, tx, kind, kept)
			if err != nil {
				return out, err
			}
			if out.Moves, err = returningIDs(ctx, tx, `UPDATE stock_moves SET entity_id = $1::uuid
				WHERE entity_id = $2::uuid AND ($3::uuid[] IS NULL OR id = ANY($3::uuid[])) RETURNING id::text`,
				keptID, goneID, nullable(only, moveIDs)); err != nil {
				return out, err
			}
		}
	}
	if k.stockColumn != "" && uuidText.MatchString(gone) && uuidText.MatchString(kept) && (only == nil || len(unitIDs) > 0) {
		if out.Units, err = returningIDs(ctx, tx, `UPDATE stock_units SET `+k.stockColumn+` = $1::uuid
			WHERE `+k.stockColumn+` = $2::uuid AND ($3::uuid[] IS NULL OR id = ANY($3::uuid[])) RETURNING id::text`,
			kept, gone, nullable(only, unitIDs)); err != nil {
			return out, err
		}
	}
	return out, nil
}

// nullable is ids as a uuid[] parameter, or null (every row) when there is no only.
func nullable(only *Moved, ids []string) []string {
	if only == nil {
		return nil
	}
	return ids
}
