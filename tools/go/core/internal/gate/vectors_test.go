package gate

import (
	"encoding/json"
	"os"
	"slices"
	"strings"
	"testing"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/jsonv"
)

// The shared vectors the agent's and the backend's validators run (backend/tests/fixtures).
const fixtures = "../../../../../backend/tests/fixtures/"

func load(t *testing.T, name string) jsonv.Object {
	t.Helper()
	raw, err := os.ReadFile(fixtures + name)
	if err != nil {
		t.Fatal(err)
	}
	return jsonv.MustParse(string(raw)).(jsonv.Object)
}

func expand(doc jsonv.Object, v any) any {
	switch t := v.(type) {
	case []any:
		out := make([]any, len(t))
		for i, e := range t {
			out[i] = expand(doc, e)
		}
		return out
	case jsonv.Object:
		if s, ok := t.Text("$repeat"); ok {
			times, _ := t.Get("times")
			if name, isName := times.(string); isName {
				times, _ = doc.Get(name)
			}
			n, _ := times.(json.Number).Int64()
			return strings.Repeat(s, int(n)+1)
		}
		out := jsonv.Object{}
		for _, m := range t {
			out = append(out, M(m.Key, expand(doc, m.Value)))
		}
		return out
	}
	return v
}

func whoOf(v any) *principal {
	o, ok := v.(jsonv.Object)
	if !ok {
		return nil
	}
	kind, _ := o.Text("kind")
	id, _ := o.Text("id")
	return &principal{kind, id}
}

func entryOfVector(v any) *entry {
	o, ok := v.(jsonv.Object)
	if !ok {
		return nil
	}
	text := func(k string) string { s, _ := o.Text(k); return s }
	e := &entry{id: text("id"), kind: text("kind"), state: text("state")}
	if h, ok := o.Text("input_hash"); ok {
		e.inputHash = &h
	}
	by, _ := o.Get("requested_by")
	owner, _ := o.Get("session_owner")
	creator, _ := o.Get("session_creator")
	e.requestedBy, e.sessionOwner, e.sessionCreator = whoOf(by), whoOf(owner), whoOf(creator)
	if qs, ok := o.Get("questions"); ok {
		e.hasQuestions = true
		for _, q := range qs.([]any) {
			q := q.(jsonv.Object)
			s, _ := q.Text("question")
			multi, _ := q.Get("multi_select")
			e.questions = append(e.questions, question{s, multi == true})
		}
	}
	if opts, ok := o.Get("options"); ok {
		e.hasOptions = true
		e.options = []string{}
		for _, s := range opts.([]any) {
			e.options = append(e.options, s.(string))
		}
	}
	inRun, _ := o.Get("in_run")
	e.inRun = inRun == true
	return e
}

func TestTheValidatorVectors(t *testing.T) {
	doc := load(t, "pending-input-vectors.json")
	max, _ := doc.Get("response_max")
	if max.(json.Number).String() != "529024" || ResponseMax != 529024 {
		t.Fatalf("the vectors' cap is %v", max)
	}
	vectors, _ := doc.Get("vectors")
	for _, raw := range vectors.([]any) {
		v := raw.(jsonv.Object)
		name, _ := v.Text("name")
		t.Run(name, func(t *testing.T) {
			r, _ := v.Get("request")
			req := r.(jsonv.Object)
			rid, _ := req.Text("request_id")
			role, _ := req.Text("role")
			responder, _ := req.Get("responder")
			response, _ := req.Get("response")
			e, _ := v.Get("entry")
			got, err := validate(entryOfVector(e), rid, expand(doc, response), *whoOf(responder), role)
			x, _ := v.Get("expect")
			expect := x.(jsonv.Object)
			if code, ok := expect.Text("refused"); ok {
				refused, isRefused := err.(*Refused)
				if !isRefused || refused.Code != code {
					t.Fatalf("got %v, %v; want refused %s", got, err, code)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			if d, ok := expect.Text("decision"); ok && got.decision != d {
				t.Fatalf("decision %q, want %q", got.decision, d)
			}
			if a, ok := expect.Get("answers"); ok {
				want := []string{}
				for _, s := range a.([]any) {
					want = append(want, s.(string))
				}
				if !slices.Equal(got.answers, want) {
					t.Fatalf("answers %q, want %q", got.answers, want)
				}
			}
		})
	}
}

func TestTheResponseHashVectors(t *testing.T) {
	doc := load(t, "response-hash-vectors.json")
	vectors, _ := doc.Get("vectors")
	for _, raw := range vectors.([]any) {
		v := raw.(jsonv.Object)
		name, _ := v.Text("name")
		value, _ := v.Get("value")
		want, _ := v.Text("sha256")
		if got, err := ResponseHash(value); err != nil || got != want {
			t.Errorf("%s: %s, %v; want %s", name, got, err, want)
		}
	}
	if _, err := ResponseHash(jsonv.Object{M("n", json.Number("1"))}); err == nil {
		t.Error("a number was hashed")
	}
}

func TestARowsRecordReadsAsTheAgentsGateRecord(t *testing.T) {
	row := &Row{RequestID: "durable:x:r:t", Kind: "answer", Expired: true}
	record := jsonv.MustParse(`{"id":"ignored","kind":"approval","inputHash":null,
		"requestedBy":{"kind":"bearer","id":"a","label":"A"},"sessionOwner":{"kind":"browser","id":"browser"},
		"sessionCreator":null,"questions":[{"question":"Q?","multiSelect":true,"header":"h"}],"inRun":true}`)
	e := gateEntry(row, record.(jsonv.Object))
	if e.id != "durable:x:r:t" || e.kind != "answer" || e.state != "resolving" || e.inputHash != nil ||
		*e.requestedBy != (principal{"bearer", "a"}) || *e.sessionOwner != (principal{"browser", "browser"}) ||
		e.sessionCreator != nil || !slices.Equal(e.questions, []question{{"Q?", true}}) ||
		e.hasOptions || !e.inRun {
		t.Fatalf("%+v", e)
	}
}

func TestJSONIsWrittenAsPythonWritesIt(t *testing.T) {
	v := jsonv.MustParse(`{"b":"é \u0001   \"q\" 🙂","a":[1,1.5,true,null],"b":"last"}`)
	if got := jsonv.Dumps(v); got != `{"b":"last","a":[1,1.5,true,null]}` {
		t.Fatal(got)
	}
	if got := jsonv.Dumps("é\u0001\n🙂"); got != `"é\u0001\n🙂"` {
		t.Fatal(got)
	}
	if got := jsonv.ASCIILen(jsonv.Object{M("a", "é🙂")}); got != 27 {
		t.Fatal(got)
	}
	if Cap("abcd", 3) != "ab…" || ToolUseID("durable:00000000-0000-4000-8000-000000000001:run:toolu_1") == nil ||
		ToolUseID("durable:x:run:toolu_1") != nil {
		t.Fatal("cap or tool use id")
	}
}
