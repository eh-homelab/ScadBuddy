package filamentpresets

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/auth"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/jsonv"
)

// fakeStarter is the Starter in memory. It records each write, the time its caller lets it
// wait, and answers with answer (a written preset by default).
type fakeStarter struct {
	mu     sync.Mutex
	writes []Write
	// deadlines are the wait's end of each Run, by its context.
	deadlines []time.Time
	answer    func(Write) (json.RawMessage, error)
}

func (f *fakeStarter) Run(ctx context.Context, w Write) (json.RawMessage, error) {
	f.mu.Lock()
	f.writes = append(f.writes, w)
	if deadline, ok := ctx.Deadline(); ok {
		f.deadlines = append(f.deadlines, deadline)
	}
	answer := f.answer
	f.mu.Unlock()
	if answer == nil {
		return nil, errors.New("the fake has no answer")
	}
	return answer(w)
}

func (f *fakeStarter) started() []Write {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]Write(nil), f.writes...)
}

var person = auth.Caller{Kind: auth.KindPerson, Principal: "user:test#ann", SessionKind: "login"}

// send is a write to the area's routes as caller c, its status and its body read with order
// kept. key "" sends no Idempotency-Key.
func (e *env) send(c auth.Caller, method, path, key, body string) (int, jsonv.Object) {
	e.t.Helper()
	req := httptest.NewRequest(method, path, strings.NewReader(body))
	if key != "" {
		req.Header.Set("Idempotency-Key", key)
	}
	req = req.WithContext(auth.WithCaller(req.Context(), c))
	rec := httptest.NewRecorder()
	e.mux.ServeHTTP(rec, req)
	raw, _ := io.ReadAll(rec.Body)
	v, err := jsonv.Parse(raw)
	if err != nil {
		e.t.Fatalf("%s %s: %d %q", method, path, rec.Code, raw)
	}
	o, _ := v.(jsonv.Object)
	if rec.Header().Get("Cache-Control") != "no-store" {
		e.t.Errorf("%s %s: Cache-Control %q", method, path, rec.Header().Get("Cache-Control"))
	}
	return rec.Code, o
}

func code(o jsonv.Object) string {
	s, _ := o.Text("code")
	return s
}

func written(id int64, version int, changed bool) func(Write) (json.RawMessage, error) {
	return func(Write) (json.RawMessage, error) {
		return json.RawMessage(fmt.Sprintf(`{"preset_id":%d,"version":%d,"changed":%t,"mirror":"off"}`, id, version, changed)), nil
	}
}

func TestCopyNeedsAKey(t *testing.T) {
	e := newEnv(t)
	id := e.preset("3dfp", "PLA", `{"name":"PLA"}`)
	for _, path := range []string{fmt.Sprintf("%s/%d/copy", listPath, id), fmt.Sprintf("%s/%d", listPath, id)} {
		method := http.MethodPost
		body := `{"name":"Mine"}`
		if !strings.HasSuffix(path, "/copy") {
			method, body = http.MethodPatch, `{"expected_version":1,"name":"Mine"}`
		}
		status, problem := e.send(person, method, path, "", body)
		kind, _ := problem.Text("type")
		if status != http.StatusPreconditionRequired || kind != keyRequiredProblem {
			t.Errorf("%s %s: %d %s", method, path, status, jsonv.Dumps(problem))
		}
	}
	if n := len(e.starter.started()); n != 0 {
		t.Errorf("%d writes started", n)
	}
}

