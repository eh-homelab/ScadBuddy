// Package discover finds the live Claude Code sessions on this machine and the facts the
// session registry keeps about each (spec 2026-11-26-fleet-session-registry-and-rotator-design
// §6.3, §6.6). It reads session files, /proc, file names under projects/, git's answers and
// one field of .credentials.json. It never reads a transcript's contents or a token.
package discover

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"
)

// Row is one live session: the body of
// PUT …/fleet-rotator/machines/{machine_id}/sessions/{session_id}. An empty optional field
// is left out, which core reads as "none". Every field is a string or an int, so two rows
// compare with ==.
type Row struct {
	SessionID      string `json:"-"`
	PID            int    `json:"pid"`
	ProcStart      string `json:"proc_start"`
	Cwd            string `json:"cwd"`
	ConfigDir      string `json:"config_dir"`
	TranscriptPath string `json:"transcript_path,omitempty"`
	GitRemote      string `json:"git_remote,omitempty"`
	GitBranch      string `json:"git_branch,omitempty"`
	WorktreeRoot   string `json:"worktree_root,omitempty"`
	GitHead        string `json:"git_head,omitempty"`
	CCVersion      string `json:"cc_version,omitempty"`
	Kind           string `json:"kind,omitempty"`
	Entrypoint     string `json:"entrypoint,omitempty"`
	Name           string `json:"name,omitempty"`
	Status         string `json:"status,omitempty"`
	AccountUUID    string `json:"account_uuid,omitempty"`
}

// Options say where to look.
type Options struct {
	// ConfigDirs are Claude Code config dirs, such as ~/.claude.
	ConfigDirs []string
	// ProcRoot is /proc; a test gives a fake one.
	ProcRoot string
	// GitTimeout bounds each git command.
	GitTimeout time.Duration
}

// The limits and forms core's registry accepts (tools/go/core/internal/fleet/registry.go,
// putMachine and putSession). A value that would fail them is left out, so one odd field
// never loses the whole row. ../../testdata/registry-vectors.json holds values core takes
// and values it refuses; core's registry tests and this package's tests both read it, so the
// cleaners below and core's checks cannot drift apart.
var (
	uuidPattern    = regexp.MustCompile(`^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$`)
	controlPattern = regexp.MustCompile(`[\x00-\x1f\x7f\x{80}-\x{9f}]`)
	versionPattern = regexp.MustCompile(`^[0-9A-Za-z.+_-]{1,64}$`)
	procPattern    = regexp.MustCompile(`^[0-9]{1,20}$`)
	headPattern    = regexp.MustCompile(`^[0-9a-f]{40}([0-9a-f]{24})?$`)
	scpPattern     = regexp.MustCompile(`^[A-Za-z0-9._-]+@[A-Za-z0-9_][A-Za-z0-9.-]*:[^-:]`)
)

const (
	pathMax = 4096
	pidMax  = 1 << 22
)

// plain is s when it is a text of at most max characters with no control character, else "".
func plain(s string, max int) string {
	if s == "" || len([]rune(s)) > max || controlPattern.MatchString(s) || !strings.ContainsFunc(s, func(r rune) bool { return r != ' ' }) {
		return ""
	}
	return s
}

// absolute is p when it is an absolute path core accepts, else "".
func absolute(p string) string {
	if !strings.HasPrefix(p, "/") || len(p) > pathMax || controlPattern.MatchString(p) {
		return ""
	}
	return p
}

func match(re *regexp.Regexp, s string) string {
	if re.MatchString(s) {
		return s
	}
	return ""
}

// The cleaners: each is a field's value as fleetd sends it, or "" when core would refuse it.
// A required field that cleans to "" loses the row (a session) or gets a stand-in (a
// machine's).

func sessionID(s string) string      { return strings.ToLower(match(uuidPattern, s)) }
func validPID(pid int) bool          { return pid >= 1 && pid <= pidMax }
func cleanProcStart(s string) string { return match(procPattern, s) }
func cleanBranch(s string) string    { return plain(s, 255) }
func cleanHead(s string) string      { return match(headPattern, s) }
func cleanVersion(s string) string   { return match(versionPattern, s) }
func cleanLabel(s string) string     { return plain(s, 40) } // kind, entrypoint and status
func cleanName(s string) string      { return plain(s, 200) }
func cleanAccount(s string) string   { return strings.ToLower(match(uuidPattern, s)) }
func cleanHostname(s string) string  { return plain(s, 253) }
func cleanOS(s string) string        { return plain(s, 100) }
func cleanOSUser(s string) string    { return plain(s, 64) }

