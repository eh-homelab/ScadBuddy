package gate

import "encoding/json"

// EntryOf is the validator's entry of an ai_pending_input row and its gate record (the
// agent's GateRecord, camelCase), in state "resolving" once the row is past its timer, as
// agent_data/gate.py gate_entry. record is JSON decoded with UseNumber.
func EntryOf(requestID, kind string, expired bool, record map[string]any) *Entry {
	e := &Entry{
		ID:             requestID,
		Kind:           kind,
		State:          "pending",
		RequestedBy:    who(record["requestedBy"]),
		SessionOwner:   who(record["sessionOwner"]),
		SessionCreator: who(record["sessionCreator"]),
		InRun:          truthy(record["inRun"]),
	}
	if expired {
		e.State = "resolving"
	}
	if h, ok := record["inputHash"].(string); ok {
		e.InputHash = &h
	}
	if questions, ok := record["questions"].([]any); ok {
		e.Questions = []Question{}
		for _, q := range questions {
			m, _ := q.(map[string]any)
			e.Questions = append(e.Questions, Question{pyStr(m["question"]), truthy(m["multiSelect"])})
		}
	}
	if options, ok := record["options"].([]any); ok {
		e.Options = []string{}
		for _, o := range options {
			e.Options = append(e.Options, pyStr(o))
		}
	}
	return e
}

func who(v any) *Principal {
	m, ok := v.(map[string]any)
	if !ok {
		return nil
	}
	return &Principal{pyStr(m["kind"]), pyStr(m["id"])}
}

// pyStr is Python's str() of a JSON value.
func pyStr(v any) string {
	switch v := v.(type) {
	case nil:
		return "None"
	case string:
		return v
	case bool:
		if v {
			return "True"
		}
		return "False"
	case json.Number:
		return pyNumber(string(v))
	}
	return dumps(v)
}

// truthy is Python's bool() of a JSON value.
func truthy(v any) bool {
	switch v := v.(type) {
	case nil:
		return false
	case bool:
		return v
	case string:
		return v != ""
	case json.Number:
		f, _ := v.Float64()
		return f != 0
	case []any:
		return len(v) > 0
	case map[string]any:
		return len(v) > 0
	}
	return true
}
