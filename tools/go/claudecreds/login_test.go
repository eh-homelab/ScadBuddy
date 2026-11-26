package claudecreds

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

// Every test works in t.TempDir(). None reads or writes the real ~/.claude or
// ~/.claude.json.

const credsFixture = `{"claudeAiOauth":{"accessToken":"old-a","refreshToken":"old-r","expiresAt":1},"mcpOAuth":{"srv|1":{"accessToken":"mcp-a","n":1.50}},"other":[1, 2,3]}`

// fastLocks makes the lock loop and the mtime updater fast for one test.
func fastLocks(t *testing.T) {
	t.Helper()
	oldMin, oldMax, oldUpd := lockRetryMin, lockRetryMax, lockUpdateInterval
	lockRetryMin, lockRetryMax, lockUpdateInterval = 5*time.Millisecond, 20*time.Millisecond, 20*time.Millisecond
	t.Cleanup(func() { lockRetryMin, lockRetryMax, lockUpdateInterval = oldMin, oldMax, oldUpd })
}

// configDir makes <tmp>/cfg with a .credentials.json, so the legacy lock
// <tmp>/cfg.lock is in the test's own temp dir too.
func configDir(t *testing.T, creds string) string {
	t.Helper()
	dir := filepath.Join(t.TempDir(), "cfg")
	if err := os.Mkdir(dir, 0o700); err != nil {
		t.Fatal(err)
	}
	if creds != "" {
		if err := os.WriteFile(filepath.Join(dir, credentialsFile), []byte(creds), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	return dir
}

func lockPaths(dir string) []string {
	return []string{
		filepath.Join(dir, ".oauth_refresh.lock"),
		dir + ".lock",
		filepath.Join(dir, ".storage-write.lock"),
	}
}

func exists(t *testing.T, p string) bool {
	t.Helper()
	_, err := os.Stat(p)
	if err == nil {
		return true
	}
	if errors.Is(err, fs.ErrNotExist) {
		return false
	}
	t.Fatal(err)
	return false
}

func TestAcquireLoginLocksOrderAndRelease(t *testing.T) {
	fastLocks(t)
	dir := configDir(t, credsFixture)
	var order []string
	onLockAcquired = func(p string) { order = append(order, p) }
	t.Cleanup(func() { onLockAcquired = nil })

	l, err := AcquireLoginLocks(context.Background(), dir)
	if err != nil {
		t.Fatal(err)
	}
	want := lockPaths(dir)
	if strings.Join(order, "\n") != strings.Join(want, "\n") {
		t.Fatalf("order = %v, want %v", order, want)
	}
	for _, p := range want {
		st, err := os.Stat(p)
		if err != nil || !st.IsDir() {
			t.Fatalf("%s is not a lock directory: %v", p, err)
		}
	}
	l.Release()
	for _, p := range want {
		if exists(t, p) {
			t.Fatalf("%s is still there after Release", p)
		}
	}
	l.Release() // a second Release does nothing
}

func TestAcquireLoginLocksReleasesAllOnFailure(t *testing.T) {
	fastLocks(t)
	dir := configDir(t, credsFixture)
	held := filepath.Join(dir, ".storage-write.lock")
	if err := os.Mkdir(held, 0o755); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 150*time.Millisecond)
	defer cancel()
	if _, err := AcquireLoginLocks(ctx, dir); err == nil {
		t.Fatal("AcquireLoginLocks took a fresh lock that another process holds")
	}
	paths := lockPaths(dir)
	for _, p := range paths[:2] {
		if exists(t, p) {
			t.Fatalf("%s was not given back after the failure", p)
		}
	}
	if !exists(t, held) {
		t.Fatal("the other process's lock was removed")
	}
}

func TestAcquireLoginLocksTakesOverOnlyStale(t *testing.T) {
	fastLocks(t)
	dir := configDir(t, credsFixture)
	refresh := filepath.Join(dir, ".oauth_refresh.lock")
	if err := os.Mkdir(refresh, 0o755); err != nil {
		t.Fatal(err)
	}
	old := time.Now().Add(-61 * time.Second)
	if err := os.Chtimes(refresh, old, old); err != nil {
		t.Fatal(err)
	}
	l, err := AcquireLoginLocks(context.Background(), dir)
	if err != nil {
		t.Fatalf("a stale lock was not taken over: %v", err)
	}
	st, err := os.Stat(refresh)
	if err != nil || time.Since(st.ModTime()) > 10*time.Second {
		t.Fatalf("the taken-over lock is not fresh: %v", err)
	}
	l.Release()

	// 30 s is stale for .storage-write (15 s) but fresh for .oauth_refresh.lock (60 s).
	if err := os.Mkdir(refresh, 0o755); err != nil {
		t.Fatal(err)
	}
	fresh := time.Now().Add(-30 * time.Second)
	if err := os.Chtimes(refresh, fresh, fresh); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 150*time.Millisecond)
	defer cancel()
	if _, err := AcquireLoginLocks(ctx, dir); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("a fresh lock did not make it wait: %v", err)
	}
	st, err = os.Stat(refresh)
	if err != nil || !st.ModTime().Equal(fresh) {
		t.Fatalf("the fresh lock was changed: %v", err)
	}
}

