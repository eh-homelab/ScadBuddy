// Package jsonv is JSON as Python's json module holds it: an object keeps its members' order,
// and Dumps writes the text json.dumps(value, ensure_ascii=False, separators=(",", ":")) writes.
// The gate's events are built here as the Python API built them, so a row reads the same
// whichever process wrote it.
package jsonv

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/jackc/pgx/v5/pgtype"
)

// Member is one name and value of an Object.
type Member struct {
	Key   string
	Value any
}

// Object is a JSON object in its members' order. A value is nil, bool, string, json.Number,
// an int, a float64, []any, []string, Object or json.RawMessage.
type Object []Member

// Get is the value of key, and whether the object has it.
func (o Object) Get(key string) (any, bool) {
	for _, m := range o {
		if m.Key == key {
			return m.Value, true
		}
	}
	return nil, false
}

// Has says whether the object has key.
func (o Object) Has(key string) bool {
	_, ok := o.Get(key)
	return ok
}

// Text is the string member key, "" and false when it is absent or no string.
func (o Object) Text(key string) (string, bool) {
	v, _ := o.Get(key)
	s, ok := v.(string)
	return s, ok
}

// MarshalJSON writes the object as Dumps does.
func (o Object) MarshalJSON() ([]byte, error) { return []byte(Dumps(o)), nil }

// Parse is raw as a value: objects as Object, numbers as json.Number. As Python's dict, a
// repeated name keeps its first place and its last value. As Python's str, a string keeps a
// lone surrogate escape (held as its WTF-8 bytes), which Dumps writes as JSON.stringify does.
func Parse(raw []byte) (any, error) {
	p := &parser{b: raw}
	v, err := p.value()
	if err != nil {
		return nil, err
	}
	p.space()
	if p.i != len(p.b) {
		return nil, errors.New("jsonv: data after the value")
	}
	return v, nil
}

// MustParse is Parse of text that is JSON already (a jsonb column's text).
func MustParse(text string) any {
	v, err := Parse([]byte(text))
	if err != nil {
		panic(err)
	}
	return v
}

type parser struct {
	b []byte
	i int
}

var errSyntax = errors.New("jsonv: not JSON")

func (p *parser) space() {
	for p.i < len(p.b) && (p.b[p.i] == ' ' || p.b[p.i] == '\t' || p.b[p.i] == '\n' || p.b[p.i] == '\r') {
		p.i++
	}
}

func (p *parser) literal(word string, v any) (any, error) {
	if !bytes.HasPrefix(p.b[p.i:], []byte(word)) {
		return nil, errSyntax
	}
	p.i += len(word)
	return v, nil
}

func (p *parser) value() (any, error) {
	p.space()
	if p.i >= len(p.b) {
		return nil, errSyntax
	}
	switch c := p.b[p.i]; {
	case c == '{':
		p.i++
		o := Object{}
		p.space()
		if p.i < len(p.b) && p.b[p.i] == '}' {
			p.i++
			return o, nil
		}
		for {
			p.space()
			key, err := p.str()
			if err != nil {
				return nil, err
			}
			p.space()
			if p.i >= len(p.b) || p.b[p.i] != ':' {
				return nil, errSyntax
			}
			p.i++
			v, err := p.value()
			if err != nil {
				return nil, err
			}
			replaced := false
			for j := range o {
				if o[j].Key == key {
					o[j].Value, replaced = v, true
				}
			}
			if !replaced {
				o = append(o, Member{key, v})
			}
			if done, err := p.next('}'); done || err != nil {
				return o, err
			}
		}
	case c == '[':
		p.i++
		a := []any{}
		p.space()
		if p.i < len(p.b) && p.b[p.i] == ']' {
			p.i++
			return a, nil
		}
		for {
			v, err := p.value()
			if err != nil {
				return nil, err
			}
			a = append(a, v)
			if done, err := p.next(']'); done || err != nil {
				return a, err
			}
		}
	case c == '"':
		return p.str()
	case c == 't':
		return p.literal("true", true)
	case c == 'f':
		return p.literal("false", false)
	case c == 'n':
		return p.literal("null", nil)
	default:
		start := p.i
		for p.i < len(p.b) && bytes.IndexByte([]byte("+-0123456789.eE"), p.b[p.i]) >= 0 {
			p.i++
		}
		n := json.Number(p.b[start:p.i])
		if !json.Valid([]byte(n)) {
			return nil, errSyntax
		}
		return n, nil
	}
}

