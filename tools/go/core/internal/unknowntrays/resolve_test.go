package unknowntrays

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"go.temporal.io/sdk/client"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/auth"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/jsonv"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/libraryrun"
)

const (
	resolveTray = "5d0c8f1e-0000-4000-8000-000000007143"
	resolveURL  = listPath + "/" + resolveTray + "/resolve"
	spoolUUID   = "5d0c8f1e-0000-4000-8000-0000000071cc"
	seenAt      = "2026-11-26T09:30:00.123456Z"
)

var ann = auth.Caller{Kind: auth.KindPerson, Principal: "user:test#ann", SessionKind: "login"}

// fakeStarter records each write and the wait its caller gives it, and answers with answer.
type fakeStarter struct {
	mu        sync.Mutex
	writes    []libraryrun.Write
	deadlines []time.Time
	answer    func(libraryrun.Write) (json.RawMessage, error)
}

func (f *fakeStarter) Run(ctx context.Context, w libraryrun.Write) (json.RawMessage, error) {
	f.mu.Lock()
	f.writes = append(f.writes, w)
	if deadline, ok := ctx.Deadline(); ok {
		f.deadlines = append(f.deadlines, deadline)
	}
	answer := f.answer
	f.mu.Unlock()
	if answer == nil {
		return json.RawMessage(`{"id":"` + spoolUUID + `","number":12,"code":"S-12","location_id":null}`), nil
	}
	return answer(w)
}

func (f *fakeStarter) started() []libraryrun.Write {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]libraryrun.Write(nil), f.writes...)
}

// resolveMux is the area's public routes with starter as their Temporal (nil: none).
func resolveMux(starter libraryrun.Starter) *http.ServeMux {
	mux := http.NewServeMux()
	for _, p := range Public(nil, nil, starter)[Area] {
		mux.Handle(p.Pattern, p.Handler)
	}
	return mux
}

func post(t *testing.T, mux http.Handler, c auth.Caller, path, body string) (int, jsonv.Object, http.Header) {
	t.Helper()
	req := httptest.NewRequest(http.MethodPost, path, strings.NewReader(body))
	req = req.WithContext(auth.WithCaller(req.Context(), c))
	rec := httptest.NewRecorder()
	mux.ServeHTTP(rec, req)
	raw, _ := io.ReadAll(rec.Body)
	v, err := jsonv.Parse(raw)
	if err != nil {
		t.Fatalf("POST %s: %d %q", path, rec.Code, raw)
	}
	o, _ := v.(jsonv.Object)
	return rec.Code, o, rec.Header()
}

func codeOf(o jsonv.Object) string {
	s, _ := o.Text("code")
	return s
}

func TestResolveBodyNeedsExactlyOneOfSpoolAndCode(t *testing.T) {
	starter := &fakeStarter{}
	mux := resolveMux(starter)
	for _, body := range []string{
		`{"spool_id":"` + spoolUUID + `","code":"S-12","net_weight_g":980,"seen_at":"` + seenAt + `"}`,
		`{"net_weight_g":980,"seen_at":"` + seenAt + `"}`,
		`{"spool_id":null,"code":null,"net_weight_g":980,"seen_at":"` + seenAt + `"}`,
		`{"code":"S-12","net_weight_g":0,"seen_at":"` + seenAt + `"}`,
		`{"code":"S-12","net_weight_g":-1,"seen_at":"` + seenAt + `"}`,
		`{"code":"S-12","net_weight_g":100000.5,"seen_at":"` + seenAt + `"}`,
		`{"code":"S-12","net_weight_g":"980","seen_at":"` + seenAt + `"}`,
		`{"code":"S-12","seen_at":"` + seenAt + `"}`,
		`{"code":"S-12","net_weight_g":980,"seen_at":"2026-11-26T09:30:00"}`,
		`{"code":"S-12","net_weight_g":980,"seen_at":"yesterday"}`,
		`{"code":"S-12","net_weight_g":980}`,
		`{"code":"","net_weight_g":980,"seen_at":"` + seenAt + `"}`,
		`{"code":12,"net_weight_g":980,"seen_at":"` + seenAt + `"}`,
		`{"spool_id":"not-a-uuid","net_weight_g":980,"seen_at":"` + seenAt + `"}`,
		`{"code":"S-12","net_weight_g":980,"seen_at":"` + seenAt + `","colour":"red"}`,
		`[]`,
		`not json`,
	} {
		status, problem, _ := post(t, mux, ann, resolveURL, body)
		if status != http.StatusUnprocessableEntity || codeOf(problem) != "invalid_input" {
			t.Errorf("%s: %d %s", body, status, jsonv.Dumps(problem))
		}
	}
	if n := len(starter.started()); n != 0 {
		t.Errorf("%d runs started", n)
	}
	// The largest weight is allowed.
	status, _, _ := post(t, mux, ann, resolveURL, `{"code":"S-12","net_weight_g":100000,"seen_at":"`+seenAt+`"}`)
	if status != http.StatusOK {
		t.Errorf("100000 g: %d", status)
	}
}

