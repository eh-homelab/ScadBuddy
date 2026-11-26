// Package activities is the inventory's writes as Temporal activities on the task queue
// inventory (#6949 K1, design §6.1a): one function per write, registered by name
// (inventory.<Name>), which a standalone start (Python's start_activity), a workflow step
// and the tests all run. Each runs one store.Write under the caller inventory with its
// idempotency key, so an activity run twice writes once. A refusal is an answer
// ({status, body}), never a failure; only an infrastructure error fails the activity, and
// Temporal retries it (Global Constraints). An activity holds only parsing and problem
// mapping: every SQL statement is in the store.
package activities

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"strings"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"go.temporal.io/sdk/temporal"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/inventory/flow"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/inventory/handlers"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/inventory/store"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/inventory/wire"
)

// Input is every activity's input, as Python's InventoryCore.write sends it: the
// idempotency key (the activity id too, but the code reads only Key), who the write is by
// (an Actor object, R-K1-5), the public route's path parameters, and its public body.
type Input struct {
	Key    string            `json:"key"`
	Actor  json.RawMessage   `json:"actor"`
	Params map[string]string `json:"params"`
	Body   json.RawMessage   `json:"body"`
}

// Activities are the inventory's activities over the base flow and the kind and item
// stores. Shop is the shop's currency when the setting orders_currency has no row
// (SCADBUDDY_ORDERS_CURRENCY, else USD).
type Activities struct {
	Flow  *flow.Flow
	Kinds *store.Kinds
	Items *store.Items
	Shop  string
}

// New is the activities over pool with K1's handlers.
func New(pool *pgxpool.Pool, shop string) *Activities {
	f := flow.New(pool)
	return &Activities{Flow: f, Kinds: &store.Kinds{Cat: f.Reg.Catalog}, Items: &store.Items{Cat: f.Reg.Catalog}, Shop: shop}
}

// write runs do in one store.Write of the caller inventory: name is the route, the key the
// input's, and the ledger's input the params and the body (the actor is in the key). A
// bad actor is the caller's fault: the activity fails, not retried, and writes nothing.
func (a *Activities) write(ctx context.Context, name string, in Input, do func(tx pgx.Tx, by store.Actor) (int, any, error)) (store.Answer, error) {
	by, err := store.ReadActor(in.Actor)
	if err != nil {
		return store.Answer{}, temporal.NewNonRetryableApplicationError(err.Error(), "bad_actor", err)
	}
	digest, err := json.Marshal(struct {
		Params map[string]string `json:"params"`
		Body   json.RawMessage   `json:"body"`
	}{in.Params, body(in.Body)})
	if err != nil {
		return store.Answer{}, err
	}
	return store.Write(ctx, a.Flow.Pool, store.WriteReq{Caller: store.Caller, Key: in.Key, Route: name, Input: digest, Actor: by},
		func(tx pgx.Tx) (int, any, error) {
			if hasNUL(in) {
				// Postgres takes no NUL in text: it would fail the write, and Temporal would
				// retry it for ever. pydantic's Text refuses it before the start.
				return 0, nil, &store.Refused{Status: http.StatusUnprocessableEntity, Code: "bad_input",
					Detail: "a text of the input holds a NUL character"}
			}
			return do(tx, by)
		})
}

// hasNUL says whether a path parameter or a string of the body (a key too) holds a NUL.
func hasNUL(in Input) bool {
	for k, v := range in.Params {
		if strings.ContainsRune(k+v, 0) {
			return true
		}
	}
	var v any
	if json.Unmarshal(in.Body, &v) != nil {
		return false
	}
	var walk func(any) bool
	walk = func(v any) bool {
		switch x := v.(type) {
		case string:
			return strings.ContainsRune(x, 0)
		case []any:
			for _, e := range x {
				if walk(e) {
					return true
				}
			}
		case map[string]any:
			for k, e := range x {
				if strings.ContainsRune(k, 0) || walk(e) {
					return true
				}
			}
		}
		return false
	}
	return walk(v)
}

// body is raw, or null when it is empty.
func body(raw json.RawMessage) json.RawMessage {
	if len(raw) == 0 {
		return json.RawMessage("null")
	}
	return raw
}

// decode reads the public body into v: anything else is 422 bad_input (what pydantic would
// have refused before the start).
func decode(raw json.RawMessage, v any) error {
	if err := json.Unmarshal(body(raw), v); err != nil {
		return &store.Refused{Status: http.StatusUnprocessableEntity, Code: "bad_input",
			Detail: "the activity's body does not read as its input: " + err.Error()}
	}
	return nil
}

