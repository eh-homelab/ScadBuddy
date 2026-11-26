package bus

import (
	"errors"
	"math"
	"strconv"
	"strings"
	"unicode"
	"unicode/utf16"
	"unicode/utf8"
)

// Value is a JSON value as Python's json.loads reads it: an object keeps its members in
// the order they came, a repeated key keeps its first place and its last value, and a
// number is an int when it has no fraction and no exponent.
type Value struct {
	Kind  byte // 'n' null, 'b' bool, 'i' int, 'f' float, 's' string, 'a' list, 'o' object
	Bool  bool
	Int   string // an int's digits, as Python writes them
	Float float64
	Str   string
	Items []Value
	Keys  []string
	Vals  []Value
}

// Get is the member name of an object, nil when it has none.
func (v *Value) Get(name string) *Value {
	if v == nil || v.Kind != 'o' {
		return nil
	}
	for i, k := range v.Keys {
		if k == name {
			return &v.Vals[i]
		}
	}
	return nil
}

var errJSON = errors.New("not JSON")

// ParseJSON reads text as Python's json.loads does (NaN, Infinity and -Infinity too). A lone
// surrogate escape reads as U+FFFD: a Go string cannot hold one.
func ParseJSON(text string) (Value, error) {
	p := &parser{s: text}
	p.space()
	v, err := p.value(0)
	if err != nil {
		return Value{}, err
	}
	p.space()
	if p.i != len(p.s) {
		return Value{}, errJSON
	}
	return v, nil
}

type parser struct {
	s string
	i int
}

func (p *parser) space() {
	for p.i < len(p.s) && strings.IndexByte(" \t\n\r", p.s[p.i]) >= 0 {
		p.i++
	}
}

func (p *parser) lit(word string) bool {
	if strings.HasPrefix(p.s[p.i:], word) {
		p.i += len(word)
		return true
	}
	return false
}

func (p *parser) value(depth int) (Value, error) {
	if depth > 1000 || p.i >= len(p.s) {
		return Value{}, errJSON
	}
	switch c := p.s[p.i]; {
	case c == '{':
		return p.object(depth)
	case c == '[':
		return p.list(depth)
	case c == '"':
		s, err := p.str()
		return Value{Kind: 's', Str: s}, err
	case p.lit("null"):
		return Value{Kind: 'n'}, nil
	case p.lit("true"):
		return Value{Kind: 'b', Bool: true}, nil
	case p.lit("false"):
		return Value{Kind: 'b'}, nil
	case p.lit("NaN"):
		return Value{Kind: 'f', Float: math.NaN()}, nil
	case p.lit("Infinity"):
		return Value{Kind: 'f', Float: math.Inf(1)}, nil
	case p.lit("-Infinity"):
		return Value{Kind: 'f', Float: math.Inf(-1)}, nil
	default:
		return p.number()
	}
}

func (p *parser) object(depth int) (Value, error) {
	v := Value{Kind: 'o', Keys: []string{}, Vals: []Value{}}
	p.i++
	p.space()
	if p.lit("}") {
		return v, nil
	}
	for {
		p.space()
		if p.i >= len(p.s) || p.s[p.i] != '"' {
			return Value{}, errJSON
		}
		key, err := p.str()
		if err != nil {
			return Value{}, err
		}
		p.space()
		if !p.lit(":") {
			return Value{}, errJSON
		}
		p.space()
		item, err := p.value(depth + 1)
		if err != nil {
			return Value{}, err
		}
		if old := v.Get(key); old != nil {
			*old = item
		} else {
			v.Keys, v.Vals = append(v.Keys, key), append(v.Vals, item)
		}
		p.space()
		if p.lit("}") {
			return v, nil
		}
		if !p.lit(",") {
			return Value{}, errJSON
		}
	}
}

func (p *parser) list(depth int) (Value, error) {
	v := Value{Kind: 'a', Items: []Value{}}
	p.i++
	p.space()
	if p.lit("]") {
		return v, nil
	}
	for {
		p.space()
		item, err := p.value(depth + 1)
		if err != nil {
			return Value{}, err
		}
		v.Items = append(v.Items, item)
		p.space()
		if p.lit("]") {
			return v, nil
		}
		if !p.lit(",") {
			return Value{}, errJSON
		}
	}
}

