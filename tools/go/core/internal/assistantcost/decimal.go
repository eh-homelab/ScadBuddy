package assistantcost

import (
	"encoding/json"
	"math"
	"math/big"
	"regexp"
	"strconv"
	"strings"
	"unicode"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/jsonv"
)

// precision is Python's default decimal context: 28 significant digits, ROUND_HALF_EVEN.
const precision = 28

// Decimal is a finite Python Decimal: (-1)^neg * coef * 10^exp.
type Decimal struct {
	neg  bool
	coef *big.Int
	exp  int
}

var (
	ten          = big.NewInt(10)
	decimalText  = regexp.MustCompile(`^([+-])?((?:\d(?:_?\d)*)?)(?:\.((?:\d(?:_?\d)*)?))?(?:[eE]([+-]?\d(?:_?\d)*))?$`)
	specialValue = regexp.MustCompile(`(?i)^[+-]?(inf|infinity|s?nan\d*)$`)
)

func pow10(n int) *big.Int { return new(big.Int).Exp(ten, big.NewInt(int64(n)), nil) }

func digits(n *big.Int) int { return len(n.String()) }

// ParseDecimal is Decimal(text) for a string: exact, whitespace stripped, underscores between
// digits. special is true for an infinity or a NaN; ok is false for no number at all.
func ParseDecimal(text string) (d Decimal, special, ok bool) {
	text = strings.TrimFunc(text, unicode.IsSpace)
	if specialValue.MatchString(text) {
		return Decimal{}, true, true
	}
	// Python's Decimal reads any Unicode decimal digit; the agent sends ASCII.
	m := decimalText.FindStringSubmatch(text)
	if m == nil || (m[2] == "" && m[3] == "") {
		return Decimal{}, false, false
	}
	whole, frac := strings.ReplaceAll(m[2], "_", ""), strings.ReplaceAll(m[3], "_", "")
	coef, _ := new(big.Int).SetString(whole+frac+"0", 10)
	coef.Quo(coef, ten)
	exp := -len(frac)
	if m[4] != "" {
		e, err := strconv.Atoi(strings.ReplaceAll(m[4], "_", ""))
		if err != nil {
			return Decimal{}, false, false
		}
		exp += e
	}
	return Decimal{neg: m[1] == "-", coef: coef, exp: exp}, false, true
}

// round is the context's rounding to precision digits, half to even.
func (d Decimal) round() Decimal {
	n := digits(d.coef)
	if n <= precision {
		return d
	}
	drop := n - precision
	div := pow10(drop)
	q, r := new(big.Int).QuoRem(d.coef, div, new(big.Int))
	twice := new(big.Int).Lsh(r, 1)
	if c := twice.Cmp(div); c > 0 || (c == 0 && q.Bit(0) == 1) {
		q.Add(q, big.NewInt(1))
	}
	out := Decimal{neg: d.neg, coef: q, exp: d.exp + drop}
	if digits(q) > precision {
		out.coef.Quo(out.coef, ten)
		out.exp++
	}
	return out
}

// Mul is d * e in the default context.
func (d Decimal) Mul(e Decimal) Decimal {
	return Decimal{neg: d.neg != e.neg, coef: new(big.Int).Mul(d.coef, e.coef), exp: d.exp + e.exp}.round()
}

// Minor is to_minor: d scaled by exponent places, quantised to a whole number half up.
// false when it is past a bigint, which Postgres refuses.
func (d Decimal) Minor(exponent int) (int64, bool) {
	exp := d.exp + exponent
	coef := new(big.Int).Set(d.coef)
	if exp >= 0 {
		coef.Mul(coef, pow10(exp))
	} else {
		div := pow10(-exp)
		q, r := new(big.Int).QuoRem(coef, div, new(big.Int))
		if new(big.Int).Lsh(r, 1).Cmp(div) >= 0 {
			q.Add(q, big.NewInt(1))
		}
		coef = q
	}
	if d.neg {
		coef.Neg(coef)
	}
	return coef.Int64(), coef.IsInt64()
}

