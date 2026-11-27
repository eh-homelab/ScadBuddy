package claudecreds

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"reflect"
	"sync"
	"time"
)

// The login file and Claude Code's locks around it (design §3 and §6.6, Claude Code
// 2.1.332's code, read 2026-11-26 with `strings` on its binary):
//
//   - `.credentials.json` in the config dir, mode 0600, `{"claudeAiOauth": {...}, ...}`.
//   - A token refresh locks `<dir>/.oauth_refresh.lock` (proper-lockfile with
//     `lockfilePath` set, `stale: 60000`, `update: 5000`), then the legacy lock
//     `<realpath(dir)>.lock` (the same options).
//   - Every write of the file locks `<dir>/.storage-write` with no `lockfilePath`, so the
//     lock directory is `<dir>/.storage-write.lock` (`stale: 15000`, `update` defaults to
//     `stale/2`).
//
// A refresh takes the locks in that order and writes the file inside them, so taking
// them in the same order cannot deadlock against Claude Code.
//
// The proper-lockfile convention
// (https://github.com/moxystudio/node-proper-lockfile#design and
// https://github.com/moxystudio/node-proper-lockfile/blob/master/lib/lockfile.js): the
// lock is a directory made with mkdir, which is atomic. The holder updates its mtime
// while it holds it. A lock whose mtime is older than the stale time is stale; a taker
// removes it and makes it again. Release removes the directory. The README warns that
// two lockers with different stale times can both hold one lock, so each lock here uses
// Claude Code's own stale time.

const credentialsFile = ".credentials.json"

// LockTimeout is how long AcquireLoginLocks waits in all when the context has no
// earlier deadline.
var LockTimeout = 30 * time.Second

var (
	lockRetryMin       = 100 * time.Millisecond // Claude Code's minTimeout for .storage-write
	lockRetryMax       = time.Second            // and its maxTimeout
	lockUpdateInterval = 5 * time.Second        // at most half of the shortest stale time (15 s)

	// Test hooks.
	onLockAcquired  func(path string)
	afterOAuthWrite func(path string)
)

var (
	// ErrLocksNotHeld means WriteLogin was called without the config dir's locks, after
	// their release, or after another process took one over.
	ErrLocksNotHeld = errors.New("claudecreds: the login locks are not held")
	// ErrNoLogin means .credentials.json has no claudeAiOauth.
	ErrNoLogin = errors.New("claudecreds: no claudeAiOauth in the file")
	// ErrRaceLost means another writer replaced oauthAccount after each try.
	ErrRaceLost = errors.New("claudecreds: another writer kept replacing oauthAccount")
)

type lockSpec struct {
	path  string
	stale time.Duration
}

func loginLockSpecs(dir string) []lockSpec {
	real, err := filepath.EvalSymlinks(dir)
	if err != nil {
		real = dir // Claude Code falls back to the path as given, too.
	}
	return []lockSpec{
		{filepath.Join(dir, ".oauth_refresh.lock"), 60 * time.Second},
		{real + ".lock", 60 * time.Second},
		{filepath.Join(dir, ".storage-write.lock"), 15 * time.Second},
	}
}

type heldLock struct {
	path  string
	mtime time.Time // the mtime this process last set
}

// Locks are Claude Code's three login locks on one config dir, held by this process.
type Locks struct {
	dir  string
	stop chan struct{}
	done chan struct{}

	mu          sync.Mutex
	held        []*heldLock
	released    bool
	compromised error
}

// AcquireLoginLocks takes the config dir's refresh lock, legacy lock and storage-write
// lock, in that order. It waits for a fresh lock and takes over a stale one. It gives
// back every lock it took when it fails. While the locks are held, it updates their
// mtimes so that Claude Code does not take them over.
func AcquireLoginLocks(ctx context.Context, dir string) (*Locks, error) {
	ctx, cancel := context.WithTimeout(ctx, LockTimeout)
	defer cancel()
	l := &Locks{dir: filepath.Clean(dir), stop: make(chan struct{}), done: make(chan struct{})}
	for _, spec := range loginLockSpecs(l.dir) {
		h, err := acquireLock(ctx, spec)
		if err != nil {
			l.removeHeld()
			return nil, err
		}
		l.held = append(l.held, h)
		if onLockAcquired != nil {
			onLockAcquired(spec.path)
		}
	}
	go l.keepFresh()
	return l, nil
}