func TestLocksRefreshMtimeWhileHeld(t *testing.T) {
	fastLocks(t)
	dir := configDir(t, credsFixture)
	l, err := AcquireLoginLocks(context.Background(), dir)
	if err != nil {
		t.Fatal(err)
	}
	defer l.Release()
	old := time.Now().Add(-time.Hour)
	l.mu.Lock()
	for _, h := range l.held {
		if err := os.Chtimes(h.path, old, old); err != nil {
			t.Fatal(err)
		}
		h.mtime = old
	}
	l.mu.Unlock()
	time.Sleep(10 * lockUpdateInterval)
	for _, p := range lockPaths(dir) {
		st, err := os.Stat(p)
		if err != nil || time.Since(st.ModTime()) > time.Minute {
			t.Fatalf("%s mtime was not refreshed: %v", p, err)
		}
	}
}

func TestReadLoginExact(t *testing.T) {
	dir := configDir(t, credsFixture)
	got, err := ReadLogin(dir)
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != `{"accessToken":"old-a","refreshToken":"old-r","expiresAt":1}` {
		t.Fatalf("ReadLogin changed the bytes: %s", got)
	}
	if _, err := ReadLogin(configDir(t, `{"mcpOAuth":{}}`)); !errors.Is(err, ErrNoLogin) {
		t.Fatalf("a file with no login gave %v", err)
	}
	_, err = ReadLogin(configDir(t, `{"claudeAiOauth":{"accessToken":"secret-tok`))
	if err == nil || strings.Contains(err.Error(), "secret-tok") {
		t.Fatalf("a broken file gave %v", err)
	}
}

func TestWriteLoginKeepsOtherKeys(t *testing.T) {
	fastLocks(t)
	dir := configDir(t, credsFixture)
	if err := os.Chmod(filepath.Join(dir, credentialsFile), 0o644); err != nil {
		t.Fatal(err)
	}
	l, err := AcquireLoginLocks(context.Background(), dir)
	if err != nil {
		t.Fatal(err)
	}
	next := json.RawMessage(`{"accessToken":"new-a","refreshToken":"new-r"}`)
	if err := WriteLogin(l, dir, next); err != nil {
		t.Fatal(err)
	}
	l.Release()
	path := filepath.Join(dir, credentialsFile)
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	want := `{"claudeAiOauth":{"accessToken":"new-a","refreshToken":"new-r"},"mcpOAuth":{"srv|1":{"accessToken":"mcp-a","n":1.50}},"other":[1, 2,3]}`
	if string(b) != want {
		t.Fatalf("file =\n%s\nwant\n%s", b, want)
	}
	st, err := os.Stat(path)
	if err != nil || st.Mode().Perm() != 0o600 {
		t.Fatalf("mode = %v, want 0600: %v", st.Mode().Perm(), err)
	}
	ents, _ := os.ReadDir(dir)
	for _, e := range ents {
		if strings.Contains(e.Name(), ".tmp") {
			t.Fatalf("temp file %s was left behind", e.Name())
		}
	}
}