func TestEditBodyAppliesR7(t *testing.T) {
	e := newEnv(t)
	id := e.preset("manual", "PLA", `{"name":"PLA"}`)
	many := make([]string, 0, maxEditKeys+1)
	for i := range maxEditKeys + 1 {
		many = append(many, fmt.Sprintf(`"k%d":"1"`, i))
	}
	long := strings.Repeat("x", 129)
	for _, c := range []struct {
		name, fields, code, key string
	}{
		{"name", `"set":{"name":"X"}`, keyRefused, "name"},
		{"printhost_apikey", `"set":{"printhost_apikey":"s"}`, keyRefused, "printhost_apikey"},
		{"my_token", `"unset":["my_token"]`, keyRefused, "my_token"},
		{"bad key", `"set":{"bad key":"1"}`, keyRefused, "bad key"},
		{"empty", `"set":{"":"1"}`, keyRefused, ""},
		{"long", `"set":{"` + long + `":"1"}`, keyRefused, strings.Repeat("x", 64) + "…"},
		{"both", `"set":{"a":"1"},"unset":["a"]`, keyRefused, "a"},
		{"a null set", `"set":null`, invalidInput, ""},
		{"a null unset", `"unset":null`, invalidInput, ""},
		{"501", `"set":{` + strings.Join(many, ",") + `}`, invalidInput, ""},
		{"empty edit", ``, invalidInput, ""},
		{"a number value", `"set":{"a":1}`, invalidInput, ""},
		{"a list of numbers", `"set":{"a":[1]}`, invalidInput, ""},
		{"an unset that is no list", `"unset":"a"`, invalidInput, ""},
		{"an empty name", `"name":"  "`, invalidInput, ""},
		{"a long name", `"name":"` + strings.Repeat("é", 201) + `"`, invalidInput, ""},
	} {
		body := `{"expected_version":1`
		if c.fields != "" {
			body += "," + c.fields
		}
		status, problem := e.send(person, http.MethodPatch, fmt.Sprintf("%s/%d", listPath, id), "k", body+"}")
		got, _ := problem.Get("key")
		if status != http.StatusUnprocessableEntity || code(problem) != c.code || c.code == keyRefused && got != c.key {
			t.Errorf("%s: %d %s", c.name, status, jsonv.Dumps(problem))
		}
	}
	for _, body := range []string{`{"name":"X"}`, `{"expected_version":0,"name":"X"}`, `{"expected_version":"1","name":"X"}`,
		`{"expected_version":1.0,"name":"X"}`, `{"expected_version":1.5,"name":"X"}`, `[]`, `{`} {
		before := len(e.starter.writes)
		if status, problem := e.send(person, http.MethodPatch, fmt.Sprintf("%s/%d", listPath, id), "k", body); status != http.StatusUnprocessableEntity ||
			code(problem) != invalidInput {
			t.Errorf("%s: %d %s", body, status, jsonv.Dumps(problem))
		}
		if len(e.starter.writes) != before {
			t.Errorf("%s: a write started", body)
		}
	}
	for _, body := range []string{`{}`, `{"name":""}`, `{"name":1}`} {
		if status, problem := e.send(person, http.MethodPost, fmt.Sprintf("%s/%d/copy", listPath, id), "k", body); status != http.StatusUnprocessableEntity ||
			code(problem) != invalidInput {
			t.Errorf("copy %s: %d %s", body, status, jsonv.Dumps(problem))
		}
	}
	if status, _ := e.send(person, http.MethodPatch, fmt.Sprintf("%s/%d", listPath, id), strings.Repeat("k", 201), `{"expected_version":1,"name":"X"}`); status != http.StatusUnprocessableEntity {
		t.Errorf("a key of 201 characters: %d", status)
	}
	if n := len(e.starter.started()); n != 0 {
		t.Errorf("%d writes started", n)
	}
}

func TestBothSetAndUnsetSaysSoLikePython(t *testing.T) {
	e := newEnv(t)
	id := e.preset("manual", "PLA", `{"name":"PLA"}`)
	_, problem := e.send(person, http.MethodPatch, fmt.Sprintf("%s/%d", listPath, id), "k",
		`{"expected_version":1,"set":{"a":"1"},"unset":["a"]}`)
	if detail, _ := problem.Get("detail"); detail != "the key a is both set and unset" {
		t.Errorf("%s", jsonv.Dumps(problem))
	}
	_, problem = e.send(person, http.MethodPatch, fmt.Sprintf("%s/%d", listPath, id), "k", `{"expected_version":1,"set":{"name":"1"}}`)
	if detail, _ := problem.Get("detail"); detail != "the key name cannot be set or unset" {
		t.Errorf("%s", jsonv.Dumps(problem))
	}
}

