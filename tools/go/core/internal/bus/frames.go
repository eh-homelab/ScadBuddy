package bus

import (
	"encoding/json"
	"strings"
)

// Keepalive is the change streams' comment line (agent_changes/hub.py frames).
const Keepalive = ": keepalive\n\n"

// SSE is one server-sent event as agent_changes/hub.py frame writes it: an id line when id
// is not "", the event's name, and its data as one line of json.dumps (compact, with
// ensure_ascii), so no newline in a payload can end it early.
func SSE(name string, data Value, id string) string {
	var b strings.Builder
	if id != "" {
		b.WriteString("id: " + id + "\n")
	}
	b.WriteString("event: " + name + "\ndata: " + Dumps(data, true, true) + "\n\n")
	return b.String()
}

// Object is a JSON object of the members given in order: string, bool and Value members.
func Object(members ...any) Value {
	v := Value{Kind: 'o', Keys: []string{}, Vals: []Value{}}
	for i := 0; i+1 < len(members); i += 2 {
		var member Value
		switch x := members[i+1].(type) {
		case string:
			member = Value{Kind: 's', Str: x}
		case bool:
			member = Value{Kind: 'b', Bool: x}
		case Value:
			member = x
		}
		v.Keys, v.Vals = append(v.Keys, members[i].(string)), append(v.Vals, member)
	}
	return v
}

// Strings is a JSON list of strings.
func Strings(items []string) Value {
	v := Value{Kind: 'a', Items: []Value{}}
	for _, s := range items {
		v.Items = append(v.Items, Value{Kind: 's', Str: s})
	}
	return v
}

// ChangeFrame is a change stream's change frame: the channel and the payload as heard, with
// id the logged event's id ("" for none).
func ChangeFrame(channel, payload, id string) string {
	return SSE("change", Object("c", channel, "p", payload), id)
}

// FromGo is v, a value encoding/json can write, as a Value.
func FromGo(v any) Value {
	raw, _ := json.Marshal(v)
	out, _ := ParseJSON(string(raw))
	return out
}
