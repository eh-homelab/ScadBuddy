// Package assistantcost is POST …/agent/orders/{order_id}/assistant-cost of
// backend/scadbuddy/api/crud_agent.py (area order-costs): the assistant's spend in one session
// on an order, in USD as the agent's ai_sessions.cost_usd knows it, written as one assistant
// cost entry (orders/lifecycle.py Lifecycle.add_assistant_cost, statement for statement).
//
// The spend is converted at pricing_usd_rate under the order's lock. Its source is
// ai_session:<id>, so a second report of the session writes nothing (201 when this call wrote
// the entry, 200 when it did not). A spend under one minor unit writes nothing and answers no
// entry, so the agent keeps it to report with more spend. After the commit the order and the
// item the entry names are told on the bus (order.changed, order_item.changed).
//
// The decimals are Python's: cost_usd as pydantic reads it, the product in the default
// 28-digit context, rounded half up to minor units, and the note's figure as str(Decimal)
// writes it, pinned by backend/tests/fixtures/assistant-cost-vectors.json.
package assistantcost

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"regexp"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/bus"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/seal"
)

const (
	principalMax = 300
	itemIDMax    = 100
	bodyMax      = 8 * 1024 * 1024
	problems     = "https://scadbuddy.dev/problems/orders/"
)

var (
	currencyCode = regexp.MustCompile(`^[A-Z]{3}$`)
	// noMinorUnit is orders/currency.py NO_MINOR_UNIT: orders_currency refuses them.
	noMinorUnit = map[string]bool{
		"XAG": true, "XAU": true, "XBA": true, "XBB": true, "XBC": true, "XBD": true, "XDR": true,
		"XPD": true, "XPT": true, "XSU": true, "XTS": true, "XUA": true, "XXX": true,
	}
	// exponents is orders/currency.py CURRENCY_EXPONENTS: every other code has 2.
	exponents = func() map[string]int {
		out := map[string]int{}
		for _, c := range strings.Fields("BIF CLP DJF GNF ISK JPY KMF KRW PYG RWF UGX UYI VND VUV XAF XOF XPF") {
			out[c] = 0
		}
		for _, c := range strings.Fields("BHD IQD JOD KWD LYD OMR TND") {
			out[c] = 3
		}
		out["CLF"], out["UYW"] = 4, 4
		for c := range noMinorUnit {
			out[c] = 0
		}
		return out
	}()
	maxRate = Decimal{coef: pow10(9)}
)

// MinorExponent is currency.py minor_exponent.
func MinorExponent(code string) int {
	if e, ok := exponents[strings.ToUpper(code)]; ok {
		return e
	}
	return 2
}

// Areas are the package's routes.
func Areas(pool *pgxpool.Pool, _ *seal.Keys) map[string][]crud.Route {
	c := &Costs{Pool: pool}
	return map[string][]crud.Route{
		"order-costs": {{Method: "POST", Path: "/orders/{order_id}/assistant-cost", Handle: c.report,
			MaxBody: bodyMax, Status: http.StatusCreated}},
	}
}

// Costs is the area's route.
type Costs struct{ Pool *pgxpool.Pool }

type actor struct {
	Kind      string  `json:"kind"`
	Principal *string `json:"principal"`
	Session   *string `json:"session"`
	RunID     *string `json:"run_id"`
}

// entry is orders/models.py CostEntry, its members in pydantic's order.
type entry struct {
	OrderItemID *string   `json:"order_item_id"`
	Kind        string    `json:"kind"`
	AmountMinor int64     `json:"amount_minor"`
	Note        string    `json:"note"`
	Source      *string   `json:"source"`
	ID          string    `json:"id"`
	OrderID     string    `json:"order_id"`
	CreatedBy   actor     `json:"created_by"`
	CreatedAt   crud.Time `json:"created_at"`
}

type answer struct {
	Entry   *entry `json:"entry"`
	Written bool   `json:"written"`
}

func refused(status int, slug, detail string) error {
	return &httpx.Problem{Status: status, Detail: detail, Type: problems + slug}
}

// UsdRate is lifecycle.py UsdRate: one USD in the shop's currency.
type UsdRate struct {
	PerUSD   Decimal
	Currency string
}

// Factor is one USD in currency: 1 for USD, the rate for the shop's currency, else false.
func (u UsdRate) Factor(currency string) (Decimal, bool) {
	if currency == "USD" {
		return Decimal{coef: pow10(0)}, true
	}
	return u.PerUSD, currency == u.Currency
}