func TestWritesStartTheActivityOnLibrary(t *testing.T) {
	e := newEnv(t)
	id := e.preset("manual", "PLA", `{"name":"PLA"}`)
	e.starter.answer = written(id, 2, true)
	token := auth.Caller{Kind: auth.KindToken, Principal: "user:test#bob", TokenScope: "write"}
	// Each send's time span: the route's wait starts inside it.
	var spans [][2]time.Time
	timed := func(send func()) {
		before := time.Now()
		send()
		spans = append(spans, [2]time.Time{before, time.Now()})
	}
	timed(func() {
		e.send(person, http.MethodPost, fmt.Sprintf("%s/%d/copy", listPath, id), "c1", `{"name":"Mine"}`)
	})
	timed(func() {
		e.send(token, http.MethodPatch, fmt.Sprintf("%s/%d", listPath, id), "e1",
			`{"expected_version":1,"name":"PLA 2","set":{"z":"1","a":["2","3"]},"unset":["b"]}`)
	})
	e.starter.answer = func(Write) (json.RawMessage, error) { return json.RawMessage(`"sent"`), nil }
	timed(func() {
		e.send(auth.Caller{Kind: auth.KindAnonymous}, http.MethodPost, fmt.Sprintf("%s/%d/mirror", listPath, id), "", ``)
	})
	writes := e.starter.started()
	if len(writes) != 3 {
		t.Fatalf("%d writes", len(writes))
	}
	want := []struct {
		activity, input string
		timeout         time.Duration
	}{
		{"filament_preset_copy", fmt.Sprintf(`{"preset_id":%d,"name":"Mine","actor":"user:test#ann","idempotency_key":"c1"}`, id), 60 * time.Second},
		{"filament_preset_edit", fmt.Sprintf(`{"preset_id":%d,"expected_version":1,"name":"PLA 2","set":{"z":"1","a":["2","3"]},"unset":["b"],"actor":"user:test#bob","idempotency_key":"e1"}`, id), 60 * time.Second},
		{"filament_preset_mirror_again", fmt.Sprintf(`{"preset_id":%d}`, id), 90 * time.Second},
	}
	for i, w := range writes {
		if w.Activity != want[i].activity || jsonv.Dumps(w.Input) != want[i].input || w.Timeout != want[i].timeout {
			t.Errorf("write %d: %s %s %s", i, w.Activity, jsonv.Dumps(w.Input), w.Timeout)
		}
	}
	// The route waits for the result the activity's bound plus 10 s (R11), and no longer: the
	// wait ends that long after a time inside the send, whatever the runner's speed.
	e.starter.mu.Lock()
	defer e.starter.mu.Unlock()
	if len(e.starter.deadlines) != len(spans) {
		t.Fatalf("%d deadlines", len(e.starter.deadlines))
	}
	for i, deadline := range e.starter.deadlines {
		limit := want[i].timeout + 10*time.Second
		if deadline.Before(spans[i][0].Add(limit)) || deadline.After(spans[i][1].Add(limit)) {
			t.Errorf("write %d waits until %s, not %s after a time in %v", i, deadline, limit, spans[i])
		}
	}
}

func TestCopyJoinsARunWithTheSameKey(t *testing.T) {
	e := newEnv(t)
	id := e.preset("manual", "PLA", `{"name":"PLA"}`)
	e.starter.answer = written(id, 1, true)
	copyPath := fmt.Sprintf("%s/%d/copy", listPath, id)
	e.send(person, http.MethodPost, copyPath, "same", `{"name":"Mine"}`)
	e.send(person, http.MethodPost, copyPath, "same", `{"name":"Mine"}`)
	e.send(person, http.MethodPost, copyPath, "other", `{"name":"Mine"}`)
	e.send(auth.Caller{Kind: auth.KindPerson, Principal: "user:test#bob"}, http.MethodPost, copyPath, "same", `{"name":"Mine"}`)
	e.send(person, http.MethodPatch, fmt.Sprintf("%s/%d", listPath, id), "same", `{"expected_version":1,"name":"X"}`)
	e.send(person, http.MethodPost, fmt.Sprintf("%s/%d/mirror", listPath, id), "", ``)
	writes := e.starter.started()
	sum := sha256.Sum256(fmt.Appendf(nil, "user:test#ann\x1fsame\x1f%d\x1f%s", id, jsonv.Dumps(writes[0].Input)))
	if want := "filament-preset-copy-" + hex.EncodeToString(sum[:]); writes[0].WorkflowID != want || writes[1].WorkflowID != want {
		t.Errorf("%s %s, want %s", writes[0].WorkflowID, writes[1].WorkflowID, want)
	}
	if writes[2].WorkflowID == writes[0].WorkflowID || writes[3].WorkflowID == writes[0].WorkflowID {
		t.Error("another key or actor joins the run")
	}
	sum = sha256.Sum256(fmt.Appendf(nil, "user:test#ann\x1fsame\x1f%d\x1f%s", id, jsonv.Dumps(writes[4].Input)))
	if want := "filament-preset-edit-" + hex.EncodeToString(sum[:]); writes[4].WorkflowID != want {
		t.Errorf("edit %s", writes[4].WorkflowID)
	}
	if want := fmt.Sprintf("filament-preset-mirror-%d", id); writes[5].WorkflowID != want {
		t.Errorf("mirror %s", writes[5].WorkflowID)
	}
}