func TestResolveStartsTheActivityOnLibrary(t *testing.T) {
	starter := &fakeStarter{}
	status, _, header := post(t, resolveMux(starter), ann, resolveURL,
		`{"code":"S-12","net_weight_g":980.5,"seen_at":"`+seenAt+`"}`)
	if status != http.StatusOK || header.Get("Cache-Control") != "no-store" {
		t.Fatalf("%d %q", status, header.Get("Cache-Control"))
	}
	writes := starter.started()
	if len(writes) != 1 {
		t.Fatalf("%d runs", len(writes))
	}
	w := writes[0]
	if w.Activity != "filament_resolve_unknown_tray" || w.Timeout != 30*time.Second {
		t.Errorf("%+v", w)
	}
	want := `{"tray_id":"` + resolveTray + `","spool_id":null,"code":"S-12","net_weight_g":980.5,"seen_at":"` + seenAt + `","actor":"user:test#ann"}`
	if got := jsonv.Dumps(w.Input); got != want {
		t.Errorf("input %s\nwant  %s", got, want)
	}
	if !strings.HasPrefix(w.WorkflowID, "unknown-tray-resolve-") || len(w.WorkflowID) != len("unknown-tray-resolve-")+64 {
		t.Errorf("id %q", w.WorkflowID)
	}
	// The route waits the activity's timeout plus the margin.
	if wait := time.Until(starter.deadlines[0]); wait <= 30*time.Second || wait > 30*time.Second+libraryrun.WaitMargin {
		t.Errorf("wait %s", wait)
	}

	// A spool by id: the code is null.
	post(t, resolveMux(starter), ann, resolveURL, `{"spool_id":"`+strings.ToUpper(spoolUUID)+`","net_weight_g":980,"seen_at":"`+seenAt+`"}`)
	byID := starter.started()[1]
	if got, _ := byID.Input.Get("spool_id"); got != spoolUUID {
		t.Errorf("spool_id %v", got)
	}
	if got, _ := byID.Input.Get("code"); got != nil {
		t.Errorf("code %v", got)
	}

	// Through the real starter, the run's activity goes to the library queue.
	c := &startClient{}
	real := libraryrun.TemporalStarter{Client: c, Queue: "filament-presets", LibraryQueue: "library"}
	status, _, _ = post(t, resolveMux(real), ann, resolveURL, `{"code":"S-12","net_weight_g":980,"seen_at":"`+seenAt+`"}`)
	if status != http.StatusOK {
		t.Fatalf("real starter: %d", status)
	}
	run := c.args[0].(libraryrun.Run)
	if run.Activity != "filament_resolve_unknown_tray" || run.Queue != "library" || run.StartToCloseS != 30 ||
		c.options.TaskQueue != "filament-presets" || c.name != libraryrun.WorkflowName {
		t.Errorf("%+v %+v %v", run, c.options, c.name)
	}
}

func TestResolveRunIDJoinsEqualSendsOnly(t *testing.T) {
	starter := &fakeStarter{}
	mux := resolveMux(starter)
	body := `{"code":"S-12","net_weight_g":980,"seen_at":"` + seenAt + `"}`
	post(t, mux, ann, resolveURL, body)
	post(t, mux, ann, resolveURL, body)
	post(t, mux, ann, resolveURL, `{"code":"S-13","net_weight_g":980,"seen_at":"`+seenAt+`"}`)
	post(t, mux, auth.Caller{Kind: auth.KindPerson, Principal: "user:test#bob"}, resolveURL, body)
	w := starter.started()
	if w[0].WorkflowID != w[1].WorkflowID {
		t.Error("two equal sends run apart")
	}
	if w[0].WorkflowID == w[2].WorkflowID || w[0].WorkflowID == w[3].WorkflowID {
		t.Error("another body or another actor joins the run")
	}
}

func TestResolveMapsEachFailureType(t *testing.T) {
	for _, c := range []struct {
		failure libraryrun.Failure
		status  int
		code    string
		detail  string
	}{
		{libraryrun.Failure{Type: "tray_changed", Message: "the tray changed or was resolved"}, 409, "tray_changed", ""},
		{libraryrun.Failure{Type: "code_unknown", Message: "no spool has the code 'S-99'"}, 404, "code_unknown", ""},
		{libraryrun.Failure{Type: "spool_archived", Message: "that spool is archived"}, 409, "spool_archived", ""},
		{libraryrun.Failure{Type: "no_ams_kind", Message: "no location kind is flagged ams"}, 409, "no_ams_kind", ""},
		{libraryrun.Failure{Type: "spool_missing", Message: "no spool x"}, 404, "spool_missing", ""},
		{libraryrun.Failure{Type: "tag_clash", Message: "the tag on AMS-A slot 2 is on S-3"}, 409, "tag_clash", "the tag on AMS-A slot 2 is on S-3"},
		{libraryrun.Failure{Type: "RuntimeError", Message: "a bug"}, 500, "", ""},
	} {
		failure := c.failure
		starter := &fakeStarter{answer: func(libraryrun.Write) (json.RawMessage, error) { return nil, &failure }}
		status, problem, header := post(t, resolveMux(starter), ann, resolveURL, `{"code":"S-12","net_weight_g":980,"seen_at":"`+seenAt+`"}`)
		if status != c.status || codeOf(problem) != c.code {
			t.Errorf("%s: %d %s", c.failure.Type, status, jsonv.Dumps(problem))
		}
		detail, _ := problem.Text("detail")
		if c.detail != "" && detail != c.detail {
			t.Errorf("%s: detail %q", c.failure.Type, detail)
		}
		if detail == "" || strings.Contains(detail, "a bug") {
			t.Errorf("%s: detail %q", c.failure.Type, detail)
		}
		if header.Get("Cache-Control") != "no-store" {
			t.Errorf("%s: Cache-Control %q", c.failure.Type, header.Get("Cache-Control"))
		}
	}
}