// rate is the snapshot's pricing_usd_rate and orders_currency: a stored row this version
// takes, else the deployment's SCADBUDDY_<FIELD>, else the default (library/settings_store.py
// _resolve).
func (c *Costs) rate(ctx context.Context) (UsdRate, error) {
	rows, err := c.Pool.Query(ctx, `SELECT name, value::text FROM settings
		WHERE name IN ('pricing_usd_rate', 'orders_currency')`)
	if err != nil {
		return UsdRate{}, err
	}
	stored := map[string]string{}
	for rows.Next() {
		var name, value string
		if err := rows.Scan(&name, &value); err != nil {
			rows.Close()
			return UsdRate{}, err
		}
		stored[name] = value
	}
	if err := rows.Err(); err != nil {
		return UsdRate{}, err
	}
	text, err := setting(ctx, stored, "pricing_usd_rate", "1", checkRate)
	if err != nil {
		return UsdRate{}, err
	}
	currency, err := setting(ctx, stored, "orders_currency", "USD", checkCurrency)
	if err != nil {
		return UsdRate{}, err
	}
	perUSD, _, _ := ParseDecimal(text)
	return UsdRate{PerUSD: perUSD, Currency: currency}, nil
}

// setting is one env-seeded field: its stored JSON string when check takes it, else the
// environment's, else fallback.
func setting(ctx context.Context, stored map[string]string, name, fallback string,
	check func(string) (string, bool)) (string, error) {
	if raw, ok := stored[name]; ok {
		var s *string
		if json.Unmarshal([]byte(raw), &s) == nil && s != nil {
			if v, ok := check(*s); ok {
				return v, nil
			}
		}
		slog.WarnContext(ctx, "ignoring a stored setting this version refuses", "setting", name)
	}
	env := "SCADBUDDY_" + strings.ToUpper(name)
	if v, ok := os.LookupEnv(env); ok {
		checked, ok := check(v)
		if !ok {
			return "", fmt.Errorf("%s is not a value the API takes", env)
		}
		return checked, nil
	}
	return fallback, nil
}

// checkRate is settings.py decimal_string with positive: finite, above 0, below 10**9.
func checkRate(value string) (string, bool) {
	text := strings.TrimFunc(value, pySpace)
	d, special, ok := ParseDecimal(text)
	if special || !ok {
		return "", false
	}
	zero := Decimal{coef: pow10(0).SetInt64(0)}
	return text, d.cmp(zero) > 0 && d.cmp(maxRate) < 0
}

func checkCurrency(code string) (string, bool) {
	return code, currencyCode.MatchString(code) && !noMinorUnit[code]
}

func pySpace(r rune) bool {
	return r == ' ' || (r >= '\t' && r <= '\r') || (r >= 0x1c && r <= 0x1f) || r == 0x85 ||
		r == 0xa0 || r == 0x1680 || (r >= 0x2000 && r <= 0x200a) || r == 0x2028 ||
		r == 0x2029 || r == 0x202f || r == 0x205f || r == 0x3000
}

type item struct{ id, status string }