func acquireLock(ctx context.Context, spec lockSpec) (*heldLock, error) {
	wait := lockRetryMin
	for {
		err := os.Mkdir(spec.path, 0o700)
		if err == nil {
			mtime, err := touch(spec.path)
			if err != nil {
				_ = os.Remove(spec.path)
				return nil, fmt.Errorf("claudecreds: %s: set lock mtime: %w", spec.path, err)
			}
			return &heldLock{path: spec.path, mtime: mtime}, nil
		}
		if !errors.Is(err, fs.ErrExist) {
			return nil, fmt.Errorf("claudecreds: %s: make lock: %w", spec.path, err)
		}
		st, err := os.Stat(spec.path)
		switch {
		case errors.Is(err, fs.ErrNotExist):
			continue // released between the mkdir and the stat
		case err != nil:
			return nil, fmt.Errorf("claudecreds: %s: stat lock: %w", spec.path, err)
		case time.Since(st.ModTime()) > spec.stale:
			// Stale: remove it and try again, as proper-lockfile does. Look again just
			// before the removal, so a lock another taker has just made is kept.
			if again, err := os.Stat(spec.path); err == nil && again.ModTime().Equal(st.ModTime()) {
				if err := os.Remove(spec.path); err != nil && !errors.Is(err, fs.ErrNotExist) {
					return nil, fmt.Errorf("claudecreds: %s: remove stale lock: %w", spec.path, err)
				}
			}
			continue
		}
		select {
		case <-ctx.Done():
			return nil, fmt.Errorf("claudecreds: %s: wait for lock: %w", spec.path, ctx.Err())
		case <-time.After(wait):
		}
		wait = min(wait*2, lockRetryMax)
	}
}

// touch sets path's mtime to now and returns the mtime the file system kept.
func touch(path string) (time.Time, error) {
	now := time.Now()
	if err := os.Chtimes(path, now, now); err != nil {
		return time.Time{}, err
	}
	st, err := os.Stat(path)
	if err != nil {
		return time.Time{}, err
	}
	return st.ModTime(), nil
}

func (l *Locks) keepFresh() {
	defer close(l.done)
	t := time.NewTicker(lockUpdateInterval)
	defer t.Stop()
	for {
		select {
		case <-l.stop:
			return
		case <-t.C:
		}
		l.mu.Lock()
		for _, h := range l.held {
			if l.compromised != nil {
				break
			}
			if err := h.check(); err != nil {
				l.compromised = err
				break
			}
			mtime, err := touch(h.path)
			if err != nil {
				l.compromised = fmt.Errorf("claudecreds: %s: update lock mtime: %w", h.path, err)
				break
			}
			h.mtime = mtime
		}
		l.mu.Unlock()
	}
}

// check tells whether the lock directory is still the one this process made.
func (h *heldLock) check() error {
	st, err := os.Stat(h.path)
	if err != nil {
		return fmt.Errorf("claudecreds: %s: lock gone: %w", h.path, err)
	}
	if !st.ModTime().Equal(h.mtime) {
		return fmt.Errorf("claudecreds: %s: lock taken over by another process", h.path)
	}
	return nil
}

