package main

import (
	"net/http"
	"testing"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/auth"
)

// The devices area (#8244) through newRouter: named, its routes are Go's; not named, Python's.
func TestTheDevicesAreaIsServedWhenNamed(t *testing.T) {
	csrf := func(r *http.Request) { r.Header.Set(auth.RequestHeader, "1") }
	a := newAPI(t, "devices")
	made := a.do("POST", "/api/v1/devices/pairings", "", csrf)
	expect(t, made.status, 200)
	expect(t, a.do("GET", "/api/v1/devices", "", csrf).raw, `{"devices":[]}`)
	expect(t, a.do("POST", "/api/v1/devices/heartbeat", "").status, 401)

	off := newAPI(t, "")
	expect(t, off.do("POST", "/api/v1/devices/pairings", "", csrf).status, http.StatusTeapot)
}
