package seal

import (
	"crypto/hkdf"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"math"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"unicode/utf16"
	"unicode/utf8"
)

// The agent's canonical JSON: what JSON.stringify writes for the value JavaScript parses
// from a JSON text, with object keys sorted by UTF-16 code unit at every depth. The approvals'
// input hash (backend flows/approval_hash.py, agent approvals/service.ts) writes numbers as
// ECMAScript does; a gate response's hash (agent_data/gate_canonical.py (removed in #7667)) holds none.

// InputHashInfo is the HKDF label of the approvals' input-hash key.
const InputHashInfo = "scadbuddy:ai_approvals:input_hash:v1"

// InputHashKey is HKDF-SHA256 of the KEK with no salt and InputHashInfo, 32 bytes.
func InputHashKey(kek *KEK) []byte {
	key, err := hkdf.Key(sha256.New, kek.key, nil, InputHashInfo, 32)
	if err != nil {
		panic(err)
	}
	return key
}

// InputHash is the hex HMAC-SHA256 of the canonical {"tool", "input"}, input a JSON text.
func InputHash(key []byte, tool, input string) (string, error) {
	// input is parsed alone, so no text in it can change the outer object.
	parsed, err := parse(input)
	if err != nil {
		return "", err
	}
	var b strings.Builder
	whole := jsObject{{key: utf16Units("tool"), value: jsString(utf16Units(tool))}, {key: utf16Units("input"), value: parsed}}
	if err := write(&b, whole, true); err != nil {
		return "", err
	}
	mac := hmac.New(sha256.New, key)
	mac.Write([]byte(b.String()))
	return hex.EncodeToString(mac.Sum(nil)), nil
}

// ResponseHash is the hex SHA-256 of a gate response's canonical JSON, which holds no number.
func ResponseHash(response string) (string, error) {
	text, err := Canonical(response, false)
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256([]byte(text))
	return hex.EncodeToString(sum[:]), nil
}

// ErrNumber is a number where canonical JSON takes none.
var ErrNumber = errors.New("a response holds no numbers, and canonical JSON has none")

// Canonical is the canonical form of the JSON text; numbers says whether it may hold any.
func Canonical(text string, numbers bool) (string, error) {
	v, err := parse(text)
	if err != nil {
		return "", err
	}
	var b strings.Builder
	if err := write(&b, v, numbers); err != nil {
		return "", err
	}
	return b.String(), nil
}

type (
	jsString []uint16
	member   struct {
		key   jsString
		value any
	}
	jsObject []member
)

func write(b *strings.Builder, v any, numbers bool) error {
	switch v := v.(type) {
	case nil:
		b.WriteString("null")
	case bool:
		b.WriteString(strconv.FormatBool(v))
	case float64:
		if !numbers {
			return ErrNumber
		}
		b.WriteString(Number(v))
	case jsString:
		writeString(b, v)
	case []any:
		b.WriteByte('[')
		for i, item := range v {
			if i > 0 {
				b.WriteByte(',')
			}
			if err := write(b, item, numbers); err != nil {
				return err
			}
		}
		b.WriteByte(']')
	case jsObject:
		sorted := slices.Clone(v)
		slices.SortStableFunc(sorted, func(x, y member) int { return slices.Compare(x.key, y.key) })
		b.WriteByte('{')
		for i, m := range sorted {
			if i > 0 {
				b.WriteByte(',')
			}
			writeString(b, m.key)
			b.WriteByte(':')
			if err := write(b, m.value, numbers); err != nil {
				return err
			}
		}
		b.WriteByte('}')
	}
	return nil
}

