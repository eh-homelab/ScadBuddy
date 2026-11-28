package ghwebhook

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"
)

const secret = "test-webhook-secret-made-for-the-test"

func sign(body string) string {
	m := hmac.New(sha256.New, []byte(secret))
	m.Write([]byte(body))
	return "sha256=" + hex.EncodeToString(m.Sum(nil))
}

type rig struct {
	h      http.Handler
	logs   *bytes.Buffer
	reader *sdkmetric.ManualReader
}

func newRig(t *testing.T) rig {
	t.Helper()
	logs := &bytes.Buffer{}
	reader := sdkmetric.NewManualReader()
	h, err := New([]byte(secret), Options{
		Logger: slog.New(slog.NewTextHandler(logs, nil)),
		Meter:  sdkmetric.NewMeterProvider(sdkmetric.WithReader(reader)),
	})
	if err != nil {
		t.Fatal(err)
	}
	return rig{h: h, logs: logs, reader: reader}
}

// send posts body with the event and signature headers ("" leaves a header out).
func (r rig) send(method, event, signature string, body io.Reader) *httptest.ResponseRecorder {
	req := httptest.NewRequest(method, "/github/webhook", body)
	if event != "" {
		req.Header.Set("X-GitHub-Event", event)
	}
	if signature != "" {
		req.Header.Set("X-Hub-Signature-256", signature)
	}
	req.Header.Set("X-GitHub-Delivery", "d-1")
	w := httptest.NewRecorder()
	r.h.ServeHTTP(w, req)
	return w
}

// events is the counter's points, as "event action" to count.
func (r rig) events(t *testing.T) map[string]int64 {
	t.Helper()
	var rm metricdata.ResourceMetrics
	if err := r.reader.Collect(context.Background(), &rm); err != nil {
		t.Fatal(err)
	}
	got := map[string]int64{}
	for _, sm := range rm.ScopeMetrics {
		for _, m := range sm.Metrics {
			if m.Name != eventsMetric {
				continue
			}
			for _, p := range m.Data.(metricdata.Sum[int64]).DataPoints {
				e, _ := p.Attributes.Value("event")
				a, _ := p.Attributes.Value("action")
				got[e.AsString()+" "+a.AsString()] += p.Value
			}
		}
	}
	return got
}

func TestMethodNotAllowed(t *testing.T) {
	r := newRig(t)
	for _, m := range []string{http.MethodGet, http.MethodPut, http.MethodDelete, http.MethodHead} {
		w := r.send(m, "installation", sign(""), nil)
		if w.Code != http.StatusMethodNotAllowed || w.Header().Get("Allow") != http.MethodPost {
			t.Errorf("%s: %d, Allow %q", m, w.Code, w.Header().Get("Allow"))
		}
	}
}

// failReader fails the test when the handler reads the body.
type failReader struct{ t *testing.T }

func (f failReader) Read([]byte) (int, error) {
	f.t.Error("the body was read with no signature")
	return 0, io.EOF
}

func TestSignature(t *testing.T) {
	r := newRig(t)
	body := `{"action":"deleted","installation":{"id":9}}`
	if w := r.send(http.MethodPost, "installation", "", failReader{t}); w.Code != http.StatusUnauthorized {
		t.Errorf("no signature: %d", w.Code)
	}
	for name, sig := range map[string]string{
		"wrong":        sign(body + " "),
		"other secret": "sha256=" + strings.Repeat("0", 64),
		"not hex":      "sha256=zz",
		"sha1":         "sha1=" + strings.Repeat("0", 40),
		"no prefix":    strings.TrimPrefix(sign(body), "sha256="),
		"upper prefix": "SHA256=" + strings.TrimPrefix(sign(body), "sha256="),
		"short":        sign(body)[:20],
	} {
		if w := r.send(http.MethodPost, "installation", sig, strings.NewReader(body)); w.Code != http.StatusUnauthorized {
			t.Errorf("%s: %d", name, w.Code)
		}
	}
	if got := r.events(t); len(got) != 0 {
		t.Errorf("counted %v", got)
	}
	if strings.Contains(r.logs.String(), secret) {
		t.Errorf("the log holds the secret: %s", r.logs.String())
	}
}

