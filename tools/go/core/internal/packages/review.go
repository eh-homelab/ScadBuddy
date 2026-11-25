package packages

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"math"
	"strconv"
	"strings"
	"unicode/utf16"

	"github.com/jackc/pgx/v5"
)

// forgetPackage is agent_data/packages.py forget_package: every ai_access and
// ai_tool_listings row of package:<name> and its servers. A server's source is matched with
// starts_with, never LIKE: a package's name may hold _.
func forgetPackage(ctx context.Context, tx pgx.Tx, name string) error {
	source, prefix := "package:"+name, "package:"+name+"/mcp:"
	if _, err := tx.Exec(ctx, `DELETE FROM ai_access WHERE source = $1 OR starts_with(source, $2)`,
		source, prefix); err != nil {
		return err
	}
	_, err := tx.Exec(ctx, `DELETE FROM ai_tool_listings WHERE source = $1 OR starts_with(source, $2)`,
		source, prefix)
	return err
}

// itemKind is access/items.ts's kind of an item, by the prefix of its name. A package's
// own source always holds items (holds_items).
func itemKind(item string) string {
	for _, k := range []string{"skill", "subagent", "builtin", "hook"} {
		if strings.HasPrefix(item, k+":") {
			return k
		}
	}
	return "tool"
}

// reviewOf is the members of a review prune_stale_rows reads. A member of another shape
// fails, as Python's iteration of it would.
type reviewOf map[string]json.RawMessage

func (r reviewOf) has(key string) bool { _, ok := r[key]; return ok }

// list is the member key as a list of T; nil when it is absent, or when it is null and
// nullOK.
func list[T any](r reviewOf, key string, nullOK bool) ([]T, error) {
	raw, ok := r[key]
	if !ok || (nullOK && string(raw) == "null") {
		return nil, nil
	}
	var out []T
	if err := json.Unmarshal(raw, &out); err != nil || out == nil {
		return nil, fmt.Errorf("the review's %s is not a list of the kind it holds", key)
	}
	return out, nil
}

// pruneStaleRows is packages.py prune_stale_rows (store.ts pruneStaleRows): it deletes what
// package:<name> stores for items the review (the pin current after the write) no longer
// has, and the rows and listings of the servers it no longer declares. What the review
// cannot tell is kept. It answers whether it deleted anything.
func pruneStaleRows(ctx context.Context, tx pgx.Tx, name string, raw json.RawMessage) (bool, error) {
	var review reviewOf
	if err := json.Unmarshal(raw, &review); err != nil {
		return false, err
	}
	deleted := false
	hooks, err := list[map[string]json.RawMessage](review, "hooks", false)
	if err != nil {
		return false, err
	}
	namesItems := review.has("described")
	for _, h := range hooks {
		if _, ok := h["id"]; !ok {
			namesItems = false
		}
	}
	if namesItems {
		source, prefix := "package:"+name, name+":"
		bare := func(q string) string { return strings.TrimPrefix(q, prefix) }
		kept := map[string]bool{}
		for _, key := range []string{"skills", "commands", "agents"} {
			names, err := list[string](review, key, false)
			if err != nil {
				return false, err
			}
			for _, n := range names {
				if key == "agents" {
					kept["subagent:"+bare(n)] = true
				} else {
					kept["skill:"+bare(n)] = true
				}
			}
		}
		builtins, err := list[json.RawMessage](review, "builtin_tools", true)
		if err != nil {
			return false, err
		}
		for _, b := range builtins {
			kept["builtin:"+pyStr(b)] = true
		}
		hookIDs := map[string]bool{}
		for _, h := range hooks {
			var id string
			if json.Unmarshal(h["id"], &id) == nil {
				hookIDs[id] = true
			}
		}
		stale := func(item string) bool {
			switch itemKind(item) {
			case "tool":
				return false
			case "hook":
				return !hookIDs[item[strings.LastIndexByte(item, ':')+1:]]
			case "builtin":
				if !review.has("builtin_tools") {
					return false
				}
			}
			return !kept[item]
		}
		rows, err := tx.Query(ctx, `SELECT item FROM ai_access WHERE source = $1`, source)
		if err != nil {
			return false, err
		}
		items, err := pgx.CollectRows(rows, pgx.RowTo[string])
		if err != nil {
			return false, err
		}
		var gone []string
		for _, i := range items {
			if stale(i) {
				gone = append(gone, i)
			}
		}
		if len(gone) > 0 {
			if _, err := tx.Exec(ctx, `DELETE FROM ai_access WHERE source = $1 AND item = ANY($2)`,
				source, gone); err != nil {
				return false, err
			}
			deleted = true
		}
	}

	local, err := list[string](review, "local_servers", true)
	if err != nil || local == nil {
		return deleted, err
	}
	servers, err := list[map[string]json.RawMessage](review, "mcp_servers", false)
	if err != nil {
		return false, err
	}
	declared := map[string]bool{}
	for _, s := range servers {
		var n string
		if err := json.Unmarshal(s["name"], &n); err != nil {
			return false, fmt.Errorf("a server of the review has no name")
		}
		declared[n] = true
	}
	for _, n := range local {
		declared[n] = true
	}
	prefix := "package:" + name + "/mcp:"
	rows, err := tx.Query(ctx, `SELECT source FROM ai_access WHERE starts_with(source, $1)
		UNION SELECT source FROM ai_tool_listings WHERE starts_with(source, $1)`, prefix)
	if err != nil {
		return false, err
	}
	sources, err := pgx.CollectRows(rows, pgx.RowTo[string])
	if err != nil {
		return false, err
	}
	var gone []string
	for _, s := range sources {
		if !declared[s[len(prefix):]] {
			gone = append(gone, s)
		}
	}
	if len(gone) > 0 {
		if _, err := tx.Exec(ctx, `DELETE FROM ai_access WHERE source = ANY($1)`, gone); err != nil {
			return false, err
		}
		if _, err := tx.Exec(ctx, `DELETE FROM ai_tool_listings WHERE source = ANY($1)`, gone); err != nil {
			return false, err
		}
		deleted = true
	}
	return deleted, nil
}