func (l *Locks) verify(dir string) error {
	if l == nil {
		return ErrLocksNotHeld
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.released || l.dir != filepath.Clean(dir) {
		return ErrLocksNotHeld
	}
	if l.compromised != nil {
		return fmt.Errorf("%w: %w", ErrLocksNotHeld, l.compromised)
	}
	for _, h := range l.held {
		if err := h.check(); err != nil {
			return fmt.Errorf("%w: %w", ErrLocksNotHeld, err)
		}
	}
	return nil
}

// Release gives back every lock, in the reverse order. It removes only a lock this
// process still holds, never one another process took over. A second call does nothing.
func (l *Locks) Release() {
	l.mu.Lock()
	if l.released {
		l.mu.Unlock()
		return
	}
	l.released = true
	l.mu.Unlock()
	close(l.stop)
	<-l.done
	l.mu.Lock()
	defer l.mu.Unlock()
	l.removeHeld()
}

func (l *Locks) removeHeld() {
	for i := len(l.held) - 1; i >= 0; i-- {
		h := l.held[i]
		if h.check() == nil {
			_ = os.Remove(h.path)
		}
	}
	l.held = nil
}

// member is one top-level key of a JSON object, with its value's bytes as stored.
type member struct {
	key string
	val json.RawMessage
}

var errNotObject = errors.New("not a JSON object")

// parseObject splits a JSON object into its top-level members, in order. The values
// keep their bytes. Its errors never quote the data.
func parseObject(b []byte) ([]member, error) {
	dec := json.NewDecoder(bytes.NewReader(b))
	if tok, err := dec.Token(); err != nil || tok != json.Delim('{') {
		return nil, errNotObject
	}
	var ms []member
	for dec.More() {
		tok, err := dec.Token()
		key, ok := tok.(string)
		if err != nil || !ok {
			return nil, errNotObject
		}
		var val json.RawMessage
		if err := dec.Decode(&val); err != nil {
			return nil, errNotObject
		}
		ms = append(ms, member{key, val})
	}
	if tok, err := dec.Token(); err != nil || tok != json.Delim('}') {
		return nil, errNotObject
	}
	if _, err := dec.Token(); err != io.EOF {
		return nil, errNotObject
	}
	return ms, nil
}

// setMember replaces every member named key, or adds one at the end.
func setMember(ms []member, key string, val json.RawMessage) []member {
	found := false
	for i := range ms {
		if ms[i].key == key {
			ms[i].val = val
			found = true
		}
	}
	if !found {
		ms = append(ms, member{key, val})
	}
	return ms
}

func getMember(ms []member, key string) (json.RawMessage, bool) {
	for _, m := range ms {
		if m.key == key {
			return m.val, true
		}
	}
	return nil, false
}

func encodeObject(ms []member) []byte {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	buf.WriteByte('{')
	for i, m := range ms {
		if i > 0 {
			buf.WriteByte(',')
		}
		_ = enc.Encode(m.key) // a string always encodes
		buf.Truncate(buf.Len() - 1)
		buf.WriteByte(':')
		buf.Write(m.val)
	}
	buf.WriteByte('}')
	return buf.Bytes()
}

func isObject(raw json.RawMessage) bool {
	_, err := parseObject(raw)
	return err == nil
}

// ReadLogin returns the config dir's claudeAiOauth exactly as stored.
func ReadLogin(dir string) (json.RawMessage, error) {
	path := filepath.Join(dir, credentialsFile)
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("claudecreds: %s: read login: %w", path, err)
	}
	ms, err := parseObject(b)
	if err != nil {
		return nil, fmt.Errorf("claudecreds: %s: parse login: %w", path, err)
	}
	login, ok := getMember(ms, "claudeAiOauth")
	if !ok {
		return nil, fmt.Errorf("%w: %s", ErrNoLogin, path)
	}
	return login, nil
}

// WriteLogin replaces the config dir's claudeAiOauth with login. Every other top-level
// key keeps its bytes. It writes a 0600 temp file in the dir, syncs it and renames it
// over .credentials.json. It refuses to write unless locks holds the dir's locks.
func WriteLogin(locks *Locks, dir string, login json.RawMessage) error {
	path := filepath.Join(dir, credentialsFile)
	if err := locks.verify(dir); err != nil {
		return fmt.Errorf("claudecreds: %s: write login: %w", path, err)
	}
	if !isObject(login) {
		return fmt.Errorf("claudecreds: %s: write login: the new login is %w", path, errNotObject)
	}
	var ms []member
	b, err := os.ReadFile(path)
	switch {
	case errors.Is(err, fs.ErrNotExist):
	case err != nil:
		return fmt.Errorf("claudecreds: %s: read login: %w", path, err)
	default:
		if ms, err = parseObject(b); err != nil {
			return fmt.Errorf("claudecreds: %s: parse login: %w", path, err)
		}
	}
	ms = setMember(ms, "claudeAiOauth", login)
	return writeAtomic(path, encodeObject(ms), 0o600)
}

// writeAtomic writes data to a temp file beside path (beside the file a symlink names),
// syncs it, and renames it over the file.
func writeAtomic(path string, data []byte, perm fs.FileMode) error {
	if real, err := filepath.EvalSymlinks(path); err == nil {
		path = real
	}
	dir := filepath.Dir(path)
	tmp, err := os.CreateTemp(dir, "."+filepath.Base(path)+".tmp-*")
	if err != nil {
		return fmt.Errorf("claudecreds: %s: make temp file: %w", path, err)
	}
	name := tmp.Name()
	fail := func(step string, err error) error {
		_ = tmp.Close()
		_ = os.Remove(name)
		return fmt.Errorf("claudecreds: %s: %s: %w", path, step, err)
	}
	if err := tmp.Chmod(perm); err != nil {
		return fail("set temp file mode", err)
	}
	if _, err := tmp.Write(data); err != nil {
		return fail("write temp file", err)
	}
	if err := tmp.Sync(); err != nil {
		return fail("sync temp file", err)
	}
	if err := tmp.Close(); err != nil {
		return fail("close temp file", err)
	}
	if err := os.Rename(name, path); err != nil {
		_ = os.Remove(name)
		return fmt.Errorf("claudecreds: %s: rename temp file: %w", path, err)
	}
	if d, err := os.Open(dir); err == nil {
		_ = d.Sync() // the rename is durable; some file systems refuse a dir sync
		_ = d.Close()
	}
	return nil
}