func TestWriteLoginRefusesWithoutLocks(t *testing.T) {
	fastLocks(t)
	dir := configDir(t, credsFixture)
	next := json.RawMessage(`{"accessToken":"new-a"}`)
	if err := WriteLogin(nil, dir, next); !errors.Is(err, ErrLocksNotHeld) {
		t.Fatalf("nil locks gave %v", err)
	}
	l, err := AcquireLoginLocks(context.Background(), dir)
	if err != nil {
		t.Fatal(err)
	}
	if err := WriteLogin(l, configDir(t, credsFixture), next); !errors.Is(err, ErrLocksNotHeld) {
		t.Fatalf("another dir's locks gave %v", err)
	}
	if err := WriteLogin(l, dir, json.RawMessage(`"not an object"`)); err == nil {
		t.Fatal("a login that is not an object was written")
	}
	l.Release()
	if err := WriteLogin(l, dir, next); !errors.Is(err, ErrLocksNotHeld) {
		t.Fatalf("released locks gave %v", err)
	}
	b, _ := os.ReadFile(filepath.Join(dir, credentialsFile))
	if string(b) != credsFixture {
		t.Fatal("the file changed without the locks")
	}
}

func TestWriteLoginRefusesWhenLockTakenOver(t *testing.T) {
	fastLocks(t)
	dir := configDir(t, credsFixture)
	l, err := AcquireLoginLocks(context.Background(), dir)
	if err != nil {
		t.Fatal(err)
	}
	defer l.Release()
	sw := filepath.Join(dir, ".storage-write.lock")
	// Another process took the lock over: removed and made it again.
	if err := os.Remove(sw); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(sw, 0o755); err != nil {
		t.Fatal(err)
	}
	other := time.Now().Add(time.Hour)
	if err := os.Chtimes(sw, other, other); err != nil {
		t.Fatal(err)
	}
	if err := WriteLogin(l, dir, json.RawMessage(`{"accessToken":"new-a"}`)); !errors.Is(err, ErrLocksNotHeld) {
		t.Fatalf("a taken-over lock gave %v", err)
	}
	l.Release()
	if !exists(t, sw) {
		t.Fatal("Release removed another process's lock")
	}
}

func TestRotate(t *testing.T) {
	fastLocks(t)
	dir := configDir(t, credsFixture)
	var got json.RawMessage
	next := json.RawMessage(`{"accessToken":"new-a"}`)
	if _, err := Rotate(context.Background(), dir, func(old json.RawMessage) error {
		got = old
		for _, p := range lockPaths(dir) {
			if !exists(t, p) {
				t.Errorf("writeBack ran without %s held", p)
			}
		}
		return nil
	}, next); err != nil {
		t.Fatal(err)
	}
	if string(got) != `{"accessToken":"old-a","refreshToken":"old-r","expiresAt":1}` {
		t.Fatalf("writeBack got %s", got)
	}
	now, _ := ReadLogin(dir)
	if string(now) != string(next) {
		t.Fatalf("login = %s", now)
	}
	for _, p := range lockPaths(dir) {
		if exists(t, p) {
			t.Fatalf("%s is held after Rotate returned", p)
		}
	}
}

