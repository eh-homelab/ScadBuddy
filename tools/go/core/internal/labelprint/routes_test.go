package labelprint

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/auth"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/testdb"
)

const pdf = "%PDF-1.7 a label"

// routeAnswer is one answer of the routes.
type routeAnswer struct {
	status int
	body   map[string]any
	raw    string
}

// serveRoutes serves Public() on a mux behind auth.Off, its server the given URL.
func serveRoutes(t *testing.T, s *Service) string {
	t.Helper()
	gate := auth.NewGate(&auth.Resolver{Pool: s.Pool}, auth.Off, nil)
	mux := http.NewServeMux()
	for _, p := range s.Public() {
		mux.Handle(p.Pattern, gate.Wrap(p.Route, p.Handler))
	}
	server := httptest.NewServer(mux)
	t.Cleanup(server.Close)
	return server.URL
}

// fakeService is a Service whose server is the fake.
func fakeService(f *fakeCUPS) *Service {
	return &Service{
		Server:       func(context.Context) (*url.URL, error) { return f.URL, nil },
		HTTP:         f.Client(),
		ListTimeout:  5 * time.Second,
		PrintTimeout: 5 * time.Second,
	}
}

func call(t *testing.T, base, method, path string, body []byte) routeAnswer {
	t.Helper()
	var reader io.Reader
	if body != nil {
		reader = bytes.NewReader(body)
	}
	req, err := http.NewRequest(method, base+path, reader)
	if err != nil {
		t.Fatal(err)
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/pdf")
	}
	res, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = res.Body.Close() }()
	raw, err := io.ReadAll(res.Body)
	if err != nil {
		t.Fatal(err)
	}
	out := routeAnswer{status: res.StatusCode, raw: string(raw)}
	_ = json.Unmarshal(raw, &out.body)
	return out
}

func wantProblem(t *testing.T, got routeAnswer, status int, reason string) {
	t.Helper()
	if got.status != status || got.body["reason"] != reason {
		t.Fatalf("answer %d %s, want %d %s", got.status, got.raw, status, reason)
	}
}

func printJobs(f *fakeCUPS) []fakeRequest {
	var out []fakeRequest
	for _, r := range f.recorded() {
		if r.Msg.Code == 0x0002 {
			out = append(out, r)
		}
	}
	return out
}

func TestQueuesRoute(t *testing.T) {
	f := newFakeCUPS(t, twoQueues())
	got := call(t, serveRoutes(t, fakeService(f)), "GET", "/api/v1/label-printing/queues", nil)
	if got.status != 200 {
		t.Fatalf("%d %s", got.status, got.raw)
	}
	qs, _ := got.body["queues"].([]any)
	if len(qs) != 2 {
		t.Fatalf("queues %s", got.raw)
	}
	first := qs[0].(map[string]any)
	if first["name"] != "S-EDCDU03-2x1" || first["width_mm"] != 50.8 || first["height_mm"] != 25.4 || first["accepting"] != true {
		t.Errorf("first %v", first)
	}
	second := qs[1].(map[string]any)
	if second["name"] != "S-EDCDU03-4x6" || second["state_message"] != loadedMsg || second["accepting"] != false {
		t.Errorf("second %v", second)
	}
}

func TestQueuesRouteAnswersAnEmptyList(t *testing.T) {
	f := newFakeCUPS(t, nil)
	got := call(t, serveRoutes(t, fakeService(f)), "GET", "/api/v1/label-printing/queues", nil)
	if got.status != 200 || got.raw != `{"queues":[]}` {
		t.Fatalf("%d %s", got.status, got.raw)
	}
}