// Rotate replaces the config dir's login with next, in the order of design §6.6
// (#8016): take the locks; read the live login; call writeBack with it; write next;
// release the locks. If writeBack fails, the file is not changed. Rotate returns only
// after the release, so the caller can then release the old lease. It returns the
// mtime of the file it wrote, read under the locks, for LoginChanged.
func Rotate(ctx context.Context, dir string, writeBack func(old json.RawMessage) error, next json.RawMessage) (time.Time, error) {
	path := filepath.Join(dir, credentialsFile)
	if !isObject(next) {
		return time.Time{}, fmt.Errorf("claudecreds: %s: rotate: the new login is %w", path, errNotObject)
	}
	locks, err := AcquireLoginLocks(ctx, dir)
	if err != nil {
		return time.Time{}, err
	}
	defer locks.Release()
	old, err := ReadLogin(dir)
	if err != nil {
		return time.Time{}, err
	}
	if err := writeBack(old); err != nil {
		return time.Time{}, fmt.Errorf("claudecreds: %s: write back the old login: %w", path, err)
	}
	if err := WriteLogin(locks, dir, next); err != nil {
		return time.Time{}, err
	}
	st, err := os.Stat(path)
	if err != nil {
		return time.Time{}, fmt.Errorf("claudecreds: %s: stat login: %w", path, err)
	}
	locks.Release()
	return st.ModTime(), nil
}

// LoginChanged tells whether .credentials.json's mtime differs from last, and returns
// the mtime now. A change the caller did not cause is a refresh by Claude Code, so the
// caller writes the login back. Poll it; a zero last always reads as a change.
func LoginChanged(dir string, last time.Time) (bool, time.Time, error) {
	path := filepath.Join(dir, credentialsFile)
	st, err := os.Stat(path)
	if err != nil {
		return false, time.Time{}, fmt.Errorf("claudecreds: %s: stat login: %w", path, err)
	}
	return !st.ModTime().Equal(last), st.ModTime(), nil
}

// UpdateOAuthAccount sets oauthAccount in ~/.claude.json (homeJSONPath), best effort.
// That file has no lock to share, and Claude Code rewrites it often (design §6.6). It
// changes only oauthAccount, keeps the other keys and the file's mode, and writes
// through a temp file and a rename. It then reads the file again and tries again, at
// most 3 times, while oauthAccount is not the new one; then it returns ErrRaceLost.
func UpdateOAuthAccount(homeJSONPath string, account map[string]any) error {
	val, err := json.Marshal(account)
	if err != nil {
		return fmt.Errorf("claudecreds: %s: encode oauthAccount: %w", homeJSONPath, err)
	}
	var want any
	_ = json.Unmarshal(val, &want) // val was just encoded
	const retries = 3
	for try := 0; try <= retries; try++ {
		st, err := os.Stat(homeJSONPath)
		if err != nil {
			return fmt.Errorf("claudecreds: %s: stat: %w", homeJSONPath, err)
		}
		ms, err := readObject(homeJSONPath)
		if err != nil {
			return err
		}
		ms = setMember(ms, "oauthAccount", val)
		if err := writeAtomic(homeJSONPath, encodeObject(ms), st.Mode().Perm()); err != nil {
			return err
		}
		if afterOAuthWrite != nil {
			afterOAuthWrite(homeJSONPath)
		}
		ms, err = readObject(homeJSONPath)
		if err != nil {
			continue // another writer's file, maybe half written; try again
		}
		if raw, ok := getMember(ms, "oauthAccount"); ok {
			var got any
			if json.Unmarshal(raw, &got) == nil && reflect.DeepEqual(got, want) {
				return nil
			}
		}
	}
	return fmt.Errorf("%w: %s", ErrRaceLost, homeJSONPath)
}

func readObject(path string) ([]member, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("claudecreds: %s: read: %w", path, err)
	}
	ms, err := parseObject(b)
	if err != nil {
		return nil, fmt.Errorf("claudecreds: %s: parse: %w", path, err)
	}
	return ms, nil
}