func TestInstallationEvents(t *testing.T) {
	r := newRig(t)
	installation := `{"action":"deleted","installation":{"id":186236249,"account":{"id":5,"login":"eh-homelab"}},"repositories":[{"id":1,"full_name":"eh-homelab/a"},{"id":2,"full_name":"eh-homelab/b"}]}`
	repos := `{"action":"removed","installation":{"id":186236249},"repository_selection":"selected","repositories_added":[],"repositories_removed":[{"id":31,"full_name":"eh-homelab/c"},{"id":32,"full_name":"eh-homelab/d"}]}`
	for _, tc := range []struct{ event, body string }{{"installation", installation}, {"installation_repositories", repos}} {
		if w := r.send(http.MethodPost, tc.event, sign(tc.body), strings.NewReader(tc.body)); w.Code != http.StatusNoContent {
			t.Errorf("%s: %d %s", tc.event, w.Code, w.Body.String())
		}
	}
	want := map[string]int64{"installation deleted": 1, "installation_repositories removed": 1}
	got := r.events(t)
	if len(got) != len(want) || got["installation deleted"] != 1 || got["installation_repositories removed"] != 1 {
		t.Errorf("counted %v, want %v", got, want)
	}
	logs := r.logs.String()
	for _, s := range []string{"installation_id=186236249", "repositories=2", "repositories_removed=2", "removed_ids=\"[31 32]\"", "delivery=d-1"} {
		if !strings.Contains(logs, s) {
			t.Errorf("the log has no %s:\n%s", s, logs)
		}
	}
	for _, s := range []string{"eh-homelab/", "login", secret} {
		if strings.Contains(logs, s) {
			t.Errorf("the log holds %q:\n%s", s, logs)
		}
	}
}

func TestBadInstallationBody(t *testing.T) {
	r := newRig(t)
	body := `{"action":`
	if w := r.send(http.MethodPost, "installation", sign(body), strings.NewReader(body)); w.Code != http.StatusBadRequest {
		t.Errorf("%d", w.Code)
	}
}

func TestOtherEvents(t *testing.T) {
	r := newRig(t)
	// Signed, but not JSON: the handler must not parse it.
	body := "not json at all"
	for _, event := range []string{"ping", "push", "pull_request", ""} {
		if w := r.send(http.MethodPost, event, sign(body), strings.NewReader(body)); w.Code != http.StatusNoContent {
			t.Errorf("%q: %d", event, w.Code)
		}
	}
	if got := r.events(t); len(got) != 0 {
		t.Errorf("counted %v", got)
	}
}

// A signed delivery with no X-GitHub-Event logs one debug line; other events log none.
func TestNoEventHeaderLogsDebug(t *testing.T) {
	logs := &bytes.Buffer{}
	h, err := New([]byte(secret), Options{Logger: slog.New(slog.NewTextHandler(logs, &slog.HandlerOptions{Level: slog.LevelDebug}))})
	if err != nil {
		t.Fatal(err)
	}
	r := rig{h: h, logs: logs}
	body := "{}"
	if w := r.send(http.MethodPost, "ping", sign(body), strings.NewReader(body)); w.Code != http.StatusNoContent {
		t.Fatalf("ping: %d", w.Code)
	}
	if logs.Len() != 0 {
		t.Fatalf("ping logged %q", logs.String())
	}
	if w := r.send(http.MethodPost, "", sign(body), strings.NewReader(body)); w.Code != http.StatusNoContent {
		t.Fatalf("no event: %d", w.Code)
	}
	out := logs.String()
	if strings.Count(out, "\n") != 1 || !strings.Contains(out, "level=DEBUG") || !strings.Contains(out, "delivery=d-1") {
		t.Fatalf("logs = %q", out)
	}
}

func TestBodyCap(t *testing.T) {
	r := newRig(t)
	body := strings.Repeat("a", MaxBody+1)
	if w := r.send(http.MethodPost, "push", sign(body), strings.NewReader(body)); w.Code != http.StatusRequestEntityTooLarge {
		t.Errorf("%d", w.Code)
	}
}

func TestEmptySecret(t *testing.T) {
	if _, err := New(nil, Options{}); err == nil {
		t.Error("no error")
	}
}