// writeString is JSON.stringify's QuoteJSONString.
func writeString(b *strings.Builder, s jsString) {
	b.WriteByte('"')
	for i := 0; i < len(s); i++ {
		u := s[i]
		switch {
		case u == '"':
			b.WriteString(`\"`)
		case u == '\\':
			b.WriteString(`\\`)
		case u == '\b':
			b.WriteString(`\b`)
		case u == '\f':
			b.WriteString(`\f`)
		case u == '\n':
			b.WriteString(`\n`)
		case u == '\r':
			b.WriteString(`\r`)
		case u == '\t':
			b.WriteString(`\t`)
		case u < 0x20:
			b.WriteString(`\u` + hex4(u))
		case utf16.IsSurrogate(rune(u)):
			if u < 0xdc00 && i+1 < len(s) && s[i+1] >= 0xdc00 && s[i+1] <= 0xdfff {
				b.WriteRune(utf16.DecodeRune(rune(u), rune(s[i+1])))
				i++
			} else {
				b.WriteString(`\u` + hex4(u))
			}
		default:
			b.WriteRune(rune(u))
		}
	}
	b.WriteByte('"')
}

func hex4(u uint16) string {
	const digits = "0123456789abcdef"
	return string([]byte{digits[u>>12], digits[u>>8&15], digits[u>>4&15], digits[u&15]})
}

func utf16Units(s string) []uint16 { return utf16.Encode([]rune(s)) }

// Number is ECMAScript's Number::toString (radix 10); null for NaN and the infinities, as
// JSON.stringify writes them.
func Number(x float64) string {
	if math.IsNaN(x) || math.IsInf(x, 0) {
		return "null"
	}
	if x == 0 {
		return "0"
	}
	sign := ""
	if x < 0 {
		sign, x = "-", -x
	}
	// The shortest digits that round-trip, as ECMAScript's are: d.ddde±n.
	mantissa, exp, _ := strings.Cut(strconv.FormatFloat(x, 'e', -1, 64), "e")
	digits := strings.Replace(mantissa, ".", "", 1)
	e, _ := strconv.Atoi(exp)
	k, n := len(digits), e+1
	switch {
	case k <= n && n <= 21:
		return sign + digits + strings.Repeat("0", n-k)
	case 0 < n && n <= 21:
		return sign + digits[:n] + "." + digits[n:]
	case -6 < n && n <= 0:
		return sign + "0." + strings.Repeat("0", -n) + digits
	}
	m := digits
	if k > 1 {
		m = digits[:1] + "." + digits[1:]
	}
	if e >= 0 {
		return sign + m + "e+" + strconv.Itoa(e)
	}
	return sign + m + "e-" + strconv.Itoa(-e)
}

// parser is JSON.parse: strings as UTF-16 code units (a lone surrogate kept), numbers as
// doubles, a repeated key's last value.
type parser struct {
	s     string
	i     int
	depth int
}

// depthMax is the most arrays and objects one value nests.
const depthMax = 512

var (
	errSyntax = errors.New("the text is not JSON")
	errDepth  = errors.New("the JSON nests too deep")
	// numberPattern is JSON's number grammar, matched at the parser's place.
	numberPattern = regexp.MustCompile(`^-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?`)
)

// parse is the one JSON value in text; text after it is refused.
func parse(text string) (any, error) {
	p := &parser{s: text}
	p.space()
	v, err := p.value()
	if err != nil {
		return nil, err
	}
	if p.space(); p.i != len(p.s) {
		return nil, errors.New("text after the JSON value")
	}
	return v, nil
}

// nest is entered for an array or an object; done leaves it.
func (p *parser) nest() error {
	if p.depth++; p.depth > depthMax {
		return errDepth
	}
	return nil
}

func (p *parser) space() {
	for p.i < len(p.s) && strings.IndexByte(" \t\n\r", p.s[p.i]) >= 0 {
		p.i++
	}
}

func (p *parser) value() (any, error) {
	if p.i >= len(p.s) {
		return nil, errSyntax
	}
	switch c := p.s[p.i]; {
	case c == '{' || c == '[':
		if err := p.nest(); err != nil {
			return nil, err
		}
		defer func() { p.depth-- }()
		if c == '{' {
			return p.object()
		}
		return p.array()
	case c == '"':
		return p.str()
	case strings.HasPrefix(p.s[p.i:], "true"):
		p.i += 4
		return true, nil
	case strings.HasPrefix(p.s[p.i:], "false"):
		p.i += 5
		return false, nil
	case strings.HasPrefix(p.s[p.i:], "null"):
		p.i += 4
		return nil, nil
	default:
		return p.number()
	}
}