func TestResolveTimeoutIs503(t *testing.T) {
	for _, err := range []error{
		context.DeadlineExceeded,
		errors.New("temporal: the start was refused"),
		&libraryrun.Failure{Type: libraryrun.TimeoutFailure, Message: "activity StartToClose timeout"},
	} {
		starter := &fakeStarter{answer: func(libraryrun.Write) (json.RawMessage, error) { return nil, err }}
		status, problem, _ := post(t, resolveMux(starter), ann, resolveURL, `{"code":"S-12","net_weight_g":980,"seen_at":"`+seenAt+`"}`)
		if status != http.StatusServiceUnavailable || codeOf(problem) != "resolve_pending" {
			t.Errorf("%v: %d %s", err, status, jsonv.Dumps(problem))
		}
	}
}

func TestResolveAnswersTheSpoolsIdentity(t *testing.T) {
	starter := &fakeStarter{answer: func(libraryrun.Write) (json.RawMessage, error) {
		return json.RawMessage(`{"id":"` + spoolUUID + `","number":12,"code":"S-12","location_id":"5d0c8f1e-0000-4000-8000-0000000071dd"}`), nil
	}}
	status, out, _ := post(t, resolveMux(starter), ann, resolveURL, `{"code":"S-12","net_weight_g":980,"seen_at":"`+seenAt+`"}`)
	want := `{"id":"` + spoolUUID + `","number":12,"code":"S-12","location_id":"5d0c8f1e-0000-4000-8000-0000000071dd"}`
	if status != http.StatusOK || jsonv.Dumps(out) != want {
		t.Fatalf("%d %s", status, jsonv.Dumps(out))
	}
	// A place that is not known is null, not left out.
	starter.answer = nil
	_, out, _ = post(t, resolveMux(starter), ann, resolveURL, `{"code":"S-12","net_weight_g":980,"seen_at":"`+seenAt+`"}`)
	if got := jsonv.Dumps(out); got != `{"id":"`+spoolUUID+`","number":12,"code":"S-12","location_id":null}` {
		t.Errorf("%s", got)
	}
	// An answer Go cannot read is a 500.
	starter.answer = func(libraryrun.Write) (json.RawMessage, error) { return json.RawMessage(`{"number":12}`), nil }
	if status, _, _ := post(t, resolveMux(starter), ann, resolveURL, `{"code":"S-12","net_weight_g":980,"seen_at":"`+seenAt+`"}`); status != 500 {
		t.Errorf("unreadable: %d", status)
	}
}

func TestResolveWithoutTemporalIs503(t *testing.T) {
	status, problem, _ := post(t, resolveMux(nil), ann, resolveURL, `{"code":"S-12","net_weight_g":980,"seen_at":"`+seenAt+`"}`)
	if status != http.StatusServiceUnavailable || codeOf(problem) != "writes_not_configured" {
		t.Errorf("%d %s", status, jsonv.Dumps(problem))
	}
}

func TestResolveOfAPathThatIsNoTrayIsTrayChanged(t *testing.T) {
	starter := &fakeStarter{}
	status, problem, _ := post(t, resolveMux(starter), ann, listPath+"/not-a-uuid/resolve", `{"code":"S-12","net_weight_g":980,"seen_at":"`+seenAt+`"}`)
	if status != http.StatusConflict || codeOf(problem) != "tray_changed" || len(starter.started()) != 0 {
		t.Errorf("%d %s", status, jsonv.Dumps(problem))
	}
}

// startClient is a Temporal client whose ExecuteWorkflow keeps what it got and answers a run
// whose result is a resolved spool. The embedded nil Client panics on any other call.
type startClient struct {
	client.Client
	options client.StartWorkflowOptions
	name    any
	args    []any
}

func (c *startClient) ExecuteWorkflow(_ context.Context, options client.StartWorkflowOptions, workflow any, args ...any) (client.WorkflowRun, error) {
	c.options, c.name, c.args = options, workflow, args
	return resolvedRun{}, nil
}

type resolvedRun struct{ client.WorkflowRun }

func (resolvedRun) Get(_ context.Context, valuePtr any) error {
	*valuePtr.(*json.RawMessage) = json.RawMessage(`{"id":"` + spoolUUID + `","number":12,"code":"S-12","location_id":null}`)
	return nil
}
