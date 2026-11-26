package store

import (
	"bytes"
	"encoding/json"
	"fmt"
	"math"
	"regexp"
	"slices"
	"sort"
	"strings"
	"unicode/utf8"
)

// A kind's attribute_schema is checked as entities/properties.py check_schema checks it: an
// object schema in an allowed subset of JSON Schema 2020-12, which refuses no key and runs
// no regex (spec 6.3a). The constants are that module's.
const (
	maxString   = 2000
	maxList     = 50
	maxListItem = 500
	draft202012 = "https://json-schema.org/draft/2020-12/schema"
	// entityProblems is entities/problems.py PROBLEMS: bad_property_schema keeps its type.
	entityProblems = "https://scadbuddy.dev/problems/entities/"
)

var (
	propertyKey  = regexp.MustCompile(`^[a-z][a-z0-9_]{0,63}$`)
	topKeywords  = []string{"type", "properties", "additionalProperties", "$schema", "$comment", "title", "description", "examples", "required"}
	keyKeywords  = []string{"type", "title", "description", "$comment", "default", "examples", "deprecated", "readOnly", "writeOnly", "enum", "const", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf", "minLength", "maxLength", "format", "items", "minItems", "maxItems", "uniqueItems"}
	itemKeywords = slices.DeleteFunc(slices.Clone(keyKeywords), func(k string) bool { return k == "items" })
	keyTypes     = []string{"string", "number", "integer", "boolean", "array"}
	itemTypes    = []string{"string"}
	jsonTypes    = []string{"null", "boolean", "object", "array", "number", "integer", "string"}
)

func badSchema(format string, args ...any) error {
	return &Refused{Status: 422, Code: "bad_property_schema", Detail: fmt.Sprintf(format, args...),
		Type: entityProblems + "bad_property_schema"}
}

// decodeJSON is raw with every number as a json.Number.
func decodeJSON(raw []byte) (any, error) {
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	var v any
	if err := dec.Decode(&v); err != nil {
		return nil, err
	}
	return v, nil
}

// checkSchema is check_schema(schema, allow_required=True, defined=...): a kind's schema may
// hold required (spec 6.3b), each key one its chain defines (inherited, the parents' merged
// keys, or its own).
func checkSchema(raw json.RawMessage, inherited map[string]bool) error {
	v, err := decodeJSON(raw)
	if err != nil {
		return badSchema("a schema is a JSON object")
	}
	schema, ok := v.(map[string]any)
	if !ok {
		return badSchema("a schema is a JSON object")
	}
	if hasNUL(schema) {
		return badSchema("a schema may not hold U+0000 in a string or a key")
	}
	if err := keywordsOnly(schema); err != nil {
		return err
	}
	if err := metaschema(schema); err != nil {
		return badSchema("not a valid Draft 2020-12 schema: %s", err.Error())
	}
	if schema["type"] != "object" {
		return badSchema(`a schema's "type" is "object"`)
	}
	if ap, ok := schema["additionalProperties"]; ok && ap != true {
		return badSchema(`"additionalProperties" may only be true`)
	}
	if s, ok := schema["$schema"]; ok && s != draft202012 {
		return badSchema(`"$schema" may only be "%s"`, draft202012)
	}
	props, _ := schema["properties"].(map[string]any)
	for _, key := range sortedKeys(props) {
		if !propertyKey.MatchString(key) {
			return badSchema("property '%s' is no key a value can be set under", key)
		}
		if err := checkKey(props[key], fmt.Sprintf("in property '%s'", key), false); err != nil {
			return err
		}
	}
	if req, ok := schema["required"]; ok {
		list, ok := req.([]any)
		if !ok {
			return badSchema(`"required" is a list of keys`)
		}
		seen := map[string]bool{}
		for _, r := range list {
			key, ok := r.(string)
			if !ok {
				return badSchema(`"required" is a list of keys`)
			}
			if seen[key] {
				return badSchema(`"required" names each key once`)
			}
			seen[key] = true
			if _, own := props[key]; !own && !inherited[key] {
				return badSchema("required key '%s' is not one of the kind's keys", key)
			}
		}
	}
	return nil
}

func sortedKeys[V any](m map[string]V) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

func hasNUL(node any) bool {
	switch n := node.(type) {
	case string:
		return strings.ContainsRune(n, 0)
	case map[string]any:
		for k, v := range n {
			if strings.ContainsRune(k, 0) || hasNUL(v) {
				return true
			}
		}
	case []any:
		for _, v := range n {
			if hasNUL(v) {
				return true
			}
		}
	}
	return false
}

func only(node map[string]any, allowed []string, where string) error {
	for _, k := range sortedKeys(node) {
		if !slices.Contains(allowed, k) {
			return badSchema("keyword '%s' is not allowed %s", k, where)
		}
	}
	return nil
}

// keywordsOnly is _keywords_only: the keyword names alone, before any value is looked at.
func keywordsOnly(schema map[string]any) error {
	if err := only(schema, topKeywords, "at the top level"); err != nil {
		return err
	}
	raw, ok := schema["properties"]
	if !ok {
		return nil
	}
	props, ok := raw.(map[string]any)
	if !ok {
		return badSchema(`"properties" is a JSON object`)
	}
	for _, key := range sortedKeys(props) {
		sub, ok := props[key].(map[string]any)
		if !ok {
			return badSchema("the subschema in property '%s' is a JSON object", key)
		}
		if err := only(sub, keyKeywords, fmt.Sprintf("in property '%s'", key)); err != nil {
			return err
		}
		if items, ok := sub["items"].(map[string]any); ok {
			if err := only(items, itemKeywords, fmt.Sprintf("in the items of property '%s'", key)); err != nil {
				return err
			}
		}
	}
	return nil
}

// metaschema checks each allowed keyword's value as the Draft 2020-12 metaschema does.
// keywordsOnly ran first, so only allowed keywords are here.
func metaschema(schema map[string]any) error {
	for _, k := range sortedKeys(schema) {
		v := schema[k]
		var err error
		switch k {
		case "type":
			err = typeKeyword(v)
		case "properties":
			for _, key := range sortedKeys(v.(map[string]any)) {
				if err = subschema(v.(map[string]any)[key]); err != nil {
					break
				}
			}
		case "additionalProperties":
			if _, ok := v.(map[string]any); !ok {
				if _, ok := v.(bool); !ok {
					err = fmt.Errorf("%s is not of type 'object', 'boolean'", k)
				}
			}
		case "$schema", "$comment", "title", "description":
			err = isString(k, v)
		case "examples":
			err = isArray(k, v)
		case "required":
			err = isArray(k, v)
			if err == nil {
				for _, r := range v.([]any) {
					if _, ok := r.(string); !ok {
						err = fmt.Errorf("required holds a value that is not a string")
					}
				}
			}
		}
		if err != nil {
			return err
		}
	}
	return nil
}

func subschema(v any) error {
	sub, ok := v.(map[string]any)
	if !ok {
		if _, ok := v.(bool); ok {
			return nil
		}
		return fmt.Errorf("a subschema is not of type 'object', 'boolean'")
	}
	for _, k := range sortedKeys(sub) {
		v := sub[k]
		var err error
		switch k {
		case "type":
			err = typeKeyword(v)
		case "title", "description", "$comment", "format":
			err = isString(k, v)
		case "examples", "enum":
			err = isArray(k, v)
		case "deprecated", "readOnly", "writeOnly", "uniqueItems":
			if _, ok := v.(bool); !ok {
				err = fmt.Errorf("%s is not of type 'boolean'", k)
			}
		case "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum":
			if _, ok := number(v); !ok {
				err = fmt.Errorf("%s is not of type 'number'", k)
			}
		case "multipleOf":
			if f, ok := number(v); !ok || f <= 0 {
				err = fmt.Errorf("multipleOf is not a number above 0")
			}
		case "minLength", "maxLength", "minItems", "maxItems":
			if f, ok := number(v); !ok || f < 0 || f != math.Trunc(f) {
				err = fmt.Errorf("%s is not a non-negative integer", k)
			}
		case "items":
			err = subschema(v)
		}
		if err != nil {
			return err
		}
	}
	return nil
}

func typeKeyword(v any) error {
	names := []any{v}
	if list, ok := v.([]any); ok {
		if len(list) == 0 {
			return fmt.Errorf("type is an empty list")
		}
		names = list
	}
	seen := map[string]bool{}
	for _, n := range names {
		s, ok := n.(string)
		if !ok || !slices.Contains(jsonTypes, s) {
			return fmt.Errorf("%v is not valid under any of the given schemas", v)
		}
		if seen[s] {
			return fmt.Errorf("%v has non-unique elements", v)
		}
		seen[s] = true
	}
	return nil
}

func isString(k string, v any) error {
	if _, ok := v.(string); !ok {
		return fmt.Errorf("%s is not of type 'string'", k)
	}
	return nil
}

func isArray(k string, v any) error {
	if _, ok := v.([]any); !ok {
		return fmt.Errorf("%s is not of type 'array'", k)
	}
	return nil
}

// number is v as a float64, when v is a JSON number.
func number(v any) (float64, bool) {
	n, ok := v.(json.Number)
	if !ok {
		return 0, false
	}
	f, err := n.Float64()
	return f, err == nil && !math.IsInf(f, 0)
}

// checkKey is _check_key: a key's (or, with item, its items') subschema is in the subset and
// leaves some value.
func checkKey(v any, where string, item bool) error {
	sub, ok := v.(map[string]any)
	if !ok {
		return badSchema("the subschema %s is a JSON object", where)
	}
	allowed, types, maxStr := keyKeywords, keyTypes, maxString
	if item {
		allowed, types, maxStr = itemKeywords, itemTypes, maxListItem
	}
	if err := only(sub, allowed, where); err != nil {
		return err
	}
	if t, ok := sub["type"]; ok {
		named := []any{t}
		if list, ok := t.([]any); ok {
			named = list
		}
		other, fits := 0, true
		for _, n := range named {
			if n != "null" {
				other++
				fits = fits && slices.Contains(types, n.(string))
			}
		}
		if other == 0 || !fits {
			return badSchema(`the type %s is one of %s, or a list of them and "null"`, where, strings.Join(types, ", "))
		}
	}
	if sub["format"] == "regex" {
		return badSchema("format 'regex' is not allowed %s", where)
	}
	if enum, ok := sub["enum"].([]any); ok {
		some := false
		for _, e := range enum {
			some = some || takes(e, item) && valid(e, sub)
		}
		if !some {
			return badSchema("'enum' %s holds no value a key can take", where)
		}
	}
	if c, ok := sub["const"]; ok && !(takes(c, item) && valid(c, sub)) {
		return badSchema("'const' %s is no value a key can take", where)
	}
	if err := checkBounds(sub, where, maxStr); err != nil {
		return err
	}
	if items, ok := sub["items"]; ok {
		return checkKey(items, "in the items of "+strings.TrimPrefix(where, "in "), true)
	}
	return nil
}

// takes is _takes: whether a key (or, with item, a list's string) can hold v.
func takes(v any, item bool) bool {
	if item {
		s, ok := v.(string)
		return ok && utf8.RuneCountInString(s) <= maxListItem
	}
	switch x := v.(type) {
	case bool:
		return true
	case json.Number:
		_, ok := number(x)
		return ok
	case string:
		return !strings.ContainsRune(x, 0) && utf8.RuneCountInString(x) <= maxString
	case []any:
		if len(x) > maxList {
			return false
		}
		for _, e := range x {
			s, ok := e.(string)
			if !ok || strings.ContainsRune(s, 0) || utf8.RuneCountInString(s) > maxListItem {
				return false
			}
		}
		return true
	}
	return false
}

// checkBounds is _check_bounds: bounds that leave no number, length or count.
func checkBounds(sub map[string]any, where string, maxStr int) error {
	get := func(k string) (float64, bool) {
		v, ok := sub[k]
		if !ok {
			return 0, false
		}
		return number(v)
	}
	low, hasLow := get("minimum")
	high, hasHigh := get("maximum")
	exLow, hasExLow := get("exclusiveMinimum")
	exHigh, hasExHigh := get("exclusiveMaximum")
	if hasLow && hasHigh && low > high || hasExLow && hasHigh && exLow >= high ||
		hasLow && hasExHigh && low >= exHigh || hasExLow && hasExHigh && exLow >= exHigh {
		return badSchema("the number bounds %s leave no number", where)
	}
	minLen, _ := get("minLength")
	maxLen, ok := get("maxLength")
	if !ok || maxLen > float64(maxStr) {
		maxLen = float64(maxStr)
	}
	if minLen > maxLen {
		return badSchema("'minLength' %s passes 'maxLength' or the cap of %d", where, maxStr)
	}
	minItems, _ := get("minItems")
	maxItems, ok := get("maxItems")
	if !ok || maxItems > maxList {
		maxItems = maxList
	}
	if minItems > maxItems {
		return badSchema("'minItems' %s passes 'maxItems' or the cap of %d", where, maxList)
	}
	return nil
}

// valid is whether v fits the subschema sub, for the keywords the subset allows (format is
// an annotation and checks nothing).
func valid(v any, sub map[string]any) bool {
	if t, ok := sub["type"]; ok {
		named := []any{t}
		if list, ok := t.([]any); ok {
			named = list
		}
		fits := false
		for _, n := range named {
			fits = fits || hasJSONType(v, n.(string))
		}
		if !fits {
			return false
		}
	}
	if enum, ok := sub["enum"].([]any); ok {
		if !slices.ContainsFunc(enum, func(e any) bool { return jsonEqual(e, v) }) {
			return false
		}
	}
	if c, ok := sub["const"]; ok && !jsonEqual(c, v) {
		return false
	}
	if f, ok := number(v); ok {
		bound := func(k string) (float64, bool) {
			b, ok := sub[k]
			if !ok {
				return 0, false
			}
			return number(b)
		}
		if b, ok := bound("minimum"); ok && f < b {
			return false
		}
		if b, ok := bound("maximum"); ok && f > b {
			return false
		}
		if b, ok := bound("exclusiveMinimum"); ok && f <= b {
			return false
		}
		if b, ok := bound("exclusiveMaximum"); ok && f >= b {
			return false
		}
		if b, ok := bound("multipleOf"); ok {
			if q := f / b; q != math.Trunc(q) {
				return false
			}
		}
	}
	limit := func(k string) (float64, bool) {
		b, ok := sub[k]
		if !ok {
			return 0, false
		}
		return number(b)
	}
	if s, ok := v.(string); ok {
		n := float64(utf8.RuneCountInString(s))
		if b, ok := limit("minLength"); ok && n < b {
			return false
		}
		if b, ok := limit("maxLength"); ok && n > b {
			return false
		}
	}
	if list, ok := v.([]any); ok {
		n := float64(len(list))
		if b, ok := limit("minItems"); ok && n < b {
			return false
		}
		if b, ok := limit("maxItems"); ok && n > b {
			return false
		}
		if sub["uniqueItems"] == true {
			for i := range list {
				for j := i + 1; j < len(list); j++ {
					if jsonEqual(list[i], list[j]) {
						return false
					}
				}
			}
		}
		if items, ok := sub["items"].(map[string]any); ok {
			for _, e := range list {
				if !valid(e, items) {
					return false
				}
			}
		}
	}
	return true
}

func hasJSONType(v any, t string) bool {
	switch t {
	case "null":
		return v == nil
	case "boolean":
		_, ok := v.(bool)
		return ok
	case "object":
		_, ok := v.(map[string]any)
		return ok
	case "array":
		_, ok := v.([]any)
		return ok
	case "string":
		_, ok := v.(string)
		return ok
	case "number":
		_, ok := number(v)
		return ok
	case "integer":
		f, ok := number(v)
		return ok && f == math.Trunc(f)
	}
	return false
}

// jsonEqual is JSON Schema's equality: numbers by value, a boolean never a number.
func jsonEqual(a, b any) bool {
	if fa, ok := number(a); ok {
		fb, ok := number(b)
		return ok && fa == fb
	}
	switch x := a.(type) {
	case []any:
		y, ok := b.([]any)
		if !ok || len(x) != len(y) {
			return false
		}
		for i := range x {
			if !jsonEqual(x[i], y[i]) {
				return false
			}
		}
		return true
	case map[string]any:
		y, ok := b.(map[string]any)
		if !ok || len(x) != len(y) {
			return false
		}
		for k, v := range x {
			w, ok := y[k]
			if !ok || !jsonEqual(v, w) {
				return false
			}
		}
		return true
	}
	return a == b
}