func (p *parser) object() (any, error) {
	p.i++
	out := jsObject{}
	seen := map[string]int{}
	p.space()
	if p.i < len(p.s) && p.s[p.i] == '}' {
		p.i++
		return out, nil
	}
	for {
		p.space()
		if p.i >= len(p.s) || p.s[p.i] != '"' {
			return nil, errSyntax
		}
		key, err := p.str()
		if err != nil {
			return nil, err
		}
		p.space()
		if p.i >= len(p.s) || p.s[p.i] != ':' {
			return nil, errSyntax
		}
		p.i++
		p.space()
		v, err := p.value()
		if err != nil {
			return nil, err
		}
		k := key.(jsString)
		if at, ok := seen[unitKey(k)]; ok {
			out[at].value = v
		} else {
			seen[unitKey(k)] = len(out)
			out = append(out, member{k, v})
		}
		p.space()
		if p.i >= len(p.s) {
			return nil, errSyntax
		}
		p.i++
		switch p.s[p.i-1] {
		case ',':
		case '}':
			return out, nil
		default:
			return nil, errSyntax
		}
	}
}

func (p *parser) array() (any, error) {
	p.i++
	out := []any{}
	p.space()
	if p.i < len(p.s) && p.s[p.i] == ']' {
		p.i++
		return out, nil
	}
	for {
		p.space()
		v, err := p.value()
		if err != nil {
			return nil, err
		}
		out = append(out, v)
		p.space()
		if p.i >= len(p.s) {
			return nil, errSyntax
		}
		p.i++
		switch p.s[p.i-1] {
		case ',':
		case ']':
			return out, nil
		default:
			return nil, errSyntax
		}
	}
}

func (p *parser) str() (any, error) {
	p.i++
	var out jsString
	for p.i < len(p.s) {
		c := p.s[p.i]
		switch {
		case c == '"':
			p.i++
			return out, nil
		case c == '\\':
			if p.i+1 >= len(p.s) {
				return nil, errSyntax
			}
			e := p.s[p.i+1]
			p.i += 2
			switch e {
			case '"', '\\', '/':
				out = append(out, uint16(e))
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
				if p.i+4 > len(p.s) {
					return nil, errSyntax
				}
				u, err := strconv.ParseUint(p.s[p.i:p.i+4], 16, 16)
				if err != nil {
					return nil, errSyntax
				}
				out = append(out, uint16(u))
				p.i += 4
			default:
				return nil, errSyntax
			}
		case c < 0x20:
			return nil, errSyntax
		default:
			r, size := utf8.DecodeRuneInString(p.s[p.i:])
			out = append(out, utf16.Encode([]rune{r})...)
			p.i += size
		}
	}
	return nil, errSyntax
}

func (p *parser) number() (any, error) {
	text := numberPattern.FindString(p.s[p.i:])
	if text == "" {
		return nil, errSyntax
	}
	p.i += len(text)
	// Past the double's range is an infinity, as JavaScript reads it.
	x, err := strconv.ParseFloat(text, 64)
	if err != nil && !errors.Is(err, strconv.ErrRange) {
		return nil, errSyntax
	}
	return x, nil
}

// unitKey is a key's UTF-16 code units as a map key: a lone surrogate stays distinct.
func unitKey(k jsString) string {
	b := make([]byte, 0, 2*len(k))
	for _, u := range k {
		b = append(b, byte(u>>8), byte(u))
	}
	return string(b)
}

// JSONString is s as JSON.stringify writes it: only what JSON must escape is escaped.
func JSONString(s string) string {
	var b strings.Builder
	writeString(&b, utf16Units(s))
	return b.String()
}
