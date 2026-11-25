package front

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestParseNetworksAsProxiesPy(t *testing.T) {
	for _, bad := range []string{"10.0.0.0/", "x", "fe80::1%eth0", "10.0.0.0/33", "::ffff:10.0.0.0/95", "10.0.0.1/+8"} {
		if _, err := ParseNetworks(bad); err == nil {
			t.Errorf("%q was taken", bad)
		}
	}
	ns, err := ParseNetworks(" 10.0.0.0/8, fd00::/8,192.168.1.10, ::ffff:172.16.0.0/108 ,")
	if err != nil || len(ns) != 4 {
		t.Fatal(ns, err)
	}
	for addr, want := range map[string]bool{
		"10.2.3.4": true, "::ffff:10.2.3.4": true, "fd00::1": true, "192.168.1.10": true,
		"192.168.1.11": false, "172.16.1.1": true, "172.32.0.1": false, "x": false,
	} {
		if ns.contains(addr) != want {
			t.Errorf("%s: %v", addr, !want)
		}
	}
}

func TestTheClientIsBelievedOnlyFromATrustedPeer(t *testing.T) {
	ns, _ := ParseNetworks("10.0.0.0/8")
	req := func(peer string, xff ...string) *http.Request {
		r := httptest.NewRequest("GET", "/", nil)
		r.RemoteAddr = peer
		for _, v := range xff {
			r.Header.Add("X-Forwarded-For", v)
		}
		return r
	}
	for _, c := range []struct {
		r    *http.Request
		want string
	}{
		{req("10.1.1.1:5", "203.0.113.9, 198.51.100.2"), "198.51.100.2"},
		{req("10.1.1.1:5", "1.1.1.1", "198.51.100.3"), "198.51.100.3"},
		{req("10.1.1.1:5", "not-an-ip"), "10.1.1.1"},
		{req("10.1.1.1:5"), "10.1.1.1"},
		{req("203.0.113.1:5", "198.51.100.2"), "203.0.113.1"},
		{req("[::ffff:10.1.1.1]:5", "::ffff:198.51.100.2"), "198.51.100.2"},
	} {
		if got := ns.Client(c.r); got != c.want {
			t.Errorf("%s %v: %s, want %s", c.r.RemoteAddr, c.r.Header["X-Forwarded-For"], got, c.want)
		}
	}
}

func TestTheCRUDPortServesOnlyItsPaths(t *testing.T) {
	h := CRUDOnly(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(204) }))
	for p, want := range map[string]int{
		"/api/v1/internal/crud/render/settings": 204, "/healthz": 204, "/api/v1/auth/service-token": 204,
		"/api/v1/models": 404, "/api/v1/internal/crud/../models": 404, "/api/v1/internal/crud//x": 404,
		"/api/v1/internal/crud/x/": 404, "/healthz/": 404,
	} {
		w := httptest.NewRecorder()
		r := httptest.NewRequest("GET", "http://x"+p, nil)
		r.URL.Path = p
		h.ServeHTTP(w, r)
		if w.Code != want {
			t.Errorf("%s: %d", p, w.Code)
		}
	}
	w := httptest.NewRecorder()
	r := httptest.NewRequest("GET", "/healthz", nil)
	r.Header.Set("Upgrade", "websocket")
	if h.ServeHTTP(w, r); w.Code != 404 {
		t.Error("an upgrade reached the CRUD port")
	}
}
