package secrets

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"net/http"
	"regexp"
	"strconv"
	"strings"

	"github.com/jackc/pgx/v5"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
)

// The agent's ai_settings: agent_data/settings.py and the settings routes of
// api/crud_agent_secrets.py (removed in #7667).

const (
	settingsMax     = 50
	settingValueMax = 256 * 1024
	// warmEvictChannel and settingsChannel are what the agent's caches LISTEN on.
	warmEvictChannel = "scadbuddy_warm_evict"
	settingsChannel  = "scadbuddy_settings"
	runtimePayload   = "session-runtime"
)

// settingKey is a settings key: printable ASCII, no space.
var settingKey = regexp.MustCompile(`^[\x21-\x7e]{1,200}$`)

func (s *Store) readSettings(r *http.Request, _ crud.Params) (any, error) {
	keys := r.URL.Query()["key"]
	switch {
	case len(keys) == 0:
		return nil, httpx.Invalid(httpx.Issue{Loc: []string{"query", "key"}, Msg: "Field required"})
	case len(keys) > settingsMax:
		return nil, httpx.Invalid(httpx.Issue{Loc: []string{"query", "key"},
			Msg: fmt.Sprintf("List should have at most %d items after validation", settingsMax)})
	}
	for _, k := range keys {
		if !settingKey.MatchString(k) {
			return nil, &httpx.Problem{Status: http.StatusUnprocessableEntity,
				Detail: "a settings key is printable ASCII with no space, 1 to 200 characters"}
		}
	}
	values := map[string]json.RawMessage{}
	rows, err := s.Pool.Query(r.Context(), `SELECT key, value::text FROM ai_settings WHERE key = ANY($1)`, keys)
	if err != nil {
		return nil, err
	}
	var key, value string
	_, err = pgx.ForEachRow(rows, []any{&key, &value}, func() error {
		var b bytes.Buffer
		if err := json.Compact(&b, []byte(value)); err != nil {
			return err
		}
		values[key] = b.Bytes()
		return nil
	})
	if err != nil {
		return nil, err
	}
	return struct {
		Values map[string]json.RawMessage `json:"values"`
	}{values}, nil
}

type expected struct {
	key     string
	present bool
	value   json.RawMessage
}