// MachineID is the hex SHA-256 of the machine id in path (/etc/machine-id) and the OS user,
// joined by ':' (a user name never holds one). The raw id never leaves this function.
func MachineID(path, user string) (string, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return "", err
	}
	id := strings.TrimSpace(string(raw))
	if id == "" {
		return "", errors.New("the machine id file is empty")
	}
	if user == "" {
		return "", errors.New("no OS user name")
	}
	sum := sha256.Sum256([]byte(id + ":" + user))
	return hex.EncodeToString(sum[:]), nil
}

// Live says whether pid runs and started at procStart: field 22 of /proc/<pid>/stat,
// so a reused pid is not taken for the session.
func Live(procRoot string, pid int, procStart string) bool {
	if pid <= 0 || procStart == "" {
		return false
	}
	b, err := os.ReadFile(filepath.Join(procRoot, strconv.Itoa(pid), "stat"))
	if err != nil {
		return false
	}
	// Field 2, comm, is in parentheses and may hold spaces and ')': the fields after the
	// last ')' start at field 3.
	i := bytes.LastIndexByte(b, ')')
	if i < 0 {
		return false
	}
	fields := strings.Fields(string(b[i+1:]))
	const start = 22 - 3
	return len(fields) > start && fields[start] == procStart
}

// Slug is Claude Code's name for a cwd's directory under projects/: every character that
// is not an ASCII letter or digit becomes '-' (Claude Code 2.1.332's code, read
// 2026-11-26; it matches every name under this machine's ~/.claude/projects).
func Slug(cwd string) string {
	b := []byte(cwd)
	for i, c := range b {
		if !('a' <= c && c <= 'z' || 'A' <= c && c <= 'Z' || '0' <= c && c <= '9') {
			b[i] = '-'
		}
	}
	// A multi-byte character is one '-' per UTF-16 unit in JavaScript; this module sends
	// only what it finds on disk, so a mismatch only leaves the path out.
	return string(b)
}

// slugMax is where Claude Code cuts a long slug and adds '-' and a hash of the cwd.
const slugMax = 200

// TranscriptPath is <configDir>/projects/<slug>/<sessionID>.jsonl when that file exists,
// else "". A slug longer than slugMax is found by its cut prefix. Only names are read.
func TranscriptPath(configDir, cwd, sessionID string) string {
	projects := filepath.Join(configDir, "projects")
	name := sessionID + ".jsonl"
	slug := Slug(cwd)
	if len(slug) <= slugMax {
		p := filepath.Join(projects, slug, name)
		if isFile(p) {
			return p
		}
		return ""
	}
	entries, err := os.ReadDir(projects)
	if err != nil {
		return ""
	}
	prefix := slug[:slugMax] + "-"
	for _, e := range entries {
		if e.IsDir() && strings.HasPrefix(e.Name(), prefix) {
			if p := filepath.Join(projects, e.Name(), name); isFile(p) {
				return p
			}
		}
	}
	return ""
}

func isFile(p string) bool {
	st, err := os.Stat(p)
	return err == nil && st.Mode().IsRegular()
}

// AccountUUID is claudeAiOauth.accountUuid in <configDir>/.credentials.json, or "". Only
// that field is decoded; the tokens beside it are skipped by the decoder. Nothing here
// logs.
func AccountUUID(configDir string) string {
	b, err := os.ReadFile(filepath.Join(configDir, ".credentials.json"))
	if err != nil {
		return ""
	}
	var f struct {
		ClaudeAiOauth struct {
			AccountUUID string `json:"accountUuid"`
		} `json:"claudeAiOauth"`
	}
	if json.Unmarshal(b, &f) != nil {
		return ""
	}
	return cleanAccount(f.ClaudeAiOauth.AccountUUID)
}

// Git is a cwd's git facts; empty outside a repo.
type Git struct {
	Remote, Branch, Root, Head string
}

// GitFacts runs git in dir, each command bounded by timeout: the worktree root, HEAD, the
// branch (none when detached) and origin's URL (else the first remote's), cleaned by
// CleanRemote.
func GitFacts(ctx context.Context, dir string, timeout time.Duration) Git {
	var g Git
	out, ok := git(ctx, dir, timeout, "rev-parse", "--show-toplevel", "HEAD", "--abbrev-ref", "HEAD")
	if lines := strings.Split(out, "\n"); ok && len(lines) == 3 {
		g.Root, g.Head = lines[0], lines[1]
		if lines[2] != "HEAD" {
			g.Branch = lines[2]
		}
	} else if out, ok := git(ctx, dir, timeout, "rev-parse", "--show-toplevel"); ok {
		// A repo with no commit yet has no HEAD.
		g.Root = out
	} else {
		return Git{}
	}
	if out, ok := git(ctx, dir, timeout, "config", "--get-regexp", `^remote\..*\.url$`); ok {
		var first string
		for _, line := range strings.Split(out, "\n") {
			key, value, _ := strings.Cut(line, " ")
			if key == "remote.origin.url" {
				first = value
				break
			}
			if first == "" {
				first = value
			}
		}
		g.Remote = CleanRemote(first)
	}
	g.Root = absolute(g.Root)
	g.Head = cleanHead(g.Head)
	g.Branch = cleanBranch(g.Branch)
	return g
}