// answer is a write's result: status with v, or the refusal err.
func answer[T any](status int, v T, err error) (int, any, error) {
	if err != nil {
		return 0, nil, err
	}
	return status, v, nil
}

// Lists as the public routes answer them.
type (
	unitList struct {
		Units []wire.Unit `json:"units"`
	}
	moveList struct {
		Moves []wire.Move `json:"moves"`
	}
	moved struct {
		Moved store.Moved `json:"moved"`
	}
)

func moves(ms []wire.Move, err error) (moveList, error) {
	if ms == nil {
		ms = []wire.Move{}
	}
	return moveList{ms}, err
}

// -- kinds -------------------------------------------------------------------------------

// KindCreate is POST /inventory/kinds: 201 ItemKind.
func (a *Activities) KindCreate(ctx context.Context, in Input) (store.Answer, error) {
	return a.write(ctx, "inventory.KindCreate", in, func(tx pgx.Tx, by store.Actor) (int, any, error) {
		var body wire.KindInput
		if err := decode(in.Body, &body); err != nil {
			return 0, nil, err
		}
		k, err := a.Kinds.Create(ctx, tx, body, by)
		return answer(http.StatusCreated, k, err)
	})
}

// KindUpdate is PATCH /inventory/kinds/{kind_id}: ItemKind.
func (a *Activities) KindUpdate(ctx context.Context, in Input) (store.Answer, error) {
	return a.write(ctx, "inventory.KindUpdate", in, func(tx pgx.Tx, by store.Actor) (int, any, error) {
		var patch wire.KindPatch
		if err := decode(in.Body, &patch); err != nil {
			return 0, nil, err
		}
		k, err := a.Kinds.Update(ctx, tx, in.Params["kind_id"], patch, by)
		return answer(http.StatusOK, k, err)
	})
}

// KindArchive is POST /inventory/kinds/{kind_id}/archive: ItemKind.
func (a *Activities) KindArchive(ctx context.Context, in Input) (store.Answer, error) {
	return a.write(ctx, "inventory.KindArchive", in, func(tx pgx.Tx, _ store.Actor) (int, any, error) {
		k, err := a.Kinds.Archive(ctx, tx, in.Params["kind_id"])
		return answer(http.StatusOK, k, err)
	})
}

// KindRestore is POST /inventory/kinds/{kind_id}/restore: ItemKind.
func (a *Activities) KindRestore(ctx context.Context, in Input) (store.Answer, error) {
	return a.write(ctx, "inventory.KindRestore", in, func(tx pgx.Tx, _ store.Actor) (int, any, error) {
		k, err := a.Kinds.Restore(ctx, tx, in.Params["kind_id"])
		return answer(http.StatusOK, k, err)
	})
}

// -- items -------------------------------------------------------------------------------

// ItemCreate is POST /inventory/items: 201 Item.
func (a *Activities) ItemCreate(ctx context.Context, in Input) (store.Answer, error) {
	return a.write(ctx, "inventory.ItemCreate", in, func(tx pgx.Tx, by store.Actor) (int, any, error) {
		var body wire.ItemInput
		if err := decode(in.Body, &body); err != nil {
			return 0, nil, err
		}
		it, err := a.Items.Create(ctx, tx, body, by)
		return answer(http.StatusCreated, it, err)
	})
}

// ItemUpdate is PATCH /inventory/items/{item_id}: Item.
func (a *Activities) ItemUpdate(ctx context.Context, in Input) (store.Answer, error) {
	return a.write(ctx, "inventory.ItemUpdate", in, func(tx pgx.Tx, by store.Actor) (int, any, error) {
		var patch wire.InventoryItemPatch
		if err := decode(in.Body, &patch); err != nil {
			return 0, nil, err
		}
		it, err := a.Items.Update(ctx, tx, in.Params["item_id"], patch, by)
		return answer(http.StatusOK, it, err)
	})
}

// ItemArchive is POST /inventory/items/{item_id}/archive: Item.
func (a *Activities) ItemArchive(ctx context.Context, in Input) (store.Answer, error) {
	return a.write(ctx, "inventory.ItemArchive", in, func(tx pgx.Tx, _ store.Actor) (int, any, error) {
		it, err := a.Items.Archive(ctx, tx, in.Params["item_id"])
		return answer(http.StatusOK, it, err)
	})
}

