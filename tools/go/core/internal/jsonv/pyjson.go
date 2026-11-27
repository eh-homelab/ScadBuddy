package jsonv

import (
	"encoding/json"
	"math"
	"math/big"
	"strconv"
	"strings"
)

// Loads is raw as Python's json.loads reads it, its numbers as Python then writes them: a
// number with no fraction and no exponent is an int, written exactly; any other is a float,
// written as repr writes it (PyFloat). So Dumps(Loads(raw)) is json.dumps(json.loads(raw),
// ensure_ascii=False, separators=(",", ":")), pinned by python-json-vectors.json.
func Loads(raw []byte) (any, error) {
	v, err := Parse(raw)
	if err != nil {
		return nil, err
	}
	return pyNumbers(v), nil
}

func pyNumbers(v any) any {
	switch t := v.(type) {
	case json.Number:
		return json.Number(PyNumber(string(t)))
	case []any:
		for i := range t {
			t[i] = pyNumbers(t[i])
		}
	case Object:
		for i := range t {
			t[i].Value = pyNumbers(t[i].Value)
		}
	}
	return v
}

// PyNumber is a JSON number's text as Python's json.dumps(json.loads(text)) writes it.
func PyNumber(text string) string {
	if !strings.ContainsAny(text, ".eE") {
		n, ok := new(big.Int).SetString(text, 10)
		if ok {
			return n.String()
		}
	}
	f, err := strconv.ParseFloat(text, 64)
	if err != nil && !math.IsInf(f, 0) {
		return text
	}
	return PyFloat(f)
}

// PyFloat is float.__repr__ as json.dumps writes it: the shortest digits that read back,
// fixed notation for exponents from -4 to 15, else d.ddde+XX; Infinity and NaN as json
// writes them.
func PyFloat(f float64) string {
	switch {
	case math.IsNaN(f):
		return "NaN"
	case math.IsInf(f, 1):
		return "Infinity"
	case math.IsInf(f, -1):
		return "-Infinity"
	}
	s := strconv.FormatFloat(f, 'e', -1, 64)
	sign := ""
	if s[0] == '-' {
		sign, s = "-", s[1:]
	}
	mantissa, expText, _ := strings.Cut(s, "e")
	exp, _ := strconv.Atoi(expText)
	digits := strings.Replace(mantissa, ".", "", 1)
	if exp < -4 || exp >= 16 {
		out := digits[:1]
		if len(digits) > 1 {
			out += "." + digits[1:]
		}
		e := strconv.Itoa(abs(exp))
		if len(e) < 2 {
			e = "0" + e
		}
		if exp < 0 {
			return sign + out + "e-" + e
		}
		return sign + out + "e+" + e
	}
	if exp < 0 {
		return sign + "0." + strings.Repeat("0", -exp-1) + digits
	}
	if len(digits) <= exp+1 {
		return sign + digits + strings.Repeat("0", exp+1-len(digits)) + ".0"
	}
	return sign + digits[:exp+1] + "." + digits[exp+1:]
}

func abs(n int) int {
	if n < 0 {
		return -n
	}
	return n
}