func git(ctx context.Context, dir string, timeout time.Duration, args ...string) (string, bool) {
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	cmd := exec.CommandContext(ctx, "git", append([]string{"-C", dir}, args...)...)
	// No prompt, no lock taken for a read, and C messages.
	cmd.Env = append(os.Environ(), "GIT_TERMINAL_PROMPT=0", "GIT_OPTIONAL_LOCKS=0", "LC_ALL=C")
	cmd.WaitDelay = time.Second
	out, err := cmd.Output()
	if err != nil {
		return "", false
	}
	return strings.TrimSpace(string(out)), true
}

// CleanRemote is remote with no secret, in a form core keeps, or "" when it has none. A
// URL loses its user info, query and fragment, and must be http(s), ssh or git. The
// scp-like user@host:path keeps its user name (such as git@), which core requires and
// which holds no password; one with a password is dropped.
func CleanRemote(remote string) string {
	if remote == "" || strings.Contains(remote, `\`) {
		return ""
	}
	if !strings.Contains(remote, "://") {
		if scpPattern.MatchString(remote) && !strings.ContainsAny(remote, "?#") {
			return plain(remote, 2048)
		}
		return ""
	}
	u, err := url.Parse(remote)
	if err != nil || u.Opaque != "" || u.Host == "" || strings.HasPrefix(u.Host, "-") {
		return ""
	}
	switch u.Scheme {
	case "http", "https", "ssh", "git":
	default:
		return ""
	}
	u.User, u.RawQuery, u.ForceQuery, u.Fragment, u.RawFragment = nil, "", false, "", ""
	return plain(u.String(), 2048)
}

// procStart is a session file's procStart: a string of digits (seen on disk), or a number.
type procStart string

func (p *procStart) UnmarshalJSON(b []byte) error {
	var s string
	if json.Unmarshal(b, &s) == nil {
		*p = procStart(s)
		return nil
	}
	var n json.Number
	if err := json.Unmarshal(b, &n); err != nil {
		return err
	}
	*p = procStart(n.String())
	return nil
}

// sessionFile is the part of <config dir>/sessions/<pid>.json this service reads.
type sessionFile struct {
	PID        int       `json:"pid"`
	SessionID  string    `json:"sessionId"`
	Cwd        string    `json:"cwd"`
	ProcStart  procStart `json:"procStart"`
	Version    string    `json:"version"`
	Kind       string    `json:"kind"`
	Entrypoint string    `json:"entrypoint"`
	Name       string    `json:"name"`
	Status     string    `json:"status"`
}

// Scan reads every config dir's sessions and returns a row for each live one, ordered by
// session id. A file it cannot read or parse is skipped.
func Scan(ctx context.Context, o Options) []Row {
	var rows []Row
	for _, dir := range o.ConfigDirs {
		account := AccountUUID(dir)
		files, _ := filepath.Glob(filepath.Join(dir, "sessions", "*.json"))
		for _, path := range files {
			b, err := os.ReadFile(path)
			if err != nil {
				continue
			}
			var s sessionFile
			if json.Unmarshal(b, &s) != nil {
				continue
			}
			start := cleanProcStart(string(s.ProcStart))
			cwd := absolute(s.Cwd)
			id := sessionID(s.SessionID)
			if id == "" || !validPID(s.PID) || start == "" ||
				cwd == "" || absolute(dir) == "" || !Live(o.ProcRoot, s.PID, start) {
				continue
			}
			g := GitFacts(ctx, cwd, o.GitTimeout)
			rows = append(rows, Row{
				SessionID:      id,
				PID:            s.PID,
				ProcStart:      start,
				Cwd:            cwd,
				ConfigDir:      dir,
				TranscriptPath: absolute(TranscriptPath(dir, s.Cwd, s.SessionID)),
				GitRemote:      g.Remote,
				GitBranch:      g.Branch,
				WorktreeRoot:   g.Root,
				GitHead:        g.Head,
				CCVersion:      cleanVersion(s.Version),
				Kind:           cleanLabel(s.Kind),
				Entrypoint:     cleanLabel(s.Entrypoint),
				Name:           cleanName(s.Name),
				Status:         cleanLabel(s.Status),
				AccountUUID:    account,
			})
		}
	}
	sort.Slice(rows, func(i, j int) bool {
		if rows[i].SessionID != rows[j].SessionID {
			return rows[i].SessionID < rows[j].SessionID
		}
		return rows[i].ConfigDir < rows[j].ConfigDir
	})
	return rows
}