// next reads a comma, or the closing close (done).
func (p *parser) next(close byte) (bool, error) {
	p.space()
	if p.i >= len(p.b) {
		return false, errSyntax
	}
	switch p.b[p.i] {
	case ',':
		p.i++
		return false, nil
	case close:
		p.i++
		return true, nil
	}
	return false, errSyntax
}

func (p *parser) str() (string, error) {
	if p.i >= len(p.b) || p.b[p.i] != '"' {
		return "", errSyntax
	}
	p.i++
	var out []byte
	for p.i < len(p.b) {
		c := p.b[p.i]
		switch {
		case c == '"':
			p.i++
			return string(out), nil
		case c < 0x20:
			return "", errSyntax
		case c != '\\':
			out = append(out, c)
			p.i++
			continue
		}
		if p.i+1 >= len(p.b) {
			return "", errSyntax
		}
		e := p.b[p.i+1]
		p.i += 2
		switch e {
		case '"', '\\', '/':
			out = append(out, e)
		case 'b':
			out = append(out, '\b')
		case 'f':
			out = append(out, '\f')
		case 'n':
			out = append(out, '\n')
		case 'r':
			out = append(out, '\r')
		case 't':
			out = append(out, '\t')
		case 'u':
			r, ok := p.hex4()
			if !ok {
				return "", errSyntax
			}
			if r >= 0xd800 && r < 0xdc00 && bytes.HasPrefix(p.b[p.i:], []byte(`\u`)) {
				save := p.i
				p.i += 2
				if low, ok := p.hex4(); ok && low >= 0xdc00 && low < 0xe000 {
					out = utf8.AppendRune(out, 0x10000+(r-0xd800)<<10+(low-0xdc00))
					continue
				}
				p.i = save
			}
			out = appendWTF8(out, r)
		default:
			return "", errSyntax
		}
	}
	return "", errSyntax
}

func (p *parser) hex4() (rune, bool) {
	if p.i+4 > len(p.b) {
		return 0, false
	}
	var r rune
	for _, c := range p.b[p.i : p.i+4] {
		r <<= 4
		switch {
		case c >= '0' && c <= '9':
			r |= rune(c - '0')
		case c >= 'a' && c <= 'f':
			r |= rune(c - 'a' + 10)
		case c >= 'A' && c <= 'F':
			r |= rune(c - 'A' + 10)
		default:
			return 0, false
		}
	}
	p.i += 4
	return r, true
}

// appendWTF8 appends r, a lone surrogate as its three generalized UTF-8 bytes.
func appendWTF8(b []byte, r rune) []byte {
	if r >= 0xd800 && r < 0xe000 {
		return append(b, 0xe0|byte(r>>12), 0x80|byte(r>>6)&0x3f, 0x80|byte(r)&0x3f)
	}
	return utf8.AppendRune(b, r)
}

// surrogate is the lone surrogate s holds at i, or -1.
func surrogate(s string, i int) rune {
	if i+2 < len(s) && s[i] == 0xed && s[i+1] >= 0xa0 && s[i+1] <= 0xbf {
		return 0xd000 | rune(s[i+1]&0x3f)<<6 | rune(s[i+2]&0x3f)
	}
	return -1
}

// UTF16 is s's UTF-16 code units, a lone surrogate as itself.
func UTF16(s string) []uint16 {
	var out []uint16
	for i := 0; i < len(s); {
		if r := surrogate(s, i); r >= 0 {
			out = append(out, uint16(r))
			i += 3
			continue
		}
		r, n := utf8.DecodeRuneInString(s[i:])
		if r > 0xffff {
			r -= 0x10000
			out = append(out, uint16(0xd800+(r>>10)), uint16(0xdc00+(r&0x3ff)))
		} else {
			out = append(out, uint16(r))
		}
		i += n
	}
	return out
}

// Dumps is v as Python's compact json.dumps with ensure_ascii=False writes it.
func Dumps(v any) string {
	var b bytes.Buffer
	write(&b, v, false, false)
	return b.String()
}

// ASCIILen is the length in characters of Python's json.dumps(v): ensure_ascii, ", " and ": ".
func ASCIILen(v any) int {
	var b bytes.Buffer
	write(&b, v, true, true)
	return b.Len()
}

