package turns

import (
	"encoding/json"
	"fmt"
	"io"
	"math"
	"math/big"
	"strconv"
	"strings"
)

// An event as Python's json.loads reads it: nil, bool, string, *big.Int (a number with no
// fraction or exponent), float64, []any, or *object (members in their first place, the last
// value of a member named twice).
type object struct {
	keys   []string
	values map[string]any
}

func (o *object) get(key string) any { return o.values[key] }

func (o *object) set(key string, value any) {
	if _, ok := o.values[key]; !ok {
		o.keys = append(o.keys, key)
	}
	o.values[key] = value
}

func newObject(pairs ...any) *object {
	o := &object{values: map[string]any{}}
	for i := 0; i < len(pairs); i += 2 {
		o.set(pairs[i].(string), pairs[i+1])
	}
	return o
}

// parse is text read as json.loads reads it.
func parse(text string) (any, error) {
	dec := json.NewDecoder(strings.NewReader(text))
	dec.UseNumber()
	v, err := value(dec)
	if err != nil {
		return nil, err
	}
	if _, err := dec.Token(); err != io.EOF {
		return nil, fmt.Errorf("more than one JSON value")
	}
	return v, nil
}

func value(dec *json.Decoder) (any, error) {
	token, err := dec.Token()
	if err != nil {
		return nil, err
	}
	switch t := token.(type) {
	case json.Delim:
		if t == '[' {
			list := []any{}
			for dec.More() {
				v, err := value(dec)
				if err != nil {
					return nil, err
				}
				list = append(list, v)
			}
			_, err := dec.Token()
			return list, err
		}
		o := newObject()
		for dec.More() {
			key, err := dec.Token()
			if err != nil {
				return nil, err
			}
			v, err := value(dec)
			if err != nil {
				return nil, err
			}
			o.set(key.(string), v)
		}
		_, err := dec.Token()
		return o, err
	case json.Number:
		if strings.ContainsAny(string(t), ".eE") {
			// Out of range is Python's inf, or its 0.
			f, _ := strconv.ParseFloat(string(t), 64)
			return f, nil
		}
		n, _ := new(big.Int).SetString(string(t), 10)
		return n, nil
	default:
		return t, nil
	}
}

// dumps is JSON.stringify's text of an event: its numbers as JavaScript writes them, its
// strings as Python's json.dumps(ensure_ascii=False) writes them.
func dumps(v any) string {
	var b strings.Builder
	write(&b, v)
	return b.String()
}

func write(b *strings.Builder, v any) {
	switch t := v.(type) {
	case nil:
		b.WriteString("null")
	case bool:
		b.WriteString(strconv.FormatBool(t))
	case string:
		writeString(b, t)
	case *big.Int:
		b.WriteString(t.String())
	case int:
		b.WriteString(strconv.Itoa(t))
	case int64:
		b.WriteString(strconv.FormatInt(t, 10))
	case float64:
		b.WriteString(jsNumber(t))
	case []any:
		b.WriteByte('[')
		for i, item := range t {
			if i > 0 {
				b.WriteByte(',')
			}
			write(b, item)
		}
		b.WriteByte(']')
	case *object:
		b.WriteByte('{')
		for i, key := range t.keys {
			if i > 0 {
				b.WriteByte(',')
			}
			writeString(b, key)
			b.WriteByte(':')
			write(b, t.values[key])
		}
		b.WriteByte('}')
	default:
		panic(fmt.Sprintf("%T is not JSON", v))
	}
}

func writeString(b *strings.Builder, s string) {
	b.WriteByte('"')
	for _, r := range s {
		switch r {
		case '"':
			b.WriteString(`\"`)
		case '\\':
			b.WriteString(`\\`)
		case '\n':
			b.WriteString(`\n`)
		case '\r':
			b.WriteString(`\r`)
		case '\t':
			b.WriteString(`\t`)
		case '\b':
			b.WriteString(`\b`)
		case '\f':
			b.WriteString(`\f`)
		default:
			if r < 0x20 {
				fmt.Fprintf(b, `\u%04x`, r)
			} else {
				b.WriteRune(r)
			}
		}
	}
	b.WriteByte('"')
}

// jsNumber is a float as JSON.stringify writes it (ECMA-262 Number::toString): the shortest
// digits that read back as it, in fixed notation for 1e-6 <= |x| < 1e21 and as 1.5e+21 or
// 1e-7 outside it; NaN and the infinities as null.
func jsNumber(v float64) string {
	switch {
	case math.IsNaN(v) || math.IsInf(v, 0):
		return "null"
	case v == 0:
		return "0"
	}
	if a := math.Abs(v); a >= 1e-6 && a < 1e21 {
		return strconv.FormatFloat(v, 'f', -1, 64)
	}
	mantissa, exponent, _ := strings.Cut(strconv.FormatFloat(v, 'e', -1, 64), "e")
	sign, digits := exponent[:1], strings.TrimLeft(exponent[1:], "0")
	return mantissa + "e" + sign + digits
}

// usd is "$" and amount.toFixed(2): the exact value rounded, a tie away from zero (Python's
// Decimal(amount).quantize(Decimal("0.01"), ROUND_HALF_UP)).
func usd(amount float64) string {
	exact := new(big.Rat).SetFloat64(amount)
	sign := ""
	if exact.Sign() < 0 || math.Signbit(amount) {
		sign = "-"
	}
	cents := new(big.Rat).Abs(exact)
	cents.Mul(cents, big.NewRat(100, 1)).Add(cents, big.NewRat(1, 2))
	digits := new(big.Int).Quo(cents.Num(), cents.Denom()).String()
	if len(digits) < 3 {
		digits = strings.Repeat("0", 3-len(digits)) + digits
	}
	return "$" + sign + digits[:len(digits)-2] + "." + digits[len(digits)-2:]
}

// wordTail is turnCore.ts wordTail: the result and a budget error with the row's figures.
func wordTail(tail []any, spent, budget float64, turns int64) []any {
	out := make([]any, len(tail))
	for i, e := range tail {
		o := e.(*object)
		switch {
		case o.get("type") == "session.result":
			o = o.clone()
			o.set("costUsd", spent)
			o.set("budgetUsd", budget)
			o.set("turns", turns)
		case o.get("type") == "error" && o.get("code") == "error_max_budget_usd":
			o = o.clone()
			o.set("message", fmt.Sprintf("this chat used its %s budget (%s spent)", usd(budget), usd(spent)))
		}
		out[i] = o
	}
	return out
}

func (o *object) clone() *object {
	c := &object{keys: append([]string{}, o.keys...), values: make(map[string]any, len(o.values))}
	for k, v := range o.values {
		c.values[k] = v
	}
	return c
}
