package packages

import (
	"encoding/json"
	"testing"
)

// The values were measured with CPython 3.12's json.dumps and repr.
func TestPythonsLengthOfAPinsJSON(t *testing.T) {
	raw := `{"a": [1, 2.5, "é", "x\ny"], "b": null, "c": 1e20, "d": 1.50, "e": 0.00001, "f": "😀", "g": {}}`
	if got := pyDumpsLen(json.RawMessage(raw)); got != 108 {
		t.Fatalf("got %d, want 108", got)
	}
	for text, want := range map[string]string{
		"1e16": "1e+16", "1e15": "1000000000000000.0", "0.0001": "0.0001", "-0.0": "-0.0",
		"123456789012345678.0": "1.2345678901234568e+17", "7": "7", "-0": "0",
	} {
		if got, _ := pyNumber(text); got != want {
			t.Errorf("%s: got %s, want %s", text, got, want)
		}
	}
}

// CPython 3.12: len(json.dumps("\x7f")) is 8, and str(None) is "None".
func TestPythonsEscapeOfDELAndItsNameOfNull(t *testing.T) {
	if got := stringLen("\x7f"); got != 8 {
		t.Errorf("stringLen: got %d, want 8", got)
	}
	if got := pyStr(json.RawMessage("null")); got != "None" {
		t.Errorf("pyStr: got %q, want None", got)
	}
	if _, got := reviewName(json.RawMessage(`{"name": null}`)); got != "None" {
		t.Errorf("reviewName: got %q, want None", got)
	}
}