// pyStr is a JSON value as Python's str() writes what json.loads made of it; an object or
// list is written as compact JSON, where Python writes its repr.
func pyStr(raw json.RawMessage) string {
	raw = bytes.TrimSpace(raw)
	var s string
	switch {
	case json.Unmarshal(raw, &s) == nil:
		return s
	case string(raw) == "null":
		return "None"
	case string(raw) == "true":
		return "True"
	case string(raw) == "false":
		return "False"
	}
	if n, ok := pyNumber(string(raw)); ok {
		return n
	}
	var b bytes.Buffer
	_ = json.Compact(&b, raw)
	return b.String()
}

// pyNumber is a JSON number as Python writes it back: an integer as itself, any other as
// float's repr.
func pyNumber(text string) (string, bool) {
	if text == "" || !strings.ContainsAny(text[len(text)-1:], "0123456789") {
		return "", false
	}
	if !strings.ContainsAny(text, ".eE") {
		if _, err := strconv.ParseFloat(text, 64); err != nil {
			return "", false
		}
		n := strings.TrimPrefix(text, "-")
		if n == "0" {
			return "0", true
		}
		return text, true
	}
	f, err := strconv.ParseFloat(text, 64)
	if err != nil && !math.IsInf(f, 0) {
		return "", false
	}
	switch {
	case math.IsInf(f, 1):
		return "Infinity", true
	case math.IsInf(f, -1):
		return "-Infinity", true
	}
	return floatRepr(f), true
}

// floatRepr is Python's repr of a float: the shortest digits, fixed between 1e-4 and 1e16.
func floatRepr(f float64) string {
	if f == 0 {
		if math.Signbit(f) {
			return "-0.0"
		}
		return "0.0"
	}
	mantissa, e, _ := strings.Cut(strconv.FormatFloat(f, 'e', -1, 64), "e")
	n, _ := strconv.Atoi(e)
	if n < -4 || n >= 16 {
		sign := "+"
		if n < 0 {
			sign, n = "-", -n
		}
		return fmt.Sprintf("%se%s%02d", mantissa, sign, n)
	}
	s := strconv.FormatFloat(f, 'f', -1, 64)
	if !strings.Contains(s, ".") {
		s += ".0"
	}
	return s
}

// pyDumpsLen is len(json.dumps(value)) of what json.loads made of raw: ASCII only, with
// ", " and ": " between members.
func pyDumpsLen(raw json.RawMessage) int {
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	var v any
	if dec.Decode(&v) != nil {
		return 0
	}
	return dumpsLen(v)
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
		n, _ := pyNumber(string(v))
		return len(n)
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
		for key, item := range v {
			if i > 0 {
				n += 2
			}
			n += stringLen(key) + 2 + dumpsLen(item)
			i++
		}
		return n
	}
	return 0
}

// stringLen is the length of s as json.dumps writes it, ensure_ascii on.
func stringLen(s string) int {
	n := 2
	for _, r := range s {
		switch {
		case r == '"' || r == '\\' || r == '\n' || r == '\r' || r == '\t' || r == '\b' || r == '\f':
			n += 2
		case r < 0x20:
			n += 6
		case r < 0x80:
			n++
		default:
			n += 6 * len(utf16.Encode([]rune{r}))
		}
	}
	return n
}