func TestTheSameKeyWithAnotherBodyRunsApart(t *testing.T) {
	e := newEnv(t)
	id := e.preset("manual", "PLA", `{"name":"PLA"}`)
	e.starter.answer = written(id, 1, true)
	copyPath := fmt.Sprintf("%s/%d/copy", listPath, id)
	e.send(person, http.MethodPost, copyPath, "same", `{"name":"Mine"}`)
	e.send(person, http.MethodPost, copyPath, "same", `{ "name": "Mine" }`)
	e.send(person, http.MethodPost, copyPath, "same", `{"name":"Yours"}`)
	writes := e.starter.started()
	if writes[0].WorkflowID != writes[1].WorkflowID {
		t.Error("the same key and body must join one run")
	}
	if writes[2].WorkflowID == writes[0].WorkflowID {
		t.Error("the same key with another body must start its own run")
	}
}

func TestEditMapsEachFailureType(t *testing.T) {
	e := newEnv(t)
	id := e.preset("manual", "PLA", `{"name":"PLA"}`)
	for _, c := range []struct {
		failure      Failure
		path         string
		status       int
		code, ext    string
		extValue     string
		problemType  string
		wantedDetail string
	}{
		{failure: Failure{Type: "preset_not_found", Message: "no filament preset 9"}, status: 404, code: "preset_not_found", wantedDetail: "no filament preset 9"},
		{failure: Failure{Type: "document_missing", Message: "lost"}, status: 409, code: "document_missing"},
		{failure: Failure{Type: "not_manual", Message: "copy it first"}, status: 409, code: "not_manual"},
		{failure: Failure{Type: "version_moved", Message: "at 4", Detail: json.RawMessage(`4`)}, status: 409, code: "version_moved", ext: "current_version", extValue: "4"},
		{failure: Failure{Type: "key_refused", Message: "the key printhost_apikey cannot be set or unset", Detail: json.RawMessage(`"printhost_apikey"`)},
			status: 422, code: "key_refused", ext: "key", extValue: `"printhost_apikey"`},
		{failure: Failure{Type: "idempotency_key_reused", Message: "another body"}, status: 422, code: "idempotency_key_reused", problemType: keyReusedProblem},
		{failure: Failure{Type: "invalid_input", Message: "set.a: not a string", Detail: json.RawMessage(`[{"loc":["set","a"],"msg":"not a string"}]`)}, status: 422, code: "invalid_input"},
		{failure: Failure{Type: "mirror_off"}, path: "mirror", status: 409, code: "mirror_off"},
		{failure: Failure{Type: "not_configured"}, path: "mirror", status: 409, code: "not_configured"},
		{failure: Failure{Type: "instance_changed"}, path: "mirror", status: 409, code: "instance_changed"},
		{failure: Failure{Type: "not_mirrored"}, path: "mirror", status: 409, code: "not_mirrored"},
		{failure: Failure{Type: "RuntimeError", Message: "a bug"}, status: 500},
	} {
		failure := c.failure
		e.starter.answer = func(Write) (json.RawMessage, error) { return nil, &failure }
		method, path, key, body := http.MethodPatch, fmt.Sprintf("%s/%d", listPath, id), "k", `{"expected_version":3,"name":"X"}`
		if c.path == "mirror" {
			method, path, key, body = http.MethodPost, path+"/mirror", "", ""
		}
		status, problem := e.send(person, method, path, key, body)
		if status != c.status || code(problem) != c.code {
			t.Errorf("%s: %d %s", c.failure.Type, status, jsonv.Dumps(problem))
			continue
		}
		if c.ext != "" {
			if got, _ := problem.Get(c.ext); jsonv.Dumps(got) != c.extValue {
				t.Errorf("%s: %s %s", c.failure.Type, c.ext, jsonv.Dumps(got))
			}
		}
		if kind, _ := problem.Text("type"); c.problemType != "" && kind != c.problemType {
			t.Errorf("%s: type %s", c.failure.Type, kind)
		}
		if detail, _ := problem.Text("detail"); c.wantedDetail != "" && detail != c.wantedDetail {
			t.Errorf("%s: detail %q", c.failure.Type, detail)
		}
		if c.status == 500 {
			if detail, _ := problem.Text("detail"); strings.Contains(detail, "a bug") {
				t.Errorf("an unknown failure's message reached the answer: %q", detail)
			}
		}
	}
}

