package gate

import (
	"bytes"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"runtime"
	"strings"
	"testing"
)

func decode(t *testing.T, raw []byte) any {
	t.Helper()
	d := json.NewDecoder(bytes.NewReader(raw))
	d.UseNumber()
	var v any
	if err := d.Decode(&v); err != nil {
		t.Fatal(err)
	}
	return v
}

func vectors(t *testing.T) map[string]any {
	t.Helper()
	_, file, _, _ := runtime.Caller(0)
	raw, err := os.ReadFile(filepath.Join(filepath.Dir(file),
		"../../../../../backend/tests/fixtures/pending-input-vectors.json"))
	if err != nil {
		t.Fatal(err)
	}
	return decode(t, raw).(map[string]any)
}

func number(t *testing.T, v any) int {
	t.Helper()
	n, err := v.(json.Number).Int64()
	if err != nil {
		t.Fatal(err)
	}
	return int(n)
}

// expand is {"$repeat": s, "times": n}: s repeated n + 1 times, n a number or a top-level name.
func expand(t *testing.T, doc map[string]any, v any) any {
	switch v := v.(type) {
	case []any:
		out := make([]any, len(v))
		for i, item := range v {
			out[i] = expand(t, doc, item)
		}
		return out
	case map[string]any:
		if s, ok := v["$repeat"].(string); ok {
			times := v["times"]
			if name, ok := times.(string); ok {
				times = doc[name]
			}
			return strings.Repeat(s, number(t, times)+1)
		}
		out := map[string]any{}
		for k, item := range v {
			out[k] = expand(t, doc, item)
		}
		return out
	}
	return v
}

func principal(v any) *Principal {
	m, ok := v.(map[string]any)
	if !ok {
		return nil
	}
	return &Principal{m["kind"].(string), m["id"].(string)}
}

func entryOf(v any) *Entry {
	m, ok := v.(map[string]any)
	if !ok {
		return nil
	}
	e := &Entry{
		ID: m["id"].(string), Kind: m["kind"].(string), State: m["state"].(string),
		RequestedBy: principal(m["requested_by"]), SessionOwner: principal(m["session_owner"]),
		SessionCreator: principal(m["session_creator"]),
	}
	if h, ok := m["input_hash"].(string); ok {
		e.InputHash = &h
	}
	if qs, ok := m["questions"].([]any); ok {
		e.Questions = []Question{}
		for _, q := range qs {
			q := q.(map[string]any)
			e.Questions = append(e.Questions, Question{q["question"].(string), q["multi_select"].(bool)})
		}
	}
	if os, ok := m["options"].([]any); ok {
		e.Options = []string{}
		for _, o := range os {
			e.Options = append(e.Options, o.(string))
		}
	}
	e.InRun, _ = m["in_run"].(bool)
	return e
}

func TestTheFileCarriesThisCopysBounds(t *testing.T) {
	doc := vectors(t)
	if number(t, doc["response_max"]) != ResponseMax || number(t, doc["answer_max"]) != AnswerMax {
		t.Fatal(doc["response_max"], doc["answer_max"])
	}
}

func TestEveryVector(t *testing.T) {
	doc := vectors(t)
	list := doc["vectors"].([]any)
	if len(list) == 0 {
		t.Fatal("no vectors")
	}
	for _, item := range list {
		v := item.(map[string]any)
		t.Run(v["name"].(string), func(t *testing.T) {
			r := v["request"].(map[string]any)
			request := Request{
				RequestID: r["request_id"].(string),
				Response:  expand(t, doc, r["response"]),
				Responder: *principal(r["responder"]),
				Role:      r["role"].(string),
			}
			expect := v["expect"].(map[string]any)
			valid, err := Validate(entryOf(v["entry"]), request)
			if code, ok := expect["refused"]; ok {
				var refused *Refused
				if !errors.As(err, &refused) || refused.Code != code {
					t.Fatalf("refused %v, want %v", err, code)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			if d, ok := expect["decision"]; ok && valid.Decision != d {
				t.Errorf("decision %q, want %v", valid.Decision, d)
			}
			if a, ok := expect["answers"]; ok {
				want := []string{}
				for _, s := range a.([]any) {
					want = append(want, s.(string))
				}
				if !reflect.DeepEqual(valid.Answers, want) {
					t.Errorf("answers %q, want %q", valid.Answers, want)
				}
			}
		})
	}
}

func TestARowsRecordReadsAsTheAgentsGateRecord(t *testing.T) {
	record := decode(t, []byte(`{"id": "ignored", "kind": "approval", "inputHash": null,
		"requestedBy": {"kind": "bearer", "id": "a", "label": "A"},
		"sessionOwner": {"kind": "browser", "id": "browser"}, "sessionCreator": null,
		"questions": [{"question": "Q?", "multiSelect": true, "header": "h"}], "inRun": true}`))
	got := EntryOf("durable:x:r:t", "answer", true, record.(map[string]any))
	want := &Entry{
		ID: "durable:x:r:t", Kind: "answer", State: "resolving",
		RequestedBy: &Principal{"bearer", "a"}, SessionOwner: &Principal{"browser", "browser"},
		Questions: []Question{{"Q?", true}}, InRun: true,
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("%+v", got)
	}
}

func TestAResponseIsMeasuredAsPythonWritesIt(t *testing.T) {
	for text, want := range map[string]string{
		`{"a":[1,1.0,-0.0,1e16,1e-5,0.0001,12345678901234567890,1.5e300]}`: `{"a":[1,1.0,-0.0,1e+16,1e-05,0.0001,12345678901234567890,1.5e+300]}`,
		`"é\u0001\"\u007f"`: `"é\u0001\"` + "\x7f" + `"`,
	} {
		if got := dumps(decode(t, []byte(text))); got != want {
			t.Errorf("%s: %s", text, got)
		}
	}
}