func (c *Costs) report(r *http.Request, p crud.Params) (any, error) {
	orderID := p["order_id"]
	body, err := crud.ReadBody(r, "session_id", "principal", "order_item_id", "cost_usd")
	if err != nil {
		return nil, err
	}
	session := body.PyUUID("session_id")
	principal := body.Get("principal").OptString(0, principalMax, nil)
	itemID := body.Get("order_item_id").OptString(0, itemIDMax, nil)
	var cost Decimal
	if raw, given := body.Raw("cost_usd"); !given {
		body.FailAt([]string{"cost_usd"}, "Field required")
	} else {
		var issue string
		if cost, issue = costIssue(raw); issue != "" {
			body.FailAt([]string{"cost_usd"}, issue)
		}
	}
	if err := body.Err(); err != nil {
		return nil, err
	}
	if strings.ContainsRune(orderID, 0) {
		return nil, &httpx.Problem{Status: http.StatusBadRequest, Detail: "a path value holds a NUL byte"}
	}
	ctx := r.Context()
	rate, err := c.rate(ctx)
	if err != nil {
		return nil, err
	}
	by := actor{Kind: "agent", Principal: principal, Session: &session}
	var out answer
	var change *toldChange
	err = pgx.BeginFunc(ctx, c.Pool, func(tx pgx.Tx) error {
		out, change = answer{}, nil
		var number, source *string
		var status, currency, orderSource string
		var submitted *time.Time
		err := tx.QueryRow(ctx, `SELECT number, status, currency, source, submitted_at
			FROM orders WHERE id = $1 FOR UPDATE`, orderID).Scan(&number, &status, &currency,
			&orderSource, &submitted)
		if errors.Is(err, pgx.ErrNoRows) {
			return refused(http.StatusNotFound, "order_not_found", "no order "+orderID)
		}
		if err != nil {
			return err
		}
		label := "cart " + orderID
		if number != nil {
			label = *number
		}
		// A visitor's cart is no job yet: nothing is spent on it (storefront spec §6.3).
		if orderSource == "storefront" && submitted == nil {
			return refused(http.StatusConflict, "order_not_submitted", label+" is not yet submitted")
		}
		// The item the session revised may be gone by now: the spend still counts, on the order.
		var items []item
		if itemID != nil {
			var found item
			var owner string
			err := tx.QueryRow(ctx, `SELECT id, order_id, status FROM order_items WHERE id = $1`,
				*itemID).Scan(&found.id, &owner, &found.status)
			switch {
			case err == nil && owner == orderID:
				items = []item{found}
			case err != nil && !errors.Is(err, pgx.ErrNoRows):
				return err
			}
		}
		var entryItem *string
		if len(items) > 0 {
			entryItem = &items[0].id
		}
		factor, ok := rate.Factor(currency)
		if !ok {
			return refused(http.StatusConflict, "currency_unconvertible",
				fmt.Sprintf("%s is in %s; the USD rate is in %s", label, currency, rate.Currency))
		}
		amount, fits := cost.Mul(factor).Minor(MinorExponent(currency))
		if !fits {
			// Python sends it, and Postgres refuses it: bigint out of range.
			return fmt.Errorf("an assistant cost past a bigint on order %s", orderID)
		}
		src := "ai_session:" + session
		source = &src
		if amount < 1 {
			out.Entry, err = entryOfSource(ctx, tx, orderID, src)
			return err
		}
		note := fmt.Sprintf("assistant session %s: %s USD", session, cost)
		createdBy, _ := json.Marshal(by)
		inserted, err := scanEntry(tx.QueryRow(ctx, `INSERT INTO order_cost_entries (order_id,
			order_item_id, kind, amount_minor, note, source, created_by)
			VALUES ($1, $2, 'assistant', $3, $4, $5, $6::jsonb)
			ON CONFLICT (order_id, kind, source) WHERE source IS NOT NULL DO NOTHING
			RETURNING `+entryColumns, orderID, entryItem, amount, note, source, string(createdBy)))
		if err != nil {
			return err
		}
		if inserted == nil {
			out.Entry, err = entryOfSource(ctx, tx, orderID, src)
			if err == nil && out.Entry == nil {
				err = errors.New("the conflicting cost entry is not there")
			}
			return err
		}
		if _, err := tx.Exec(ctx, `UPDATE orders SET updated_at = now() WHERE id = $1`, orderID); err != nil {
			return err
		}
		detail, _ := json.Marshal(map[string]any{
			"entry_id": inserted.ID, "item_id": inserted.OrderItemID, "entry_kind": inserted.Kind,
			"amount_minor": inserted.AmountMinor,
		})
		if _, err := tx.Exec(ctx, `INSERT INTO order_events (order_id, by, kind, detail)
			VALUES ($1, $2::jsonb, 'cost.added', $3::jsonb)`, orderID, string(createdBy), string(detail)); err != nil {
			return err
		}
		out = answer{Entry: inserted, Written: true}
		change = &toldChange{orderID: orderID, status: status, items: items}
		return nil
	})
	if err != nil {
		return nil, err
	}
	if change != nil {
		change.publish(ctx, c.Pool)
	}
	if !out.Written {
		return &crud.Reply{Status: http.StatusOK, Value: out}, nil
	}
	return out, nil
}

// toldChange is lifecycle.py _told for this change: the order, then each item, on the bus.
type toldChange struct {
	orderID, status string
	items           []item
}

func (t *toldChange) publish(ctx context.Context, pool *pgxpool.Pool) {
	bus.Publish(ctx, pool, "order.changed",
		httpx.Field{Key: "order_id", Value: t.orderID}, httpx.Field{Key: "status", Value: t.status},
		httpx.Field{Key: "cart", Value: false}, httpx.Field{Key: "deleted", Value: false})
	for _, it := range t.items {
		bus.Publish(ctx, pool, "order_item.changed",
			httpx.Field{Key: "order_id", Value: t.orderID}, httpx.Field{Key: "item_id", Value: it.id},
			httpx.Field{Key: "status", Value: it.status}, httpx.Field{Key: "attempt", Value: nil},
			httpx.Field{Key: "cart", Value: false})
	}
}

const entryColumns = `id, order_id, order_item_id, kind, amount_minor, note, source,
 created_by::text, created_at`

func scanEntry(row pgx.Row) (*entry, error) {
	var e entry
	var createdBy string
	var created time.Time
	err := row.Scan(&e.ID, &e.OrderID, &e.OrderItemID, &e.Kind, &e.AmountMinor, &e.Note,
		&e.Source, &createdBy, &created)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	if err := json.Unmarshal([]byte(createdBy), &e.CreatedBy); err != nil {
		return nil, err
	}
	e.CreatedAt = crud.Time(created)
	return &e, nil
}

func entryOfSource(ctx context.Context, tx pgx.Tx, orderID, source string) (*entry, error) {
	return scanEntry(tx.QueryRow(ctx, `SELECT `+entryColumns+` FROM order_cost_entries
		WHERE order_id = $1 AND kind = 'assistant' AND source = $2`, orderID, source))
}
