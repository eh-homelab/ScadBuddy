package store

import (
	"math/big"
	"strings"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/inventory/wire"
)

// Quantities and costs are numeric(20,6) in minor units of the shop currency, unrounded
// (phase 2 inventory/stock.py): exact here as big.Rat, and six places where a column
// stores them.

// limit is _LIMIT: numeric(20,6) holds an absolute value below it.
var limit = new(big.Rat).SetInt(new(big.Int).Exp(big.NewInt(10), big.NewInt(14), nil))

func badQuantity(what string) error {
	return &Refused{Status: 422, Code: "bad_quantity",
		Detail: "a quantity or a cost is out of range: at most six decimal places, whole for an `each` item, " +
			"and small enough that a stock move's amount fits: " + what}
}

// six is _six: six places, half away from zero, as Postgres round (FloatString rounds
// half away from zero).
func six(r *big.Rat) *big.Rat {
	out, _ := new(big.Rat).SetString(r.FloatString(6))
	return out
}

// text is a decimal as models.py _Decimals writes it: plain, with no trailing zero.
func text(r *big.Rat) string {
	s := r.FloatString(6)
	if strings.Contains(s, ".") {
		s = strings.TrimRight(strings.TrimRight(s, "0"), ".")
	}
	if s == "-0" {
		return "0"
	}
	return s
}

// arg is r as a numeric parameter's text.
func arg(r *big.Rat) string { return r.FloatString(6) }

func abs(r *big.Rat) *big.Rat { return new(big.Rat).Abs(r) }

// amount is _amount: round(qty * cost, 6), with qty and the amount inside numeric(20,6);
// else bad_quantity.
func amount(qty, cost *big.Rat) (*big.Rat, error) {
	exact := new(big.Rat).Mul(qty, cost)
	if abs(qty).Cmp(limit) >= 0 || abs(exact).Cmp(limit) >= 0 {
		return nil, badQuantity(exact.FloatString(6))
	}
	out := six(exact)
	if abs(out).Cmp(limit) >= 0 {
		return nil, badQuantity(text(out))
	}
	return out, nil
}

// whole is _whole: an each item takes whole numbers only; every quantity has at most six
// places.
func whole(uom string, qty *big.Rat) error {
	if qty.Cmp(six(qty)) != 0 || (uom == "each" && !qty.IsInt()) {
		return badQuantity(qty.RatString())
	}
	return nil
}

func positive(uom string, qty *big.Rat) error {
	if qty.Sign() <= 0 {
		return badQuantity(qty.RatString())
	}
	return whole(uom, qty)
}

// decimal is a wire decimal as a number: bad_quantity when it is not one, or is past what
// numeric(20,6) holds.
func decimal(d wire.Decimal) (*big.Rat, error) {
	r, ok := new(big.Rat).SetString(string(d))
	if !ok || strings.ContainsAny(string(d), "/eE") {
		return nil, badQuantity(string(d))
	}
	if abs(r).Cmp(limit) >= 0 {
		return nil, badQuantity(string(d))
	}
	return r, nil
}

// cost is a cost given: 0 or more, at most six places.
func cost(d wire.Decimal) (*big.Rat, error) {
	r, err := decimal(d)
	if err != nil {
		return nil, err
	}
	if r.Sign() < 0 || r.Cmp(six(r)) != 0 {
		return nil, badQuantity(string(d))
	}
	return r, nil
}

// numeric reads a numeric column's text.
func numeric(s string) *big.Rat {
	r, _ := new(big.Rat).SetString(s)
	return r
}
