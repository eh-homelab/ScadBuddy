package devices

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/auth"
)

// Review focus 4: a code claimed twice, or after 10 minutes, is a 410 and makes no device.
func TestPairCodeSpentOnce(t *testing.T) {
	h := newHarness(t, auth.Off)
	me := h.person("user:i#me")
	made := h.do("POST", "/api/v1/devices/pairings", me, "")
	expectStatus(t, made, 200)
	url := made.body["code_url"].(string)
	if !strings.HasPrefix(url, "http://127.0.0.1:") || !strings.HasSuffix(url, "/claim") {
		t.Fatalf("code_url %q", url)
	}
	first := h.claim(url, allMethods)
	expectStatus(t, first, 200)
	if !strings.HasPrefix(first.body["token"].(string), TokenPrefix) {
		t.Fatalf("token %v", first.body["token"])
	}
	again := h.claim(url, allMethods)
	expectStatus(t, again, 410)
	expect(t, again.body["reason"], any("code_gone"))

	late := h.do("POST", "/api/v1/devices/pairings", me, "")
	h.clock.add(11 * time.Minute)
	expectStatus(t, h.claim(late.body["code_url"].(string), allMethods), 410)
	expect(t, h.count(`SELECT count(*) FROM devices`), 1)
	// Only hashes are stored.
	code := url[strings.Index(url, "/pairings/")+len("/pairings/") : len(url)-len("/claim")]
	expect(t, h.count(`SELECT count(*) FROM device_pairings WHERE code_hash = convert_to($1, 'UTF8')`, code), 0)
	expect(t, h.count(`SELECT count(*) FROM device_tokens WHERE token_hash = convert_to($1, 'UTF8')`, first.body["token"]), 0)
	expect(t, h.count(`SELECT count(*) FROM device_tokens WHERE token_hash = sha256(convert_to($1, 'UTF8'))`, first.body["token"]), 1)
}

func TestPairUnknownCapabilityRefused(t *testing.T) {
	h := newHarness(t, auth.Off)
	made := h.do("POST", "/api/v1/devices/pairings", "", "")
	got := h.claim(made.body["code_url"].(string), `["nfc.read","location.get"]`)
	expectStatus(t, got, 422)
	expect(t, h.count(`SELECT count(*) FROM devices`), 0)
	// The code was not spent: the app may claim it again with what it has.
	expectStatus(t, h.claim(made.body["code_url"].(string), `["nfc.read"]`), 200)
}

// Each use moves the expiry to now + an hour; an hour and a minute of no use ends it.
// (The plan's third step, t0+59m+61m, falls past the expiry its second step set, which is
// t0+59m+60m; this test uses 59 m, which only a sliding expiry lets through.)
func TestTokenIdleExpiry(t *testing.T) {
	h := newHarness(t, auth.Off)
	d := h.pair("")
	ctx := context.Background()
	use := func() error { _, err := h.store.Authenticate(ctx, "Bearer "+d.token); return err }
	if err := use(); err != nil {
		t.Fatal(err)
	}
	h.clock.add(59 * time.Minute)
	if err := use(); err != nil {
		t.Fatalf("at 59 m: %v", err)
	}
	h.clock.add(59 * time.Minute)
	if err := use(); err != nil {
		t.Fatalf("at 118 m, 59 m after the last use: %v", err)
	}
	h.clock.add(61 * time.Minute)
	if err := use(); !errors.Is(err, ErrUnauthorized) {
		t.Fatalf("after 61 m of no use: %v", err)
	}
	expectStatus(t, h.do("POST", "/api/v1/devices/heartbeat", d.token, ""), 401)
}

func TestTokenHardCap(t *testing.T) {
	h := newHarness(t, auth.Off)
	d := h.pair("")
	ctx := context.Background()
	for i := 0; i < 30*48-1; i++ {
		h.clock.add(30 * time.Minute)
		if _, err := h.store.Authenticate(ctx, "Bearer "+d.token); err != nil {
			t.Fatalf("after %v: %v", time.Duration(i+1)*30*time.Minute, err)
		}
	}
	h.clock.add(30 * time.Minute)
	if _, err := h.store.Authenticate(ctx, "Bearer "+d.token); !errors.Is(err, ErrUnauthorized) {
		t.Fatalf("at 30 days: %v", err)
	}
}

// The device routes check the token in every mode: in observe a bad one is a 401, never
// served as anonymous.
func TestDeviceRoutesRefuseInObserve(t *testing.T) {
	h := newHarness(t, auth.Observe)
	bad := TokenPrefix + strings.Repeat("A", 43)
	expectStatus(t, h.do("POST", "/api/v1/devices/pairings/"+strings.Repeat("A", 22)+"/claim", "",
		`{"name":"x","capabilities":["nfc.read"]}`), 401)
	expectStatus(t, h.do("POST", "/api/v1/devices/heartbeat", bad, ""), 401)
	expectStatus(t, h.do("POST", "/api/v1/devices/heartbeat", "", ""), 401)
	expectStatus(t, h.do("POST", "/api/v1/devices/calls/"+uuidZero+"/events", bad, `{"events":[]}`), 401)
	expectStatus(t, h.do("POST", "/api/v1/devices/calls/"+uuidZero+"/result", bad, `{"outcome":"error"}`), 401)
	expectStatus(t, h.do("POST", "/api/v1/devices/uploads?call="+uuidZero+"&kind=tag_dump", bad, `{}`), 401)
	_, res := h.pods[0].dial(map[string]string{"Authorization": "Bearer " + bad})
	expect(t, res.StatusCode, 401)
	// In enforce too: the app holds no person's login.
	e := newHarness(t, auth.Enforce)
	made := e.do("POST", "/api/v1/devices/pairings", e.person("user:i#me"), "")
	expectStatus(t, made, 200)
	d := e.claim(made.body["code_url"].(string), allMethods)
	expectStatus(t, d, 200)
	expectStatus(t, e.do("POST", "/api/v1/devices/heartbeat", d.body["token"].(string), ""), 204)
	expectStatus(t, e.do("POST", "/api/v1/devices/pairings", "", ""), 401)
}

const uuidZero = "00000000-0000-4000-8000-000000000000"

func TestListOnlyOwn(t *testing.T) {
	h := newHarness(t, auth.Observe)
	me, you := h.person("user:i#me"), h.person("user:i#you")
	mine := h.pair(me)
	yours := h.pair(you)
	list := func(who string) []any {
		got := h.do("GET", "/api/v1/devices", who, "")
		expectStatus(t, got, 200)
		return got.body["devices"].([]any)
	}
	l := list(me)
	expect(t, len(l), 1)
	row := l[0].(map[string]any)
	expect(t, row["id"], any(mine.id))
	expect(t, row["online"], any(true))
	expect(t, row["name"], any("Pixel"))
	for _, k := range []string{"model", "app_version", "capabilities", "created_at", "last_seen_at", "expires_at", "hard_cap_at"} {
		if _, ok := row[k]; !ok {
			t.Errorf("no %s in %v", k, row)
		}
	}
	expect(t, list(you)[0].(map[string]any)["id"], any(yours.id))
	expect(t, len(list("")), 0)
	// Another person cannot revoke it.
	expectStatus(t, h.do("DELETE", "/api/v1/devices/"+mine.id, you, ""), 404)
	expectStatus(t, h.do("DELETE", "/api/v1/devices/"+mine.id, me, ""), 204)
	expect(t, len(list(me)), 0)
	expectStatus(t, h.do("POST", "/api/v1/devices/heartbeat", mine.token, ""), 401)
}