func TestPrintRoute(t *testing.T) {
	f := newFakeCUPS(t, twoQueues())
	got := call(t, serveRoutes(t, fakeService(f)), "POST",
		"/api/v1/label-printing/queues/S-EDCDU03-2x1/jobs?media_type=Direct&darkness=-1", []byte(pdf))
	if got.status != http.StatusAccepted {
		t.Fatalf("%d %s", got.status, got.raw)
	}
	want := map[string]any{"queue": "S-EDCDU03-2x1", "job_id": 7.0, "job_state": "processing", "job_state_reasons": []any{"job-printing"}}
	if !reflect.DeepEqual(got.body, want) {
		t.Errorf("body %v", got.body)
	}
	jobs := printJobs(f)
	if len(jobs) != 1 {
		t.Fatalf("print jobs %d", len(jobs))
	}
	msg := jobs[0].Msg
	if !bytes.Equal(msg.Data, []byte(pdf)) || jobs[0].Path != "/printers/S-EDCDU03-2x1" {
		t.Errorf("job %q at %s", msg.Data, jobs[0].Path)
	}
	if got := msg.Strings(TagJob, "MediaType"); !reflect.DeepEqual(got, []string{"Direct"}) {
		t.Errorf("MediaType %v", got)
	}
	for _, name := range []string{"Darkness", "zePrintRate"} {
		if got := msg.Strings(TagJob, name); got != nil {
			t.Errorf("%s %v", name, got)
		}
	}
}

func TestPrintRouteSendsEveryOption(t *testing.T) {
	f := newFakeCUPS(t, twoQueues())
	got := call(t, serveRoutes(t, fakeService(f)), "POST",
		"/api/v1/label-printing/queues/S-EDCDU03-2x1/jobs?media_type=Saved&darkness=30&print_rate=12", []byte(pdf))
	if got.status != http.StatusAccepted {
		t.Fatalf("%d %s", got.status, got.raw)
	}
	msg := printJobs(f)[0].Msg
	if msg.Strings(TagJob, "MediaType") != nil ||
		!reflect.DeepEqual(msg.Strings(TagJob, "Darkness"), []string{"30"}) ||
		!reflect.DeepEqual(msg.Strings(TagJob, "zePrintRate"), []string{"12"}) {
		t.Errorf("attrs %+v", msg.Attrs)
	}
}

func TestPrintAnswersAcceptedNotPrinted(t *testing.T) {
	f := newFakeCUPS(t, twoQueues())
	f.setJobState(4, "job-hold-until-specified")
	got := call(t, serveRoutes(t, fakeService(f)), "POST", "/api/v1/label-printing/queues/S-EDCDU03-2x1/jobs", []byte(pdf))
	if got.status != http.StatusAccepted || got.body["job_state"] != "pending-held" ||
		!reflect.DeepEqual(got.body["job_state_reasons"], []any{"job-hold-until-specified"}) {
		t.Fatalf("%d %s", got.status, got.raw)
	}
}

func TestUnknownQueueIsRefusedBeforePrint(t *testing.T) {
	f := newFakeCUPS(t, twoQueues())
	base := serveRoutes(t, fakeService(f))
	wantProblem(t, call(t, base, "POST", "/api/v1/label-printing/queues/Other/jobs", []byte(pdf)), 422, "label_queue_unknown")
	if n := len(printJobs(f)); n != 0 {
		t.Fatalf("%d print jobs", n)
	}
	before := len(f.recorded())
	for _, q := range []string{"a%2Fb", strings.Repeat("q", 128)} {
		wantProblem(t, call(t, base, "POST", "/api/v1/label-printing/queues/"+q+"/jobs", []byte(pdf)), 422, "label_queue_unknown")
	}
	if n := len(f.recorded()); n != before {
		t.Fatalf("the fake got %d more requests", n-before)
	}
}

func TestNotAPDFIs415(t *testing.T) {
	f := newFakeCUPS(t, twoQueues())
	base := serveRoutes(t, fakeService(f))
	for _, body := range []string{"", "%PDF", "<html>%PDF-1.7"} {
		wantProblem(t, call(t, base, "POST", "/api/v1/label-printing/queues/S-EDCDU03-2x1/jobs", []byte(body)), 415, "label_not_pdf")
	}
	if n := len(f.recorded()); n != 0 {
		t.Fatalf("the fake got %d requests", n)
	}
}