func write(b *bytes.Buffer, v any, ascii, spaced bool) {
	comma, colon := ",", ":"
	if spaced {
		comma, colon = ", ", ": "
	}
	switch t := v.(type) {
	case nil:
		b.WriteString("null")
	case bool:
		b.WriteString(strconv.FormatBool(t))
	case string:
		quote(b, t, ascii)
	case json.Number:
		b.WriteString(string(t))
	case int:
		b.WriteString(strconv.Itoa(t))
	case int64:
		b.WriteString(strconv.FormatInt(t, 10))
	case float64:
		b.WriteString(Float(t))
	case json.RawMessage:
		parsed, err := Parse(t)
		if err != nil {
			panic(err)
		}
		write(b, parsed, ascii, spaced)
	case []string:
		b.WriteByte('[')
		for i, s := range t {
			if i > 0 {
				b.WriteString(comma)
			}
			quote(b, s, ascii)
		}
		b.WriteByte(']')
	case []any:
		b.WriteByte('[')
		for i, e := range t {
			if i > 0 {
				b.WriteString(comma)
			}
			write(b, e, ascii, spaced)
		}
		b.WriteByte(']')
	case Object:
		b.WriteByte('{')
		for i, m := range t {
			if i > 0 {
				b.WriteString(comma)
			}
			quote(b, m.Key, ascii)
			b.WriteString(colon)
			write(b, m.Value, ascii, spaced)
		}
		b.WriteByte('}')
	case []Object:
		b.WriteByte('[')
		for i, e := range t {
			if i > 0 {
				b.WriteString(comma)
			}
			write(b, e, ascii, spaced)
		}
		b.WriteByte(']')
	default:
		panic(fmt.Sprintf("jsonv: no JSON for %T", v))
	}
}

// Float is Python's repr of a float, as json.dumps writes one.
func Float(f float64) string {
	// Python's repr is positional for decimal exponents -4 to 15 and
	// scientific outside them (1e+16, 1e-05); Go's 'g' switches at 1e+21
	// for shortest output, so the exponent is read from 'e' first.
	s := strconv.FormatFloat(f, 'e', -1, 64)
	at := strings.IndexByte(s, 'e')
	if exp, _ := strconv.Atoi(s[at+1:]); exp < -4 || exp >= 16 {
		return s
	}
	s = strconv.FormatFloat(f, 'f', -1, 64)
	if !strings.Contains(s, ".") {
		s += ".0"
	}
	return s
}

const hex = "0123456789abcdef"

func quote(b *bytes.Buffer, s string, ascii bool) {
	b.WriteByte('"')
	for i := 0; i < len(s); {
		if r := surrogate(s, i); r >= 0 {
			unicode(b, r)
			i += 3
			continue
		}
		r, n := utf8.DecodeRuneInString(s[i:])
		i += n
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
			case r < 0x20 || (ascii && r > 0x7e && r <= 0xffff):
				unicode(b, r)
			case ascii && r > 0xffff:
				r -= 0x10000
				unicode(b, 0xd800+(r>>10))
				unicode(b, 0xdc00+(r&0x3ff))
			default:
				b.WriteRune(r)
			}
		}
	}
	b.WriteByte('"')
}

func unicode(b *bytes.Buffer, r rune) {
	b.WriteString(`\u`)
	for shift := 12; shift >= 0; shift -= 4 {
		b.WriteByte(hex[(r>>shift)&0xf])
	}
}

// UTF16Len is a JavaScript string's length: what the agent's bounds count.
func UTF16Len(s string) int { return len(UTF16(s)) }

// Chars is Python's len of a string: its code points.
func Chars(s string) int { return utf8.RuneCountInString(s) }

// Time is a timestamptz column as pydantic writes a datetime: UTC, "Z", microseconds when
// there are any.
type Time struct{ time.Time }

// ScanTimestamptz reads the column (pgtype.TimestamptzScanner).
func (t *Time) ScanTimestamptz(v pgtype.Timestamptz) error {
	if !v.Valid {
		return errors.New("jsonv: a null timestamptz into a Time; scan into *Time")
	}
	t.Time = v.Time
	return nil
}

// MarshalJSON writes the time as pydantic does.
func (t Time) MarshalJSON() ([]byte, error) {
	u := t.UTC()
	s := u.Format("2006-01-02T15:04:05")
	if us := u.Nanosecond() / 1000; us != 0 {
		s += fmt.Sprintf(".%06d", us)
	}
	return []byte(`"` + s + `Z"`), nil
}

// ISO is JavaScript's toISOString of t: UTC, milliseconds, "Z".
func ISO(t time.Time) string { return t.UTC().Format("2006-01-02T15:04:05.000Z") }
