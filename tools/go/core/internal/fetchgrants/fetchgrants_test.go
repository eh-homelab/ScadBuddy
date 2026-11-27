package fetchgrants

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/auth"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/seal"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/testdb"
)

func TestMain(m *testing.M) { testdb.Main(m) }

var kek = seal.NewKEK(bytes.Repeat([]byte{0x42}, 32))

func serve(t *testing.T, keys *seal.Keys) func(body string) (int, string) {
	t.Helper()
	pool := testdb.New(t)
	router := &crud.Router{Resolver: &auth.Resolver{Pool: pool}, Next: http.NotFoundHandler()}
	router.Add("agent", Areas(pool, keys)["keys"]...)
	server := httptest.NewServer(router)
	t.Cleanup(server.Close)
	id, secret := make([]byte, 8), make([]byte, 32)
	_, _ = rand.Read(id)
	_, _ = rand.Read(secret)
	token := "sbsvc_" + hex.EncodeToString(id) + "." + base64.RawURLEncoding.EncodeToString(secret)
	hash := sha256.Sum256([]byte(token))
	if _, err := pool.Exec(context.Background(), `INSERT INTO service_credentials (id, caller,
		scopes, secret_hash, expires_at, proof) VALUES ($1, 'agent', '{api,crud.agent}', $2,
		now() + interval '15 minutes', 'inprocess')`, hex.EncodeToString(id), hash[:]); err != nil {
		t.Fatal(err)
	}
	return func(body string) (int, string) {
		req, _ := http.NewRequest("POST", server.URL+"/api/v1/internal/crud/agent/fetch-grants",
			strings.NewReader(body))
		req.Header.Set("Authorization", "Bearer "+token)
		res, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		defer res.Body.Close()
		raw, _ := io.ReadAll(res.Body)
		return res.StatusCode, string(raw)
	}
}

func TestAGrantNamesTheHostTheURLAndTenMinutes(t *testing.T) {
	post := serve(t, &seal.Keys{Current: kek})
	status, raw := post(`{"host":"example.com","url":"https://Example.com/a.stl"}`)
	if status != 200 {
		t.Fatalf("%d %s", status, raw)
	}
	var out struct{ Grant string }
	_ = json.Unmarshal([]byte(raw), &out)
	encodedBody, encodedMAC, _ := strings.Cut(out.Grant, ".")
	body, _ := base64.RawURLEncoding.DecodeString(encodedBody)
	mac, _ := base64.RawURLEncoding.DecodeString(encodedMAC)
	check := hmac.New(sha256.New, Key(kek))
	check.Write(body)
	if !hmac.Equal(mac, check.Sum(nil)) {
		t.Fatal("the grant is not MACed under the fetch-grant key")
	}
	fields := strings.Split(string(body), "\n")
	digest := sha256.Sum256([]byte("https://Example.com/a.stl"))
	expires, _ := strconv.ParseInt(fields[3], 10, 64)
	if len(fields) != 5 || fields[0] != "asset-fetch-once" || fields[1] != "example.com" ||
		fields[2] != hex.EncodeToString(digest[:]) || len(fields[4]) != 32 ||
		expires-time.Now().Unix() < 595 || expires-time.Now().Unix() > 600 {
		t.Fatalf("grant body %q", body)
	}
}

func TestABadAskIsRefused(t *testing.T) {
	post := serve(t, &seal.Keys{Current: kek})
	for _, c := range []struct{ body, want string }{
		{`{"host":"evil.com","url":"https://example.com/a"}`, `"loc":["body"]`},
		{`{"host":"example.com","url":"http://example.com/a"}`, `"loc":["body"]`},
		{`{"host":"example.com","url":"https://user@example.com/a"}`, `"loc":["body"]`},
		{`{"host":"","url":"https://example.com/a"}`, `"loc":["body","host"]`},
		{`{"host":"example.com"}`, `"loc":["body","url"]`},
		{`{"host":"example.com","url":"https://example.com/a","x":1}`, `"loc":["body","x"]`},
	} {
		if status, raw := post(c.body); status != 422 || !strings.Contains(raw, c.want) {
			t.Errorf("%s: %d %s", c.body, status, raw)
		}
	}
}

func TestNoKEKIsKEKMissing(t *testing.T) {
	post := serve(t, nil)
	if status, raw := post(`{"host":"example.com","url":"https://example.com/a"}`); status != 503 ||
		!strings.Contains(raw, `"reason":"kek-missing"`) {
		t.Fatalf("%d %s", status, raw)
	}
}
