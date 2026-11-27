package assistantcost

import (
	"encoding/json"
	"fmt"
	"os"
	"strings"
	"testing"
)

func TestTheCostAmountAndNoteArePythons(t *testing.T) {
	raw, err := os.ReadFile("../../../../../backend/tests/fixtures/assistant-cost-vectors.json")
	if err != nil {
		t.Fatal(err)
	}
	var cases []struct {
		CostUSD       string      `json:"cost_usd"`
		Rate          string      `json:"rate"`
		ShopCurrency  string      `json:"shop_currency"`
		OrderCurrency string      `json:"order_currency"`
		Valid         bool        `json:"valid"`
		Convertible   bool        `json:"convertible"`
		AmountMinor   json.Number `json:"amount_minor"`
		Note          string      `json:"note"`
	}
	if err := json.Unmarshal(raw, &cases); err != nil {
		t.Fatal(err)
	}
	if len(cases) == 0 {
		t.Fatal("no vectors")
	}
	for _, c := range cases {
		name := fmt.Sprintf("%s at %s %s for %s", c.CostUSD, c.Rate, c.ShopCurrency, c.OrderCurrency)
		cost, issue := costIssue(json.RawMessage(c.CostUSD))
		if (issue == "") != c.Valid {
			t.Errorf("%s: issue %q, want valid %v", name, issue, c.Valid)
			continue
		}
		if !c.Valid {
			continue
		}
		perUSD, _, ok := ParseDecimal(strings.TrimSpace(c.Rate))
		if !ok {
			t.Fatalf("%s: the rate does not parse", name)
		}
		factor, convertible := UsdRate{perUSD, c.ShopCurrency}.Factor(c.OrderCurrency)
		if convertible != c.Convertible {
			t.Errorf("%s: convertible %v", name, convertible)
			continue
		}
		if !convertible {
			continue
		}
		got, fits := cost.Mul(factor).Minor(MinorExponent(c.OrderCurrency))
		if want, err := c.AmountMinor.Int64(); fits != (err == nil) || (fits && got != want) {
			t.Errorf("%s: amount %d (fits %v), want %s", name, got, fits, c.AmountMinor)
		}
		note := fmt.Sprintf("assistant session 00000000-0000-4000-8000-000000000000: %s USD", cost)
		if note != c.Note {
			t.Errorf("%s: note %q, want %q", name, note, c.Note)
		}
	}
}