// ItemRestore is POST /inventory/items/{item_id}/restore: Item.
func (a *Activities) ItemRestore(ctx context.Context, in Input) (store.Answer, error) {
	return a.write(ctx, "inventory.ItemRestore", in, func(tx pgx.Tx, _ store.Actor) (int, any, error) {
		it, err := a.Items.Restore(ctx, tx, in.Params["item_id"])
		return answer(http.StatusOK, it, err)
	})
}

// -- stock -------------------------------------------------------------------------------

// ReceiveLot is POST /inventory/items/{item_id}/lots: 201 Lot, its cost in the shop's
// currency unless the body names another.
func (a *Activities) ReceiveLot(ctx context.Context, in Input) (store.Answer, error) {
	return a.write(ctx, "inventory.ReceiveLot", in, func(tx pgx.Tx, by store.Actor) (int, any, error) {
		var lot wire.ReceiveLot
		if err := decode(in.Body, &lot); err != nil {
			return 0, nil, err
		}
		shop, err := store.ShopCurrency(ctx, tx, a.Shop)
		if err != nil {
			return 0, nil, err
		}
		out, err := a.Flow.Receive(ctx, tx, in.Params["item_id"], handlers.ReceiveIn{Lot: &lot, By: by, Shop: shop})
		if err != nil {
			return 0, nil, err
		}
		if out.Lot == nil {
			return 0, nil, errors.New("activities: the receive made no lot")
		}
		return http.StatusCreated, *out.Lot, nil
	})
}

// ReceiveUnits is POST /inventory/items/{item_id}/units: 201 UnitList.
func (a *Activities) ReceiveUnits(ctx context.Context, in Input) (store.Answer, error) {
	return a.write(ctx, "inventory.ReceiveUnits", in, func(tx pgx.Tx, by store.Actor) (int, any, error) {
		var units wire.ReceiveUnits
		if err := decode(in.Body, &units); err != nil {
			return 0, nil, err
		}
		shop, err := store.ShopCurrency(ctx, tx, a.Shop)
		if err != nil {
			return 0, nil, err
		}
		out, err := a.Flow.Receive(ctx, tx, in.Params["item_id"], handlers.ReceiveIn{Units: &units, By: by, Shop: shop})
		if out.Units == nil {
			out.Units = []wire.Unit{}
		}
		return answer(http.StatusCreated, unitList{out.Units}, err)
	})
}

// LotUpdate is PATCH /inventory/lots/{lot_id} (cost, place, note): Lot.
func (a *Activities) LotUpdate(ctx context.Context, in Input) (store.Answer, error) {
	return a.write(ctx, "inventory.LotUpdate", in, func(tx pgx.Tx, by store.Actor) (int, any, error) {
		var patch wire.LotPatch
		if err := decode(in.Body, &patch); err != nil {
			return 0, nil, err
		}
		l, err := a.Flow.LotUpdate(ctx, tx, in.Params["lot_id"], patch, by)
		return answer(http.StatusOK, l, err)
	})
}

// LotAdjust is POST /inventory/lots/{lot_id}/adjust (a count of one lot): Lot.
func (a *Activities) LotAdjust(ctx context.Context, in Input) (store.Answer, error) {
	return a.write(ctx, "inventory.LotAdjust", in, func(tx pgx.Tx, by store.Actor) (int, any, error) {
		var adjust wire.AdjustInput
		if err := decode(in.Body, &adjust); err != nil {
			return 0, nil, err
		}
		lotID := in.Params["lot_id"]
		if _, err := a.Flow.Count(ctx, tx, store.Target{Lot: lotID}, handlers.CountIn{Adjust: &adjust, By: by}); err != nil {
			return 0, nil, err
		}
		l, _, err := store.Lot(ctx, tx, lotID)
		return answer(http.StatusOK, l, err)
	})
}

// Take is POST /inventory/items/{item_id}/use, and an order's take (the body names the
// order and its item): MoveList.
func (a *Activities) Take(ctx context.Context, in Input) (store.Answer, error) {
	return a.write(ctx, "inventory.Take", in, func(tx pgx.Tx, by store.Actor) (int, any, error) {
		var use wire.UseInput
		if err := decode(in.Body, &use); err != nil {
			return 0, nil, err
		}
		list, err := moves(a.Flow.Consume(ctx, tx, in.Params["item_id"], handlers.ConsumeIn{Use: use, By: by}))
		return answer(http.StatusOK, list, err)
	})
}