func TestBodyPastTheLimitIs413(t *testing.T) {
	f := newFakeCUPS(t, twoQueues())
	body := append([]byte(pdf), make([]byte, MaxBody+1-len(pdf))...)
	got := call(t, serveRoutes(t, fakeService(f)), "POST", "/api/v1/label-printing/queues/S-EDCDU03-2x1/jobs", body)
	wantProblem(t, got, http.StatusRequestEntityTooLarge, "label_too_large")
	if n := len(f.recorded()); n != 0 {
		t.Fatalf("the fake got %d requests", n)
	}
}

func TestBadOptionIs422(t *testing.T) {
	f := newFakeCUPS(t, twoQueues())
	base := serveRoutes(t, fakeService(f))
	for _, q := range []string{"darkness=31", "darkness=0", "darkness=01", "media_type=Ribbon", "print_rate=13", "print_rate=0"} {
		wantProblem(t, call(t, base, "POST", "/api/v1/label-printing/queues/S-EDCDU03-2x1/jobs?"+q, []byte(pdf)), 422, "label_option_invalid")
	}
	if n := len(f.recorded()); n != 0 {
		t.Fatalf("the fake got %d requests", n)
	}
}

func TestQueuesWhenCUPSIsDown(t *testing.T) {
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	down, _ := url.Parse("http://" + l.Addr().String())
	_ = l.Close()
	s := &Service{Server: func(context.Context) (*url.URL, error) { return down, nil }, ListTimeout: time.Second}
	wantProblem(t, call(t, serveRoutes(t, s), "GET", "/api/v1/label-printing/queues", nil), 502, "label_server_unreachable")
}

func TestQueuesTimeOut(t *testing.T) {
	f := newFakeCUPS(t, twoQueues())
	f.setHang(true)
	s := fakeService(f)
	s.ListTimeout = 50 * time.Millisecond
	wantProblem(t, call(t, serveRoutes(t, s), "GET", "/api/v1/label-printing/queues", nil), 502, "label_server_unreachable")
}

func TestPrintTimesOut(t *testing.T) {
	f := newFakeCUPS(t, twoQueues())
	f.setHangPrint(true)
	s := fakeService(f)
	s.PrintTimeout = 50 * time.Millisecond
	got := call(t, serveRoutes(t, s), "POST", "/api/v1/label-printing/queues/S-EDCDU03-2x1/jobs", []byte(pdf))
	wantProblem(t, got, 502, "label_server_unreachable")
	if n := len(printJobs(f)); n != 1 {
		t.Fatalf("%d print jobs", n)
	}
}

func TestPrintRefusedIs502(t *testing.T) {
	f := newFakeCUPS(t, twoQueues())
	f.setPrintReply(0x0506, "Destination is not accepting jobs.")
	got := call(t, serveRoutes(t, fakeService(f)), "POST", "/api/v1/label-printing/queues/S-EDCDU03-2x1/jobs", []byte(pdf))
	wantProblem(t, got, 502, "label_print_refused")
	if got.body["ipp_status"] != 1286.0 || got.body["detail"] != "Destination is not accepting jobs." {
		t.Errorf("body %s", got.raw)
	}
}

func TestPrintNotFoundIsUnknownQueue(t *testing.T) {
	f := newFakeCUPS(t, twoQueues())
	f.setPrintReply(0x0406, "The printer or class does not exist.")
	got := call(t, serveRoutes(t, fakeService(f)), "POST", "/api/v1/label-printing/queues/S-EDCDU03-2x1/jobs", []byte(pdf))
	wantProblem(t, got, 422, "label_queue_unknown")
}

