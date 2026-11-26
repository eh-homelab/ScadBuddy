package discover

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"
)

// Dummy values only: no test reads the real ~/.claude, .credentials.json or /etc/machine-id.
const (
	dummyAccess  = "sk-ant-oat01-DUMMY-ACCESS-TOKEN"
	dummyRefresh = "sk-ant-ort01-DUMMY-REFRESH-TOKEN"
	dummyOrg     = "0b4a1d2e-0000-4000-8000-00000000beef"
	dummyAccount = "11111111-2222-4333-8444-555555555555"
	sessionLive  = "aaaaaaaa-1111-4111-8111-111111111111"
	sessionDead  = "bbbbbbbb-2222-4222-8222-222222222222"
)

func write(t *testing.T, path, body string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
}

// fakeStat is a /proc/<pid>/stat line whose field 22 is start; comm holds a space and a
// ')' so the parser must find the last one.
func fakeStat(pid int, start string) string {
	fields := []string{strconv.Itoa(pid), "(claude) x)", "S"}
	for i := 4; i <= 52; i++ {
		v := strconv.Itoa(i * 10)
		if i == 22 {
			v = start
		}
		fields = append(fields, v)
	}
	return strings.Join(fields, " ") + "\n"
}

func TestLive(t *testing.T) {
	proc := t.TempDir()
	write(t, filepath.Join(proc, "4242", "stat"), fakeStat(4242, "97360477"))
	cases := []struct {
		pid   int
		start string
		want  bool
	}{
		{4242, "97360477", true},
		{4242, "97360478", false}, // the pid was reused
		{4343, "97360477", false}, // no such process
		{0, "97360477", false},
		{4242, "", false},
	}
	for _, c := range cases {
		if got := Live(proc, c.pid, c.start); got != c.want {
			t.Errorf("Live(%d, %q) = %v, want %v", c.pid, c.start, got, c.want)
		}
	}
}

