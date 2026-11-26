package bus

import (
	"encoding/json"
	"os"
	"reflect"
	"strings"
	"testing"
)

// Vectors are backend/tests/fixtures/event-stream-vectors.json, which the backend's
// tests/test_event_stream_vectors.py writes from Python's own readers.
type Vectors struct {
	Kinds      map[string][]map[string]any `json:"kinds"`
	NotifyOnly []string                    `json:"notify_only"`
	Changes    []struct {
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
	if len(v.Kinds) == 0 || len(v.NotifyOnly) == 0 || len(v.Changes) == 0 || len(v.Topics) == 0 || len(v.Reprs) == 0 || len(v.Frames) == 0 {
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

// A NOTIFY-only kind is never in the events log. Its change frame carries no id, so a
// reader's resume point stays on a logged event.
func TestTheNotifyOnlyKindsAreThePythonHubs(t *testing.T) {
	v := ReadVectors(t, VectorsFile)
	want := map[string]bool{}
	for _, k := range v.NotifyOnly {
		want[k] = true
		if _, ok := Kinds[k]; !ok {
			t.Errorf("%s: not a typed kind here", k)
		}
	}
	for k := range Kinds {
		if got := (&Event{Kind: k}).NotifyOnly(); got != want[k] {
			t.Errorf("%s: NotifyOnly %v, want %v", k, got, want[k])
		}
	}
}

// Every change frame is lines of at most 1 MiB (sbfleet's lineMax), and one of them is
// data, so a newline in a payload does not break the frame.
func TestAChangeFrameIsOneDataLine(t *testing.T) {
	check := func(name, frame string) {
		body, ok := strings.CutSuffix(frame, "\n\n")
		if !ok {
			t.Errorf("%s: no blank line ends %q", name, frame)
			return
		}
		data := 0
		for _, line := range strings.Split(body, "\n") {
			if len(line) > 1<<20 {
				t.Errorf("%s: a line of %d bytes", name, len(line))
			}
			switch {
			case strings.HasPrefix(line, "data: "):
				data++
			case strings.HasPrefix(line, "id: "), strings.HasPrefix(line, "event: "):
			default:
				t.Errorf("%s: a stray line %q", name, line)
			}
		}
		if data != 1 {
			t.Errorf("%s: %d data lines", name, data)
		}
	}
	for _, c := range ReadVectors(t, VectorsFile).Changes {
		channel := c.Channel
		if channel == "" {
			channel = "scadbuddy_events"
		}
		check(c.Name, ChangeFrame(channel, c.Payload, ""))
		check(c.Name+" (Python)", c.Want.Live)
		if c.Want.Replay != nil {
			check(c.Name+" (Python replay)", *c.Want.Replay)
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