func (s *Store) writeSettings(r *http.Request, _ crud.Params) (any, error) {
	f, err := readForm(r, "values", "expect", "notify")
	if err != nil {
		return nil, err
	}
	type pair struct {
		key   string
		value json.RawMessage
	}
	var values []pair
	if raw, ok := f.required("values"); ok {
		object, err := orderedObject(raw)
		switch {
		case err != nil:
			f.fail([]string{"values"}, "Input should be a valid dictionary")
		case len(object) < 1:
			f.fail([]string{"values"}, "Dictionary should have at least 1 item after validation")
		case len(object) > settingsMax:
			f.fail([]string{"values"},
				fmt.Sprintf("Dictionary should have at most %d items after validation", settingsMax))
		default:
			for _, m := range object {
				if !settingKey.MatchString(m.key) {
					f.fail([]string{"values", m.key, "[key]"}, "String should match pattern")
				}
				values = append(values, pair{m.key, m.value})
			}
			for _, m := range object {
				if n := pyDumpsLen(m.value); n > settingValueMax {
					f.fail([]string{"values"}, fmt.Sprintf(
						"Value error, the value of %s is over %d bytes as JSON", m.key, settingValueMax))
					break
				}
			}
		}
	}
	var expect []expected
	given := f.raw("expect") != nil
	if given {
		var items []json.RawMessage
		if json.Unmarshal(f.raw("expect"), &items) != nil {
			f.fail([]string{"expect"}, "Input should be a valid list")
		} else if len(items) > settingsMax {
			f.fail([]string{"expect"},
				fmt.Sprintf("List should have at most %d items after validation", settingsMax))
		}
		for i, item := range items {
			e := &form{at: []string{"body", "expect", strconv.Itoa(i)}, issues: f.issues}
			if !e.object(item, []string{"key", "present", "value"}) {
				continue
			}
			key := e.needText("key", 0, math.MaxInt, settingKey, false)
			got := expected{present: e.needBool("present"), value: e.fields["value"]}
			if got.value == nil {
				got.value = json.RawMessage("null")
			}
			if key != nil {
				got.key = *key
			}
			if pyDumpsLen(got.value) > settingValueMax {
				name := got.key
				if key == nil {
					name = "expect"
				}
				e.fail([]string{"value"}, fmt.Sprintf(
					"Value error, the value of %s is over %d bytes as JSON", name, settingValueMax))
			}
			expect = append(expect, got)
		}
	}
	var warmEvict, runtime bool
	if n := f.nested("notify", "warm_evict", "runtime"); n != nil {
		warmEvict, runtime = n.boolean("warm_evict", false), n.boolean("runtime", false)
	}
	if err := f.err(); err != nil {
		return nil, err
	}
	written := true
	err = pgx.BeginFunc(r.Context(), s.Pool, func(tx pgx.Tx) error {
		ctx := r.Context()
		if given {
			if _, err := tx.Exec(ctx, `LOCK TABLE ai_settings IN SHARE ROW EXCLUSIVE MODE`); err != nil {
				return err
			}
			for _, e := range expect {
				var same bool
				err := tx.QueryRow(ctx, `SELECT value = $1::text::jsonb AS same FROM ai_settings WHERE key = $2`,
					string(e.value), e.key).Scan(&same)
				if errors.Is(err, pgx.ErrNoRows) {
					same = !e.present
				} else if err != nil {
					return err
				} else {
					same = e.present && same
				}
				if !same {
					written = false
					return nil
				}
			}
		}
		for _, v := range values {
			if _, err := tx.Exec(ctx, `INSERT INTO ai_settings (key, value) VALUES ($1, $2::text::jsonb)
				ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()`,
				v.key, string(v.value)); err != nil {
				return err
			}
		}
		if warmEvict {
			if _, err := tx.Exec(ctx, `SELECT pg_notify($1, '*')`, warmEvictChannel); err != nil {
				return err
			}
		}
		if runtime {
			if _, err := tx.Exec(ctx, `SELECT pg_notify($1, $2)`, settingsChannel, runtimePayload); err != nil {
				return err
			}
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	return struct {
		Written bool `json:"written"`
	}{written}, nil
}

type member struct {
	key   string
	value json.RawMessage
}

// orderedObject is a JSON object's members in order, a repeated key's last value in its
// first place, as Python's dict.
func orderedObject(raw json.RawMessage) ([]member, error) {
	dec := json.NewDecoder(bytes.NewReader(raw))
	if t, err := dec.Token(); err != nil || t != json.Delim('{') {
		return nil, errors.New("not an object")
	}
	var out []member
	at := map[string]int{}
	for dec.More() {
		t, err := dec.Token()
		if err != nil {
			return nil, err
		}
		key := t.(string)
		var value json.RawMessage
		if err := dec.Decode(&value); err != nil {
			return nil, err
		}
		if i, ok := at[key]; ok {
			out[i].value = value
		} else {
			at[key] = len(out)
			out = append(out, member{key, value})
		}
	}
	return out, nil
}

// pyDumpsLen is len(json.dumps(value)) in Python, for the JSON text raw: ASCII escapes, ", "
// and ": " between members, a float as repr writes it.
func pyDumpsLen(raw json.RawMessage) int {
	raw = bytes.TrimSpace(raw)
	switch {
	case len(raw) == 0:
		return 0
	case raw[0] == 't' || raw[0] == 'f' || raw[0] == 'n':
		return len(raw)
	case raw[0] == '"':
		var s string
		_ = json.Unmarshal(raw, &s)
		return pyStringLen(s)
	case raw[0] == '[':
		var items []json.RawMessage
		_ = json.Unmarshal(raw, &items)
		n := 2
		for i, item := range items {
			if i > 0 {
				n += 2
			}
			n += pyDumpsLen(item)
		}
		return n
	case raw[0] == '{':
		members, _ := orderedObject(raw)
		n := 2
		for i, m := range members {
			if i > 0 {
				n += 2
			}
			n += pyStringLen(m.key) + 2 + pyDumpsLen(m.value)
		}
		return n
	case bytes.ContainsAny(raw, ".eE"):
		x, err := strconv.ParseFloat(string(raw), 64)
		if err != nil {
			return len("Infinity") + strings.Count(string(raw[:1]), "-")
		}
		return len(pyFloat(x))
	}
	return len(raw)
}

// pyFloat is Python's repr of a float.
func pyFloat(x float64) string {
	if x == 0 {
		return strconv.FormatFloat(x, 'f', 1, 64)
	}
	e := strconv.FormatFloat(x, 'e', -1, 64)
	exp, _ := strconv.Atoi(e[strings.IndexByte(e, 'e')+1:])
	if exp >= -4 && exp < 16 {
		f := strconv.FormatFloat(x, 'f', -1, 64)
		if !strings.Contains(f, ".") {
			f += ".0"
		}
		return f
	}
	mantissa := e[:strings.IndexByte(e, 'e')]
	sign := "+"
	if exp < 0 {
		sign, exp = "-", -exp
	}
	return fmt.Sprintf("%se%s%02d", mantissa, sign, exp)
}

func pyStringLen(s string) int {
	n := 2
	for _, r := range s {
		switch {
		case r == '"' || r == '\\' || r == '\n' || r == '\r' || r == '\t' || r == '\b' || r == '\f':
			n += 2
		case r < 0x20 || (r > 0x7f && r <= 0xffff):
			n += 6
		case r > 0xffff:
			n += 12
		default:
			n++
		}
	}
	return n
}