func TestSlug(t *testing.T) {
	// Real directory names under ~/.claude/projects (2026-11-26): every character that is
	// not a letter or a digit becomes '-'.
	cases := map[string]string{
		"/home/elan/repos/eh-homelab/ScadBuddy":                   "-home-elan-repos-eh-homelab-ScadBuddy",
		"/home/elan/repos/eh-homelab/ScadBuddy/.claude/worktrees": "-home-elan-repos-eh-homelab-ScadBuddy--claude-worktrees",
		"/tmp/a_b c+d": "-tmp-a-b-c-d",
	}
	for in, want := range cases {
		if got := Slug(in); got != want {
			t.Errorf("Slug(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestTranscriptPath(t *testing.T) {
	cfg := t.TempDir()
	cwd := "/home/u/repo/.claude/worktrees/x"
	want := filepath.Join(cfg, "projects", "-home-u-repo--claude-worktrees-x", sessionLive+".jsonl")
	if got := TranscriptPath(cfg, cwd, sessionLive); got != "" {
		t.Fatalf("no transcript yet, got %q", got)
	}
	write(t, want, "the contents are never read\n")
	if got := TranscriptPath(cfg, cwd, sessionLive); got != want {
		t.Fatalf("got %q, want %q", got, want)
	}

	// A slug longer than 200 is cut and gets a hash suffix; the directory is found by its
	// prefix and the transcript's file name.
	long := "/" + strings.Repeat("d", 250)
	dir := Slug(long)[:200] + "-abc123"
	longWant := filepath.Join(cfg, "projects", dir, sessionLive+".jsonl")
	write(t, longWant, "")
	if got := TranscriptPath(cfg, long, sessionLive); got != longWant {
		t.Fatalf("long: got %q, want %q", got, longWant)
	}
}

func TestMachineID(t *testing.T) {
	f := filepath.Join(t.TempDir(), "machine-id")
	write(t, f, "0123456789abcdef0123456789abcdef\n")
	got, err := MachineID(f, "elan")
	if err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256([]byte("0123456789abcdef0123456789abcdef:elan"))
	if want := hex.EncodeToString(sum[:]); got != want {
		t.Fatalf("got %s, want %s", got, want)
	}
	if strings.Contains(got, "0123456789abcdef") {
		t.Fatal("the raw machine id leaked")
	}
	other, _ := MachineID(f, "root")
	if other == got {
		t.Fatal("two users share a machine id")
	}
	write(t, f, "\n")
	if _, err := MachineID(f, "elan"); err == nil {
		t.Fatal("an empty machine-id was accepted")
	}
}

func credentials(t *testing.T, cfg string) {
	write(t, filepath.Join(cfg, ".credentials.json"), `{"claudeAiOauth":{"accessToken":"`+dummyAccess+
		`","refreshToken":"`+dummyRefresh+`","expiresAt":1,"accountUuid":"`+dummyAccount+
		`","organizationUuid":"`+dummyOrg+`","scopes":["user:inference"],"subscriptionType":"max"}}`)
}

func TestAccountUUID(t *testing.T) {
	cfg := t.TempDir()
	if got := AccountUUID(cfg); got != "" {
		t.Fatalf("no file: got %q", got)
	}
	credentials(t, cfg)
	if got := AccountUUID(cfg); got != dummyAccount {
		t.Fatalf("got %q", got)
	}
	write(t, filepath.Join(cfg, ".credentials.json"), `{"claudeAiOauth":{"accountUuid":"not a uuid"}}`)
	if got := AccountUUID(cfg); got != "" {
		t.Fatalf("a bad uuid passed: %q", got)
	}
}

func TestCleanRemote(t *testing.T) {
	cases := map[string]string{
		"https://x-access-token:ghp_SECRET@github.com/o/r.git": "https://github.com/o/r.git",
		"https://ghp_SECRET@github.com/o/r":                    "https://github.com/o/r",
		"https://github.com/o/r.git?token=SECRET#frag":         "https://github.com/o/r.git",
		"ssh://git:pw@github.com/o/r.git":                      "ssh://github.com/o/r.git",
		"git@github.com:o/r.git":                               "git@github.com:o/r.git",
		"user:pw@github.com:o/r.git":                           "",
		"/srv/git/r.git":                                       "",
		"file:///srv/git/r.git":                                "",
		"ext::sh -c touch% /tmp/x":                             "",
	}
	for in, want := range cases {
		if got := CleanRemote(in); got != want {
			t.Errorf("CleanRemote(%q) = %q, want %q", in, got, want)
		}
	}
}

// gitRepo is a repo with one commit, on branch main, whose origin holds a token.
func gitRepo(t *testing.T) (dir, head string) {
	t.Helper()
	if _, err := exec.LookPath("git"); err != nil {
		t.Skip("no git")
	}
	dir = t.TempDir()
	home := t.TempDir()
	run := func(args ...string) string {
		cmd := exec.Command("git", append([]string{"-C", dir}, args...)...)
		cmd.Env = append(os.Environ(), "HOME="+home, "GIT_CONFIG_NOSYSTEM=1", "GIT_CONFIG_GLOBAL=/dev/null",
			"GIT_AUTHOR_NAME=t", "GIT_AUTHOR_EMAIL=t@example.com", "GIT_COMMITTER_NAME=t", "GIT_COMMITTER_EMAIL=t@example.com")
		out, err := cmd.CombinedOutput()
		if err != nil {
			t.Fatalf("git %v: %v\n%s", args, err, out)
		}
		return strings.TrimSpace(string(out))
	}
	run("init", "-q", "-b", "main")
	run("commit", "-q", "--allow-empty", "-m", "one")
	run("remote", "add", "origin", "https://x-access-token:ghp_DUMMYSECRET@github.com/o/r.git")
	return dir, run("rev-parse", "HEAD")
}

func TestGitFacts(t *testing.T) {
	dir, head := gitRepo(t)
	sub := filepath.Join(dir, "sub")
	if err := os.Mkdir(sub, 0o755); err != nil {
		t.Fatal(err)
	}
	g := GitFacts(context.Background(), sub, 5*time.Second)
	root, _ := filepath.EvalSymlinks(dir)
	if g.Root != root && g.Root != dir {
		t.Errorf("root %q, want %q", g.Root, dir)
	}
	if g.Head != head {
		t.Errorf("head %q, want %q", g.Head, head)
	}
	if g.Branch != "main" {
		t.Errorf("branch %q", g.Branch)
	}
	if g.Remote != "https://github.com/o/r.git" {
		t.Errorf("remote %q: the userinfo must be stripped", g.Remote)
	}

	none := GitFacts(context.Background(), t.TempDir(), 5*time.Second)
	if none != (Git{}) {
		t.Errorf("not a repo: %+v", none)
	}
}

// TestScan builds a fake config dir: one live session in a git repo, one whose pid was
// reused, a session file that is not JSON and a file that is not a session.
func TestScan(t *testing.T) {
	repo, head := gitRepo(t)
	cfg := t.TempDir()
	proc := t.TempDir()
	credentials(t, cfg)
	write(t, filepath.Join(proc, "100", "stat"), fakeStat(100, "5000"))
	write(t, filepath.Join(proc, "200", "stat"), fakeStat(200, "9999"))
	session := func(pid int, id, start string) string {
		b, _ := json.Marshal(map[string]any{
			"pid": pid, "sessionId": id, "cwd": repo, "startedAt": 1786428316046, "procStart": start,
			"version": "2.1.332", "kind": "interactive", "entrypoint": "cli", "name": "fleetd work",
			"status": "busy", "updatedAt": 1788802405689, "messagingSocketPath": "/tmp/sock-SECRETPATH",
			"bridgeSessionId": "bridge-SECRET",
		})
		return string(b)
	}
	write(t, filepath.Join(cfg, "sessions", "100.json"), session(100, sessionLive, "5000"))
	write(t, filepath.Join(cfg, "sessions", "200.json"), session(200, sessionDead, "6000"))
	write(t, filepath.Join(cfg, "sessions", "300.json"), "{not json")
	write(t, filepath.Join(cfg, "sessions", "100.abc.key"), "not a session")
	transcript := filepath.Join(cfg, "projects", Slug(repo), sessionLive+".jsonl")
	write(t, transcript, `{"secret":"never read"}`)

	rows := Scan(context.Background(), Options{ConfigDirs: []string{cfg}, ProcRoot: proc, GitTimeout: 5 * time.Second})
	if len(rows) != 1 {
		t.Fatalf("rows %+v, want only the live session", rows)
	}
	r := rows[0]
	want := Row{
		SessionID: sessionLive, PID: 100, ProcStart: "5000", Cwd: repo, ConfigDir: cfg,
		TranscriptPath: transcript, GitRemote: "https://github.com/o/r.git", GitBranch: "main",
		WorktreeRoot: r.WorktreeRoot, GitHead: head, CCVersion: "2.1.332", Kind: "interactive",
		Entrypoint: "cli", Name: "fleetd work", Status: "busy", AccountUUID: dummyAccount,
	}
	if r != want {
		t.Fatalf("row\n%+v\nwant\n%+v", r, want)
	}
	b, err := json.Marshal(r)
	if err != nil {
		t.Fatal(err)
	}
	for _, leak := range []string{dummyAccess, dummyRefresh, dummyOrg, "SECRET", "max", "never read"} {
		if strings.Contains(string(b), leak) {
			t.Errorf("the row carries %q: %s", leak, b)
		}
	}
	var keys map[string]any
	_ = json.Unmarshal(b, &keys)
	allowed := map[string]bool{"pid": true, "proc_start": true, "cwd": true, "config_dir": true,
		"transcript_path": true, "git_remote": true, "git_branch": true, "worktree_root": true,
		"git_head": true, "cc_version": true, "kind": true, "entrypoint": true, "name": true,
		"status": true, "account_uuid": true}
	for k := range keys {
		if !allowed[k] {
			t.Errorf("the row sends %q", k)
		}
	}
}

func TestReadMachine(t *testing.T) {
	dir := t.TempDir()
	write(t, filepath.Join(dir, "machine-id"), "fedcba9876543210fedcba9876543210\n")
	write(t, filepath.Join(dir, "os-release"), "NAME=\"Ubuntu\"\nPRETTY_NAME=\"Ubuntu 24.04.1 LTS\"\n")
	m, err := ReadMachine(MachineOptions{MachineIDFile: filepath.Join(dir, "machine-id"),
		OSReleaseFile: filepath.Join(dir, "os-release"), User: "elan", Hostname: "spark", Version: "(devel)"})
	if err != nil {
		t.Fatal(err)
	}
	want, _ := MachineID(filepath.Join(dir, "machine-id"), "elan")
	if m.ID != want || m.Hostname != "spark" || m.OSUser != "elan" || m.ServiceVersion != "dev" ||
		!strings.HasSuffix(m.OS, " Ubuntu 24.04.1 LTS") {
		t.Fatalf("%+v", m)
	}
	b, _ := json.Marshal(m)
	if strings.Contains(string(b), "fedcba98") || strings.Contains(string(b), want) {
		t.Fatalf("the body carries an id: %s", b)
	}
}

func TestScanDropsBadValues(t *testing.T) {
	cfg := t.TempDir()
	proc := t.TempDir()
	write(t, filepath.Join(proc, "100", "stat"), fakeStat(100, "5000"))
	write(t, filepath.Join(proc, "101", "stat"), fakeStat(101, "5000"))
	write(t, filepath.Join(proc, "102", "stat"), fakeStat(102, "5000"))
	write(t, filepath.Join(cfg, "sessions", "100.json"),
		`{"pid":100,"sessionId":"`+sessionLive+`","cwd":"/nowhere","procStart":"5000","name":"bad\u0007name","version":"2.1 (x)"}`)
	write(t, filepath.Join(cfg, "sessions", "101.json"),
		`{"pid":101,"sessionId":"not-a-uuid","cwd":"/nowhere","procStart":"5000"}`)
	write(t, filepath.Join(cfg, "sessions", "102.json"),
		`{"pid":102,"sessionId":"`+sessionDead+`","cwd":"relative","procStart":5000}`)
	rows := Scan(context.Background(), Options{ConfigDirs: []string{cfg}, ProcRoot: proc, GitTimeout: time.Second})
	if len(rows) != 1 || rows[0].SessionID != sessionLive {
		t.Fatalf("rows %+v", rows)
	}
	if rows[0].Name != "" || rows[0].CCVersion != "" {
		t.Errorf("bad values were kept: %+v", rows[0])
	}
}