// pydanticNormal is pydantic's Decimal after its checks: exact, trailing zeros dropped, a
// positive exponent written out.
func (d Decimal) pydanticNormal() Decimal {
	coef, exp := new(big.Int).Set(d.coef), d.exp
	if coef.Sign() == 0 {
		return Decimal{neg: d.neg, coef: coef, exp: 0}
	}
	r := new(big.Int)
	for exp < 0 {
		q, rem := new(big.Int).QuoRem(coef, ten, r)
		if rem.Sign() != 0 {
			break
		}
		coef, exp = q, exp+1
	}
	if exp > 0 {
		coef.Mul(coef, pow10(exp))
		exp = 0
	}
	return Decimal{neg: d.neg, coef: coef, exp: exp}
}

// String is str(Decimal): Python's to-scientific-string.
func (d Decimal) String() string {
	ds := d.coef.String()
	left := d.exp + len(ds)
	dot := 1
	if d.exp <= 0 && left > -6 {
		dot = left
	}
	var s string
	switch {
	case dot <= 0:
		s = "0." + strings.Repeat("0", -dot) + ds
	case dot >= len(ds):
		s = ds + strings.Repeat("0", dot-len(ds))
	default:
		s = ds[:dot] + "." + ds[dot:]
	}
	if e := left - dot; e > 0 {
		s += "E+" + strconv.Itoa(e)
	} else if e < 0 {
		s += "E" + strconv.Itoa(e)
	}
	if d.neg {
		s = "-" + s
	}
	return s
}

func (d Decimal) cmp(e Decimal) int {
	a, b := new(big.Int).Set(d.coef), new(big.Int).Set(e.coef)
	if d.neg {
		a.Neg(a)
	}
	if e.neg {
		b.Neg(b)
	}
	if d.exp > e.exp {
		a.Mul(a, pow10(d.exp-e.exp))
	} else {
		b.Mul(b, pow10(e.exp-d.exp))
	}
	return a.Cmp(b)
}

// costIssue reads cost_usd as pydantic reads Annotated[Decimal, Field(ge=0, le=10**6,
// decimal_places=8)] from JSON: a number with a fraction or an exponent is a float first (its
// repr is what Decimal reads), an integer is exact, a string is Decimal's own text. The value,
// or the issue's message.
func costIssue(raw json.RawMessage) (Decimal, string) {
	var d Decimal
	switch {
	case len(raw) > 0 && raw[0] == '"':
		var s string
		_ = json.Unmarshal(raw, &s)
		parsed, special, ok := ParseDecimal(s)
		if special {
			return d, "Input should be a finite number"
		}
		if !ok {
			return d, "Input should be a valid decimal"
		}
		d = parsed
	case len(raw) > 0 && (raw[0] == '-' || (raw[0] >= '0' && raw[0] <= '9')):
		text := string(raw)
		if strings.ContainsAny(text, ".eE") {
			f, err := strconv.ParseFloat(text, 64)
			if err != nil && !math.IsInf(f, 0) {
				return d, "Input should be a valid decimal"
			}
			if math.IsInf(f, 0) {
				return d, "Input should be a finite number"
			}
			text = jsonv.PyFloat(f)
		}
		d, _, _ = ParseDecimal(text)
		if !strings.ContainsAny(text, ".eE") && d.coef.Sign() == 0 {
			d.neg = false // an int: Python has no -0
		}
	default:
		return d, "Decimal input should be an integer, float, string or Decimal object"
	}
	d = d.pydanticNormal()
	if -d.exp > 8 {
		return d, "Decimal input should have no more than 8 decimal places"
	}
	zero := Decimal{coef: new(big.Int)}
	if d.cmp(zero) < 0 {
		return d, "Input should be greater than or equal to 0"
	}
	if d.cmp(Decimal{coef: big.NewInt(1_000_000)}) > 0 {
		return d, "Input should be less than or equal to 1000000"
	}
	return d, ""
}
