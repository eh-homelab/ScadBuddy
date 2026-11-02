package api

import (
	"bytes"
	"context"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
)

// A personal access token (ScadBuddy #4975 phase 6) goes as a bearer on the generated
// calls and the raw downloads alike, and never over plain http.
func TestTokenIsSentAsABearer(t *testing.T) {
	var mu sync.Mutex
	var seen []string
	note := func(r *http.Request) {
		mu.Lock()
		seen = append(seen, r.Header.Get("Authorization"))
		mu.Unlock()
	}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /api/v1/print/library", func(w http.ResponseWriter, r *http.Request) {
		note(r)
		writeJSON(w, http.StatusOK, map[string]any{"folders": []any{}, "files": []any{}})
	})
	mux.HandleFunc("GET /api/v1/print/library/7/file", func(w http.ResponseWriter, r *http.Request) {
		note(r)
		_, _ = w.Write([]byte("x"))
	})
	srv := httptest.NewTLSServer(mux)
	t.Cleanup(srv.Close)

	c, err := New(srv.URL+"/api/v1", srv.Client(), WithToken(" sbpat_0123456789abcdef.secret\n"))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := c.List(context.Background(), 0, false); err != nil {
		t.Fatal(err)
	}
	if err := c.Download(context.Background(), 7, &bytes.Buffer{}); err != nil {
		t.Fatal(err)
	}
	want := "Bearer sbpat_0123456789abcdef.secret"
	if len(seen) != 2 || seen[0] != want || seen[1] != want {
		t.Fatalf("Authorization headers %q", seen)
	}

	if _, err := New("http://scadbuddy.lan/api/v1", nil, WithToken("t")); err == nil {
		t.Fatal("a token was accepted for plain http")
	}
	if _, err := New("http://scadbuddy.lan/api/v1", nil); err != nil {
		t.Fatalf("without a token plain http is fine: %v", err)
	}
}
