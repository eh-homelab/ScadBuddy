package sessions

import (
	"bytes"
	"encoding/json"
	"io"
)

// pythonDumpsLen is the length of Python's json.dumps(value) in bytes: ", " and ": "
// between members, every character past ASCII escaped. A number counts as it was sent.
func pythonDumpsLen(raw json.RawMessage) (int, error) {
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	var v any
	if err := dec.Decode(&v); err != nil {
		return 0, err
	}
	if _, err := dec.Token(); err != io.EOF {
		return 0, err
	}
	return dumpsLen(v), nil
}

func dumpsLen(v any) int {
	switch v := v.(type) {
	case nil:
		return 4
	case bool:
		if v {
			return 4
		}
		return 5
	case json.Number:
		return len(v)
	case string:
		return stringLen(v)
	case []any:
		n := 2
		for i, item := range v {
			if i > 0 {
				n += 2
			}
			n += dumpsLen(item)
		}
		return n
	case map[string]any:
		n, i := 2, 0
		for k, item := range v {
			if i > 0 {
				n += 2
			}
			n += stringLen(k) + 2 + dumpsLen(item)
			i++
		}
		return n
	}
	return 0
}

func stringLen(s string) int {
	n := 2
	for _, c := range s {
		switch {
		case c == '"' || c == '\\' || c == '\n' || c == '\r' || c == '\t' || c == '\b' || c == '\f':
			n += 2
		case c < 0x20:
			n += 6
		case c < 0x80:
			n++
		case c > 0xFFFF:
			n += 12
		default:
			n += 6
		}
	}
	return n
}