func (p *parser) number() (Value, error) {
	start := p.i
	digits := func() int {
		n := 0
		for p.i < len(p.s) && p.s[p.i] >= '0' && p.s[p.i] <= '9' {
			p.i++
			n++
		}
		return n
	}
	if p.i < len(p.s) && p.s[p.i] == '-' {
		p.i++
	}
	first := p.i
	if n := digits(); n == 0 || n > 1 && p.s[first] == '0' {
		return Value{}, errJSON
	}
	float := false
	if p.i < len(p.s) && p.s[p.i] == '.' {
		p.i++
		if digits() == 0 {
			return Value{}, errJSON
		}
		float = true
	}
	if p.i < len(p.s) && (p.s[p.i] == 'e' || p.s[p.i] == 'E') {
		p.i++
		if p.i < len(p.s) && (p.s[p.i] == '+' || p.s[p.i] == '-') {
			p.i++
		}
		if digits() == 0 {
			return Value{}, errJSON
		}
		float = true
	}
	text := p.s[start:p.i]
	if float {
		f, err := strconv.ParseFloat(text, 64)
		if err != nil && !errors.Is(err, strconv.ErrRange) {
			return Value{}, errJSON
		}
		return Value{Kind: 'f', Float: f}, nil
	}
	if text == "-0" {
		text = "0"
	}
	return Value{Kind: 'i', Int: text}, nil
}

func (p *parser) str() (string, error) {
	p.i++
	var b strings.Builder
	for {
		if p.i >= len(p.s) {
			return "", errJSON
		}
		c := p.s[p.i]
		switch {
		case c == '"':
			p.i++
			return b.String(), nil
		case c < 0x20:
			return "", errJSON // json.loads is strict about control characters
		case c != '\\':
			r, size := utf8.DecodeRuneInString(p.s[p.i:])
			b.WriteRune(r)
			p.i += size
			continue
		}
		p.i++
		if p.i >= len(p.s) {
			return "", errJSON
		}
		e := p.s[p.i]
		p.i++
		switch e {
		case '"', '\\', '/':
			b.WriteByte(e)
		case 'b':
			b.WriteByte('\b')
		case 'f':
			b.WriteByte('\f')
		case 'n':
			b.WriteByte('\n')
		case 'r':
			b.WriteByte('\r')
		case 't':
			b.WriteByte('\t')
		case 'u':
			r, ok := p.hex4()
			if !ok {
				return "", errJSON
			}
			if utf16.IsSurrogate(r) && r < 0xdc00 && strings.HasPrefix(p.s[p.i:], `\u`) {
				save := p.i
				p.i += 2
				if low, ok := p.hex4(); ok && low >= 0xdc00 && low <= 0xdfff {
					r = utf16.DecodeRune(r, low)
				} else {
					p.i = save
				}
			}
			b.WriteRune(r) // a lone surrogate is written as U+FFFD
		default:
			return "", errJSON
		}
	}
}

func (p *parser) hex4() (rune, bool) {
	if p.i+4 > len(p.s) {
		return 0, false
	}
	n, err := strconv.ParseUint(p.s[p.i:p.i+4], 16, 32)
	if err != nil {
		return 0, false
	}
	p.i += 4
	return rune(n), true
}

// Dumps is v as Python's json.dumps writes it: with ensure_ascii when ascii, and with the
// separators "," and ":" when compact, else ", " and ": ".
func Dumps(v Value, ascii, compact bool) string {
	var b strings.Builder
	dump(&b, v, ascii, compact)
	return b.String()
}

func dump(b *strings.Builder, v Value, ascii, compact bool) {
	comma, colon := ", ", ": "
	if compact {
		comma, colon = ",", ":"
	}
	switch v.Kind {
	case 'n':
		b.WriteString("null")
	case 'b':
		b.WriteString(strconv.FormatBool(v.Bool))
	case 'i':
		b.WriteString(v.Int)
	case 'f':
		switch {
		case math.IsNaN(v.Float):
			b.WriteString("NaN")
		case math.IsInf(v.Float, 1):
			b.WriteString("Infinity")
		case math.IsInf(v.Float, -1):
			b.WriteString("-Infinity")
		default:
			b.WriteString(PyFloat(v.Float))
		}
	case 's':
		b.WriteString(Quote(v.Str, ascii))
	case 'a':
		b.WriteByte('[')
		for i, item := range v.Items {
			if i > 0 {
				b.WriteString(comma)
			}
			dump(b, item, ascii, compact)
		}
		b.WriteByte(']')
	case 'o':
		b.WriteByte('{')
		for i, k := range v.Keys {
			if i > 0 {
				b.WriteString(comma)
			}
			b.WriteString(Quote(k, ascii))
			b.WriteString(colon)
			dump(b, v.Vals[i], ascii, compact)
		}
		b.WriteByte('}')
	}
}