func TestRotateDoesNotWriteWhenWriteBackFails(t *testing.T) {
	fastLocks(t)
	dir := configDir(t, credsFixture)
	path := filepath.Join(dir, credentialsFile)
	before, _ := os.Stat(path)
	boom := errors.New("pool down")
	_, err := Rotate(context.Background(), dir, func(json.RawMessage) error { return boom }, json.RawMessage(`{"accessToken":"new-a"}`))
	if !errors.Is(err, boom) {
		t.Fatalf("err = %v", err)
	}
	b, _ := os.ReadFile(path)
	after, _ := os.Stat(path)
	if string(b) != credsFixture || !after.ModTime().Equal(before.ModTime()) {
		t.Fatal("the file changed after writeBack failed")
	}
	for _, p := range lockPaths(dir) {
		if exists(t, p) {
			t.Fatalf("%s was not released", p)
		}
	}
}

func TestRotateWaitsForStorageWriteHolder(t *testing.T) {
	fastLocks(t)
	dir := configDir(t, credsFixture)
	sw := filepath.Join(dir, ".storage-write.lock")
	if err := os.Mkdir(sw, 0o755); err != nil {
		t.Fatal(err)
	}
	var wg sync.WaitGroup
	done := make(chan error, 1)
	wg.Add(1)
	go func() {
		defer wg.Done()
		_, err := Rotate(context.Background(), dir, func(json.RawMessage) error { return nil }, json.RawMessage(`{"accessToken":"new-a"}`))
		done <- err
	}()
	time.Sleep(200 * time.Millisecond)
	select {
	case err := <-done:
		t.Fatalf("Rotate did not wait for the holder: %v", err)
	default:
	}
	b, _ := os.ReadFile(filepath.Join(dir, credentialsFile))
	if string(b) != credsFixture {
		t.Fatal("Rotate wrote while another process held .storage-write")
	}
	// The other writer (a Claude Code refresh) writes its own login, then releases.
	refreshed := `{"claudeAiOauth":{"accessToken":"refreshed-a","refreshToken":"refreshed-r"},"mcpOAuth":{}}`
	if err := os.WriteFile(filepath.Join(dir, credentialsFile), []byte(refreshed), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Remove(sw); err != nil {
		t.Fatal(err)
	}
	wg.Wait()
	if err := <-done; err != nil {
		t.Fatal(err)
	}
	now, _ := ReadLogin(dir)
	if string(now) != `{"accessToken":"new-a"}` {
		t.Fatalf("login = %s", now)
	}
}

func TestRotateWritesBackTheRefreshedLogin(t *testing.T) {
	fastLocks(t)
	dir := configDir(t, credsFixture)
	sw := filepath.Join(dir, ".storage-write.lock")
	if err := os.Mkdir(sw, 0o755); err != nil {
		t.Fatal(err)
	}
	got := make(chan json.RawMessage, 1)
	done := make(chan error, 1)
	go func() {
		_, err := Rotate(context.Background(), dir, func(old json.RawMessage) error { got <- old; return nil }, json.RawMessage(`{"accessToken":"new-a"}`))
		done <- err
	}()
	time.Sleep(100 * time.Millisecond)
	refreshed := `{"claudeAiOauth":{"refreshToken":"live-r"}}`
	if err := os.WriteFile(filepath.Join(dir, credentialsFile), []byte(refreshed), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Remove(sw); err != nil {
		t.Fatal(err)
	}
	if err := <-done; err != nil {
		t.Fatal(err)
	}
	if old := <-got; string(old) != `{"refreshToken":"live-r"}` {
		t.Fatalf("writeBack got %s, not the refreshed login", old)
	}
}

const homeFixture = `{"numStartups":7,"oauthAccount":{"accountUuid":"a1","emailAddress":"old@example.com"},"projects":{"/x":{"allowedTools":[]}},"z":"last"}`

func homeFile(t *testing.T, content string) string {
	t.Helper()
	p := filepath.Join(t.TempDir(), "home.claude.json")
	if err := os.WriteFile(p, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
	return p
}

func TestUpdateOAuthAccountKeepsOtherKeys(t *testing.T) {
	p := homeFile(t, homeFixture)
	acct := map[string]any{"accountUuid": "b2", "emailAddress": "new@example.com"}
	if err := UpdateOAuthAccount(p, acct); err != nil {
		t.Fatal(err)
	}
	b, _ := os.ReadFile(p)
	want := `{"numStartups":7,"oauthAccount":{"accountUuid":"b2","emailAddress":"new@example.com"},"projects":{"/x":{"allowedTools":[]}},"z":"last"}`
	if string(b) != want {
		t.Fatalf("file =\n%s\nwant\n%s", b, want)
	}
	st, _ := os.Stat(p)
	if st.Mode().Perm() != 0o644 {
		t.Fatalf("mode = %v, want the old 0644", st.Mode().Perm())
	}
}

func TestUpdateOAuthAccountRetriesAfterARace(t *testing.T) {
	p := homeFile(t, homeFixture)
	acct := map[string]any{"accountUuid": "b2"}
	// Another writer (Claude Code) rewrites the file from its old copy after our
	// first rename, and so puts the old oauthAccount back.
	calls := 0
	afterOAuthWrite = func(path string) {
		calls++
		if calls == 1 {
			if err := os.WriteFile(path, []byte(homeFixture), 0o644); err != nil {
				t.Error(err)
			}
		}
	}
	t.Cleanup(func() { afterOAuthWrite = nil })
	if err := UpdateOAuthAccount(p, acct); err != nil {
		t.Fatal(err)
	}
	if calls != 2 {
		t.Fatalf("writes = %d, want 2", calls)
	}
	b, _ := os.ReadFile(p)
	if !bytes.Contains(b, []byte(`"oauthAccount":{"accountUuid":"b2"}`)) || !bytes.Contains(b, []byte(`"numStartups":7`)) {
		t.Fatalf("file = %s", b)
	}
}

func TestUpdateOAuthAccountRaceLost(t *testing.T) {
	p := homeFile(t, homeFixture)
	calls := 0
	afterOAuthWrite = func(path string) {
		calls++
		if err := os.WriteFile(path, []byte(homeFixture), 0o644); err != nil {
			t.Error(err)
		}
	}
	t.Cleanup(func() { afterOAuthWrite = nil })
	err := UpdateOAuthAccount(p, map[string]any{"accountUuid": "b2"})
	if !errors.Is(err, ErrRaceLost) {
		t.Fatalf("err = %v", err)
	}
	if calls != 4 {
		t.Fatalf("writes = %d, want 1 and 3 retries", calls)
	}
}

func TestLoginChanged(t *testing.T) {
	fastLocks(t)
	dir := configDir(t, credsFixture)
	changed, mtime, err := LoginChanged(dir, time.Time{})
	if err != nil || !changed {
		t.Fatalf("first look: changed=%v err=%v", changed, err)
	}
	changed, again, err := LoginChanged(dir, mtime)
	if err != nil || changed || !again.Equal(mtime) {
		t.Fatalf("no change: changed=%v err=%v", changed, err)
	}
	// Claude Code refreshes the login.
	later := mtime.Add(2 * time.Second)
	if err := os.WriteFile(filepath.Join(dir, credentialsFile), []byte(`{"claudeAiOauth":{}}`), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Chtimes(filepath.Join(dir, credentialsFile), later, later); err != nil {
		t.Fatal(err)
	}
	changed, now, err := LoginChanged(dir, mtime)
	if err != nil || !changed || !now.Equal(later) {
		t.Fatalf("refresh: changed=%v now=%v err=%v", changed, now, err)
	}
	// A rotation's own write is not a refresh: Rotate returns the mtime it left.
	written, err := Rotate(context.Background(), dir, func(json.RawMessage) error { return nil }, json.RawMessage(`{"accessToken":"n"}`))
	if err != nil {
		t.Fatal(err)
	}
	if changed, _, err := LoginChanged(dir, written); err != nil || changed {
		t.Fatalf("own write: changed=%v err=%v", changed, err)
	}
	if _, _, err := LoginChanged(configDir(t, ""), time.Time{}); !errors.Is(err, fs.ErrNotExist) {
		t.Fatalf("no file gave %v", err)
	}
}