func TestAWriteTimeoutIs503Pending(t *testing.T) {
	e := newEnv(t)
	id := e.preset("manual", "PLA", `{"name":"PLA"}`)
	for _, err := range []error{context.DeadlineExceeded, errors.New("temporal: the start was refused"), &Failure{Type: timeoutFailure}} {
		e.starter.answer = func(Write) (json.RawMessage, error) { return nil, err }
		status, problem := e.send(person, http.MethodPost, fmt.Sprintf("%s/%d/copy", listPath, id), "k", `{"name":"Mine"}`)
		detail, _ := problem.Text("detail")
		if status != http.StatusServiceUnavailable || code(problem) != writePending || detail != problems[writePending].detail || !strings.Contains(detail, "same Idempotency-Key") {
			t.Errorf("%v: %d %s", err, status, jsonv.Dumps(problem))
		}
	}
	// A send again has no key, so the text must not ask every write for one.
	status, problem := e.send(person, http.MethodPost, fmt.Sprintf("%s/%d/mirror", listPath, id), "", ``)
	detail, _ := problem.Text("detail")
	if status != http.StatusServiceUnavailable || code(problem) != writePending || detail != mirrorPendingDetail || strings.Contains(detail, "Key") {
		t.Errorf("mirror: %d %s", status, jsonv.Dumps(problem))
	}
}

func TestWritesWithoutTemporalAre503(t *testing.T) {
	e := newEnvWith(t, nil)
	id := e.preset("manual", "PLA", `{"name":"PLA"}`)
	for _, w := range []struct{ method, path, key, body string }{
		{http.MethodPost, fmt.Sprintf("%s/%d/copy", listPath, id), "k", `{"name":"Mine"}`},
		{http.MethodPatch, fmt.Sprintf("%s/%d", listPath, id), "k", `{"expected_version":1,"name":"X"}`},
		{http.MethodPost, fmt.Sprintf("%s/%d/mirror", listPath, id), "", ``},
	} {
		if status, problem := e.send(person, w.method, w.path, w.key, w.body); status != http.StatusServiceUnavailable || code(problem) != writesNotConfigured {
			t.Errorf("%s %s: %d %s", w.method, w.path, status, jsonv.Dumps(problem))
		}
	}
	if status, _, _ := e.get(listPath); status != http.StatusOK {
		t.Errorf("the list: %d", status)
	}
}

func TestTheWriteAnswersHaveTheWireKeys(t *testing.T) {
	e := newEnv(t)
	source := e.preset("3dfp", "PLA", `{"name":"PLA"}`)
	made := e.preset("manual", "Mine", `{"name":"Mine"}`)
	fixture := wire(t)

	e.starter.answer = written(made, 1, true)
	status, detail := e.send(person, http.MethodPost, fmt.Sprintf("%s/%d/copy", listPath, source), "k", `{"name":"Mine"}`)
	if status != http.StatusCreated || number(detail, "id") != fmt.Sprint(made) {
		t.Fatalf("copy: %d %s", status, jsonv.Dumps(detail))
	}
	sameKeys(t, "copy", detail, member(fixture, "detail"))

	e.starter.answer = written(made, 1, false)
	status, edited := e.send(person, http.MethodPatch, fmt.Sprintf("%s/%d", listPath, made), "k", `{"expected_version":1,"name":"Mine"}`)
	if changed, _ := edited.Get("changed"); status != http.StatusOK || changed != false {
		t.Fatalf("edit: %d %s", status, jsonv.Dumps(edited))
	}
	want := member(fixture, "edited")
	sameKeys(t, "edited", edited, want)
	sameKeys(t, "edited.preset", field(edited, "preset"), field(want, "preset"))

	e.starter.answer = func(Write) (json.RawMessage, error) { return json.RawMessage(`"in_sync"`), nil }
	status, outcome := e.send(person, http.MethodPost, fmt.Sprintf("%s/%d/mirror", listPath, made), "", ``)
	if got, _ := outcome.Text("outcome"); status != http.StatusOK || got != "in_sync" {
		t.Fatalf("mirror: %d %s", status, jsonv.Dumps(outcome))
	}
	want = member(fixture, "mirror_again_outcome")
	sameKeys(t, "mirror_again_outcome", outcome, want)
	sameKeys(t, "mirror_again_outcome.preset", field(outcome, "preset"), field(want, "preset"))
}