// Quote is s as a JSON string, as Python's json.dumps writes it with ensure_ascii (every
// character outside space to "~" escaped) or without it (only '"', '\' and the control
// characters). pydantic's model_dump_json writes strings as the latter.
func Quote(s string, ascii bool) string {
	var b strings.Builder
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
			switch {
			case r < 0x20 || ascii && r >= 0x7f && r < 0x10000:
				b.WriteString(`\u`)
				b.WriteString(hex4(r))
			case ascii && r >= 0x10000:
				hi, lo := utf16.EncodeRune(r)
				b.WriteString(`\u` + hex4(hi) + `\u` + hex4(lo))
			default:
				b.WriteRune(r)
			}
		}
	}
	b.WriteByte('"')
	return b.String()
}

func hex4(r rune) string {
	h := strconv.FormatInt(int64(r), 16)
	return strings.Repeat("0", 4-len(h)) + h
}

// PyFloat is f as Python's repr writes a finite float: the shortest digits that read back,
// fixed between 1e-4 and 1e16 (with ".0" when whole), else with an exponent of at least
// two digits.
func PyFloat(f float64) string {
	e := strconv.FormatFloat(f, 'e', -1, 64)
	_, exp, _ := strings.Cut(e, "e")
	n, _ := strconv.Atoi(exp)
	if n < -4 || n >= 16 {
		return e
	}
	s := strconv.FormatFloat(f, 'f', -1, 64)
	if !strings.ContainsAny(s, ".") {
		s += ".0"
	}
	return s
}

// Repr is v as Python's repr writes the value json.loads made of it: what the realtime
// socket's refusals quote ("unknown topics: ['x', 5]").
func Repr(v Value) string {
	var b strings.Builder
	repr(&b, v)
	return b.String()
}

func repr(b *strings.Builder, v Value) {
	switch v.Kind {
	case 'n':
		b.WriteString("None")
	case 'b':
		if v.Bool {
			b.WriteString("True")
		} else {
			b.WriteString("False")
		}
	case 'i':
		b.WriteString(v.Int)
	case 'f':
		switch {
		case math.IsNaN(v.Float):
			b.WriteString("nan")
		case math.IsInf(v.Float, 1):
			b.WriteString("inf")
		case math.IsInf(v.Float, -1):
			b.WriteString("-inf")
		default:
			b.WriteString(PyFloat(v.Float))
		}
	case 's':
		b.WriteString(reprStr(v.Str))
	case 'a':
		b.WriteByte('[')
		for i, item := range v.Items {
			if i > 0 {
				b.WriteString(", ")
			}
			repr(b, item)
		}
		b.WriteByte(']')
	case 'o':
		b.WriteByte('{')
		for i, k := range v.Keys {
			if i > 0 {
				b.WriteString(", ")
			}
			b.WriteString(reprStr(k))
			b.WriteString(": ")
			repr(b, v.Vals[i])
		}
		b.WriteByte('}')
	}
}

// ReprList is Python's repr of a list of strings.
func ReprList(items []Value) string { return Repr(Value{Kind: 'a', Items: items}) }

func reprStr(s string) string {
	quote := byte('\'')
	if strings.ContainsRune(s, '\'') && !strings.ContainsRune(s, '"') {
		quote = '"'
	}
	var b strings.Builder
	b.WriteByte(quote)
	for _, r := range s {
		switch {
		case r == rune(quote) || r == '\\':
			b.WriteByte('\\')
			b.WriteRune(r)
		case r == '\n':
			b.WriteString(`\n`)
		case r == '\r':
			b.WriteString(`\r`)
		case r == '\t':
			b.WriteString(`\t`)
		case r == ' ' || r < 0x7f && r >= 0x20:
			b.WriteRune(r)
		case printable(r):
			b.WriteRune(r)
		case r < 0x100:
			b.WriteString(`\x` + strconv.FormatInt(int64(r)|0x100, 16)[1:])
		case r < 0x10000:
			b.WriteString(`\u` + hex4(r))
		default:
			h := strconv.FormatInt(int64(r), 16)
			b.WriteString(`\U` + strings.Repeat("0", 8-len(h)) + h)
		}
	}
	b.WriteByte(quote)
	return b.String()
}

// printable is Python's str.isprintable for one character outside ASCII: not a control,
// format, surrogate, private-use or unassigned character, and no separator.
func printable(r rune) bool {
	return r > 0x7f && unicode.IsGraphic(r) && !unicode.Is(unicode.Zs, r) &&
		!unicode.In(r, unicode.Cc, unicode.Cf, unicode.Co, unicode.Cs, unicode.Zl, unicode.Zp)
}
