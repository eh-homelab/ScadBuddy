package assistantcost

import (
	"encoding/json"
	"testing"
	"time"
)

// A huge exponent must be refused by its size, never written out in full.
func TestAHugeExponentIsRefusedWithoutBeingExpanded(t *testing.T) {
	for _, c := range []struct{ raw, want string }{
		{`"1e999999999"`, "Input should be less than or equal to 1000000"},
		{`"-1e999999999"`, "Input should be greater than or equal to 0"},
		{`"0e999999999"`, ""},
		{`"1e6"`, ""},
		{`"1.000001e6"`, "Input should be less than or equal to 1000000"},
		{`"100000000.123456789"`, "Decimal input should have no more than 8 decimal places"},
	} {
		start := time.Now()
		_, issue := costIssue(json.RawMessage(c.raw))
		if issue != c.want {
			t.Errorf("%s: issue %q, want %q", c.raw, issue, c.want)
		}
		if d := time.Since(start); d > time.Second {
			t.Errorf("%s took %s", c.raw, d)
		}
	}
	big, _, _ := ParseDecimal("1e999999999")
	if big.cmp(maxRate) <= 0 {
		t.Error("1e999999999 is not above the rate cap")
	}
}
