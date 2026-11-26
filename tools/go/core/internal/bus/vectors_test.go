package bus

import (
	"encoding/json"
	"os"
	"reflect"
	"testing"
)

// Vectors are backend/tests/fixtures/event-stream-vectors.json, which the backend's
// tests/test_event_stream_vectors.py writes from Python's own readers.
type Vectors struct {
	Kinds   map[string][]map[string]any `json:"kinds"`
	Changes []struct {
		Name    string  `json:"name"`
		Channel string  `json:"channel"`
		Payload string  `json:"payload"`
		Stored  *string `json:"stored"`
		Want    struct {
			Decodes bool            `json:"decodes"`
			Live    string          `json:"live"`
			Keep    map[string]bool `json:"keep"`
			Replay  *string         `json:"replay"`
			Topics  []string        `json:"topics"`
			WS      *string         `json:"ws"`
		} `json:"want"`
	} `json:"changes"`
	Topics []struct {
		Topic string `json:"topic"`
		Valid bool   `json:"valid"`
	} `json:"topics"`
	Reprs []struct {
		JSON string `json:"json"`
		Repr string `json:"repr"`
	} `json:"reprs"`
	Frames []struct {
		Kind  string         `json:"kind"`
		Data  map[string]any `json:"data"`
		ID    string         `json:"id"`
		Bytes string         `json:"bytes"`
	} `json:"frames"`
}

// VectorsFile is the shared file, from a package two levels under core.
const VectorsFile = "../../../../../backend/tests/fixtures/event-stream-vectors.json"

func ReadVectors(t *testing.T, path string) Vectors {
	t.Helper()
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var v Vectors
	if err := json.Unmarshal(raw, &v); err != nil {
		t.Fatal(err)
	}
	if len(v.Kinds) == 0 || len(v.Changes) == 0 || len(v.Topics) == 0 || len(v.Reprs) == 0 || len(v.Frames) == 0 {
		t.Fatalf("%s holds no vectors of some kind", path)
	}
	return v
}

func TestTheKindsAreThePythonModels(t *testing.T) {
	v := ReadVectors(t, VectorsFile)
	if len(v.Kinds) != len(Kinds) {
		t.Errorf("%d kinds, Python has %d", len(Kinds), len(v.Kinds))
	}
	for kind, want := range v.Kinds {
		if len(want) < 3 || want[0]["name"] != "id" || want[1]["name"] != "at" || want[2]["name"] != "kind" {
			t.Fatalf("%s: %v", kind, want)
		}
		var got []map[string]any
		for _, f := range Kinds[kind] {
			m := map[string]any{"name": f.Name, "type": f.Type, "nullable": f.Nullable, "required": f.Required}
			if len(f.Choices) > 0 {
				choices := []any{}
				for _, c := range f.Choices {
					choices = append(choices, c)
				}
				m["choices"] = choices
			}
			if !f.Required {
				var zero any
				if f.Type == "bool" {
					zero = false
				}
				m["default"] = zero
			}
			got = append(got, m)
		}
		if !reflect.DeepEqual(got, want[3:]) {
			t.Errorf("%s:\n got %v\nwant %v", kind, got, want[3:])
		}
	}
}

func TestAnEventIsReadAndWrittenAsPythonDoes(t *testing.T) {
	for _, c := range ReadVectors(t, VectorsFile).Changes {
		if c.Channel != "" && c.Channel != "scadbuddy_events" {
			continue
		}
		e, err := Decode(c.Payload)
		if (err == nil) != c.Want.Decodes {
			t.Errorf("%s: decodes %v, want %v", c.Name, err == nil, c.Want.Decodes)
			continue
		}
		id := ""
		if e != nil && !e.NotifyOnly() {
			id = e.ID
		}
		if got := ChangeFrame("scadbuddy_events", c.Payload, id); got != c.Want.Live {
			t.Errorf("%s: live\n got %q\nwant %q", c.Name, got, c.Want.Live)
		}
		var replay *string
		if c.Stored != nil {
			if r, err := Decode(*c.Stored); err == nil {
				s := ChangeFrame("scadbuddy_events", r.Encoded(), r.ID)
				replay = &s
			}
		}
		if !reflect.DeepEqual(replay, c.Want.Replay) {
			t.Errorf("%s: replay\n got %v\nwant %v", c.Name, deref(replay), deref(c.Want.Replay))
		}
		if e == nil {
			continue
		}
		topics := e.Topics()
		if len(topics) != len(c.Want.Topics) || len(topics) > 0 && !reflect.DeepEqual(topics, c.Want.Topics) {
			t.Errorf("%s: topics %q, want %q", c.Name, topics, c.Want.Topics)
		}
		var ws *string
		if len(topics) > 0 {
			s := e.Frame(topics)
			ws = &s
		}
		if !reflect.DeepEqual(ws, c.Want.WS) {
			t.Errorf("%s: ws\n got %v\nwant %v", c.Name, deref(ws), deref(c.Want.WS))
		}
	}
}

func deref(s *string) string {
	if s == nil {
		return "<none>"
	}
	return *s
}

func TestAValueIsNamedAsPythonsReprNamesIt(t *testing.T) {
	for _, c := range ReadVectors(t, VectorsFile).Reprs {
		v, err := ParseJSON(c.JSON)
		if err != nil {
			t.Fatal(err)
		}
		if got := Repr(v); got != c.Repr {
			t.Errorf("%s:\n got %s\nwant %s", c.JSON, got, c.Repr)
		}
	}
}

func TestAStreamFrameIsWrittenAsPythonWritesIt(t *testing.T) {
	for _, c := range ReadVectors(t, VectorsFile).Frames {
		data := Object()
		for _, key := range []string{"resync", "reconnect"} {
			if b, ok := c.Data[key].(bool); ok {
				data = Object(append(pairs(data), key, b)...)
			}
		}
		if got := SSE(c.Kind, data, c.ID); got != c.Bytes {
			t.Errorf("%s: %q, want %q", c.Kind, got, c.Bytes)
		}
	}
}

func pairs(v Value) []any {
	var out []any
	for i, k := range v.Keys {
		out = append(out, k, v.Vals[i])
	}
	return out
}
