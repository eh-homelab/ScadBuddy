package discover

import (
	"encoding/json"
	"os"
	"strings"
	"testing"
)

// The registry's limits (#8590), held to testdata/registry-vectors.json, which core's
// registry tests read too.

type vectorGroup struct {
	Fields  []string
	OK, Bad []json.RawMessage
}

type registryVectors struct {
	SessionID struct{ OK, Bad []json.RawMessage } `json:"session_id"`
	Machine   []vectorGroup
	Session   []vectorGroup
}

// vectorValue is a vector's value: a string, a number, or {prefix, repeat, n, suffix}.
func vectorValue(t *testing.T, raw json.RawMessage) any {
	t.Helper()
	var s string
	if json.Unmarshal(raw, &s) == nil {
		return s
	}
	var n int
	if json.Unmarshal(raw, &n) == nil {
		return n
	}
	var r struct {
		Prefix, Repeat, Suffix string
		N                      int
	}
	if err := json.Unmarshal(raw, &r); err != nil || r.Repeat == "" {
		t.Fatalf("bad vector %s", raw)
	}
	return r.Prefix + strings.Repeat(r.Repeat, r.N) + r.Suffix
}

func readRegistryVectors(t *testing.T) registryVectors {
	t.Helper()
	raw, err := os.ReadFile("../../testdata/registry-vectors.json")
	if err != nil {
		t.Fatal(err)
	}
	var v registryVectors
	if err := json.Unmarshal(raw, &v); err != nil {
		t.Fatal(err)
	}
	if len(v.SessionID.OK) == 0 || len(v.Machine) == 0 || len(v.Session) == 0 {
		t.Fatal("registry-vectors.json lacks a list")
	}
	return v
}

// cleaners are what Scan, GitFacts, AccountUUID and ReadMachine send for each field core
// checks.
var cleaners = map[string]func(string) string{
	"session_id":      sessionID,
	"hostname":        cleanHostname,
	"os":              cleanOS,
	"os_user":         cleanOSUser,
	"service_version": cleanVersion,
	"proc_start":      cleanProcStart,
	"cwd":             absolute,
	"config_dir":      absolute,
	"transcript_path": absolute,
	"worktree_root":   absolute,
	"git_remote":      CleanRemote,
	"git_branch":      cleanBranch,
	"git_head":        cleanHead,
	"cc_version":      cleanVersion,
	"kind":            cleanLabel,
	"entrypoint":      cleanLabel,
	"status":          cleanLabel,
	"name":            cleanName,
	"account_uuid":    cleanAccount,
}

// check holds field's cleaner to the vectors: an ok value is sent as it is, and a bad one is
// never sent as it is (it is left out, or cleaned into another value).
func check(t *testing.T, field string, ok, bad []json.RawMessage) {
	t.Helper()
	if field == "pid" {
		for _, raw := range ok {
			if n := vectorValue(t, raw).(int); !validPID(n) {
				t.Errorf("pid %d is refused, but core takes it", n)
			}
		}
		for _, raw := range bad {
			if n := vectorValue(t, raw).(int); validPID(n) {
				t.Errorf("pid %d is sent, but core refuses it", n)
			}
		}
		return
	}
	clean, found := cleaners[field]
	if !found {
		t.Errorf("no cleaner for %s", field)
		return
	}
	for _, raw := range ok {
		v := vectorValue(t, raw).(string)
		if got := clean(v); got != v {
			t.Errorf("%s %q is sent as %q, but core takes it as it is", field, short(v), short(got))
		}
	}
	for _, raw := range bad {
		v := vectorValue(t, raw).(string)
		if got := clean(v); got != "" && got == v {
			t.Errorf("%s %q is sent as it is, but core refuses it", field, short(v))
		}
	}
}

func short(s string) string {
	if r := []rune(s); len(r) > 40 {
		return string(r[:40]) + "…"
	}
	return s
}

func TestTheCleanersHoldToTheRegistryVectors(t *testing.T) {
	v := readRegistryVectors(t)
	check(t, "session_id", v.SessionID.OK, v.SessionID.Bad)
	seen := map[string]bool{"session_id": true}
	for _, g := range append(append([]vectorGroup{}, v.Machine...), v.Session...) {
		for _, f := range g.Fields {
			seen[f] = true
			check(t, f, g.OK, g.Bad)
		}
	}
	// Every field fleetd sends has vectors.
	for f := range cleaners {
		if !seen[f] {
			t.Errorf("no vectors for %s", f)
		}
	}
	if !seen["pid"] {
		t.Error("no vectors for pid")
	}
}

func TestAMachineWhoseUserCoreWouldRefuseKeepsOne(t *testing.T) {
	dir := t.TempDir()
	write(t, dir+"/machine-id", "0123456789abcdef0123456789abcdef\n")
	m, err := ReadMachine(MachineOptions{MachineIDFile: dir + "/machine-id", OSReleaseFile: dir + "/none",
		User: strings.Repeat("u", 65), Hostname: "h", Version: "1"})
	if err != nil {
		t.Fatal(err)
	}
	if m.OSUser != "unknown" {
		t.Fatalf("os_user %q", m.OSUser)
	}
}