// Count is POST /inventory/items/{item_id}/count (a count of the whole item): MoveList.
func (a *Activities) Count(ctx context.Context, in Input) (store.Answer, error) {
	return a.write(ctx, "inventory.Count", in, func(tx pgx.Tx, by store.Actor) (int, any, error) {
		var count wire.CountInput
		if err := decode(in.Body, &count); err != nil {
			return 0, nil, err
		}
		list, err := moves(a.Flow.Count(ctx, tx, store.Target{Item: in.Params["item_id"]}, handlers.CountIn{Count: &count, By: by}))
		return answer(http.StatusOK, list, err)
	})
}

// UnitUpdate is PATCH /inventory/units/{unit_id} (name, serial, place): Unit.
func (a *Activities) UnitUpdate(ctx context.Context, in Input) (store.Answer, error) {
	return a.write(ctx, "inventory.UnitUpdate", in, func(tx pgx.Tx, by store.Actor) (int, any, error) {
		var patch wire.UnitPatch
		if err := decode(in.Body, &patch); err != nil {
			return 0, nil, err
		}
		u, err := a.Flow.UnitUpdate(ctx, tx, in.Params["unit_id"], patch, by)
		return answer(http.StatusOK, u, err)
	})
}

// UnitStatus is POST /inventory/units/{unit_id}/status: Unit.
func (a *Activities) UnitStatus(ctx context.Context, in Input) (store.Answer, error) {
	return a.write(ctx, "inventory.UnitStatus", in, func(tx pgx.Tx, by store.Actor) (int, any, error) {
		var status wire.UnitStatusInput
		if err := decode(in.Body, &status); err != nil {
			return 0, nil, err
		}
		u, err := a.Flow.Retire(ctx, tx, in.Params["unit_id"], handlers.RetireIn{Status: status, By: by})
		return answer(http.StatusOK, u, err)
	})
}

// GiveBack is an unfulfil's return of the item's parts to the order item (body
// {order_item_id}): MoveList, one return per use not yet reversed.
func (a *Activities) GiveBack(ctx context.Context, in Input) (store.Answer, error) {
	return a.write(ctx, "inventory.GiveBack", in, func(tx pgx.Tx, by store.Actor) (int, any, error) {
		var body struct {
			OrderItemID string `json:"order_item_id"`
		}
		if err := decode(in.Body, &body); err != nil {
			return 0, nil, err
		}
		list, err := moves(a.Flow.GiveBack(ctx, tx, in.Params["item_id"], body.OrderItemID, by))
		return answer(http.StatusOK, list, err)
	})
}

// Recost is a re-cost of the lot's past uses (body {orders}, the orders the caller
// locked): RecostResult, or 409 recost_orders_moved.
func (a *Activities) Recost(ctx context.Context, in Input) (store.Answer, error) {
	return a.write(ctx, "inventory.Recost", in, func(tx pgx.Tx, _ store.Actor) (int, any, error) {
		var body struct {
			Orders []string `json:"orders"`
		}
		if err := decode(in.Body, &body); err != nil {
			return 0, nil, err
		}
		r, err := a.Flow.Recost(ctx, tx, in.Params["lot_id"], body.Orders)
		return answer(http.StatusOK, r, err)
	})
}

// Repoint moves the stock rows that name the entity kind:gone to kind:kept (body {kind,
// gone, kept, only?}), for a printer merge: {moved: {moves, units}}.
func (a *Activities) Repoint(ctx context.Context, in Input) (store.Answer, error) {
	return a.write(ctx, "inventory.Repoint", in, func(tx pgx.Tx, _ store.Actor) (int, any, error) {
		var body struct {
			Kind string       `json:"kind"`
			Gone string       `json:"gone"`
			Kept string       `json:"kept"`
			Only *store.Moved `json:"only"`
		}
		if err := decode(in.Body, &body); err != nil {
			return 0, nil, err
		}
		if body.Kind == "" || body.Gone == "" || body.Kept == "" {
			return 0, nil, &store.Refused{Status: http.StatusUnprocessableEntity, Code: "bad_input",
				Detail: "a repoint names its kind, the entity gone and the one kept"}
		}
		m, err := a.Flow.Repoint(ctx, tx, body.Kind, body.Gone, body.Kept, body.Only)
		return answer(http.StatusOK, moved{m}, err)
	})
}