func TestParseServer(t *testing.T) {
	for _, v := range []string{"http://cups.cups.svc:8631", "https://cups.internal.nullreference.io", "http://10.0.0.5:631/"} {
		u, err := ParseServer(v)
		if err != nil {
			t.Errorf("ParseServer(%q): %v", v, err)
			continue
		}
		if got := u.String(); got != strings.TrimSuffix(v, "/") {
			t.Errorf("ParseServer(%q) = %s", v, got)
		}
	}
	for _, v := range []string{"", "cups:631", "ftp://cups", "http://", "http://u:p@cups", "http://cups/printers",
		"http://cups?x=1", "http://cups#f", "javascript:alert(1)", "http://cups:99999"} {
		if u, err := ParseServer(v); err == nil {
			t.Errorf("ParseServer(%q) = %s", v, u)
		}
	}
}

func TestServerSetting(t *testing.T) {
	pool := testdb.New(t)
	ctx := context.Background()
	s := &Service{Pool: pool, ListTimeout: time.Second}
	u, err := s.server(ctx)
	if err != nil || u.String() != DefaultServer {
		t.Fatalf("no row: %v %v", u, err)
	}
	set := func(value string) {
		t.Helper()
		if _, err := pool.Exec(ctx, `INSERT INTO settings (name, value) VALUES ($1, $2::jsonb)
			ON CONFLICT (name) DO UPDATE SET value = EXCLUDED.value`, ServerSetting, value); err != nil {
			t.Fatal(err)
		}
	}
	set(`null`)
	if u, err := s.server(ctx); err != nil || u.String() != DefaultServer {
		t.Fatalf("null: %v %v", u, err)
	}
	set(`"https://cups.example.lan"`)
	if u, err := s.server(ctx); err != nil || u.String() != "https://cups.example.lan" {
		t.Fatalf("a row: %v %v", u, err)
	}
	set(`"cups:631"`)
	if _, err := s.server(ctx); !errors.Is(err, errBadServer) {
		t.Fatalf("a bad row: %v", err)
	}
	wantProblem(t, call(t, serveRoutes(t, s), "GET", "/api/v1/label-printing/queues", nil), 502, "label_server_unreachable")
}

func TestListRefusedIs502(t *testing.T) {
	f := newFakeCUPS(t, twoQueues())
	f.setListStatus(0x0507)
	got := call(t, serveRoutes(t, fakeService(f)), "GET", "/api/v1/label-printing/queues", nil)
	wantProblem(t, got, 502, "label_print_refused")
	if got.body["ipp_status"] != 1287.0 {
		t.Errorf("body %s", got.raw)
	}
}

// captureLog sends the default logger's output to the buffer until the test ends.
func captureLog(t *testing.T) *bytes.Buffer {
	t.Helper()
	var buf bytes.Buffer
	old := slog.Default()
	slog.SetDefault(slog.New(slog.NewTextHandler(&buf, nil)))
	t.Cleanup(func() { slog.SetDefault(old) })
	return &buf
}

func TestStoredValuesThatBreakTheRuleNeverReachTheLog(t *testing.T) {
	pool := testdb.New(t)
	ctx := context.Background()
	s := &Service{Pool: pool, ListTimeout: time.Second}
	for _, value := range []string{`"http://u:p@x y"`, `"http://us3r:s3cr3t@cups:99999"`, `7`} {
		if _, err := pool.Exec(ctx, `INSERT INTO settings (name, value) VALUES ($1, $2::jsonb)
			ON CONFLICT (name) DO UPDATE SET value = EXCLUDED.value`, ServerSetting, value); err != nil {
			t.Fatal(err)
		}
		buf := captureLog(t)
		got := call(t, serveRoutes(t, s), "GET", "/api/v1/label-printing/queues", nil)
		wantProblem(t, got, 502, "label_server_unreachable")
		for _, secret := range []string{"p@x", "s3cr3t", "us3r"} {
			if strings.Contains(buf.String(), secret) || strings.Contains(got.raw, secret) {
				t.Errorf("value %s leaks %q: log %s answer %s", value, secret, buf, got.raw)
			}
		}
		if !strings.Contains(buf.String(), "the stored label server URL is not valid") {
			t.Errorf("value %s: log %s", value, buf)
		}
	}
}
