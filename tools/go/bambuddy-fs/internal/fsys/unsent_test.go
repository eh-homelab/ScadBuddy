package fsys

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"
)

// retrying mounts lib as releases does, with the retrier running at a backoff of
// milliseconds until the test ends.
func retrying(t *testing.T, lib Library, opts Options) (string, <-chan struct{}) {
	t.Helper()
	dir, root := mountRoot(t, lib, opts)
	done := make(chan struct{}, 16)
	root.tree.released = func() { done <- struct{}{} }
	q := root.tree.unsent
	q.first, q.max = 20*time.Millisecond, 100*time.Millisecond
	ctx, cancel := context.WithCancel(context.Background())
	stopped := make(chan struct{})
	go func() {
		RetryUnsent(ctx, root)
		close(stopped)
	}()
	// Registered after the mount's cleanup, so it runs before the unmount.
	t.Cleanup(func() {
		cancel()
		<-stopped
	})
	return dir, done
}

// eventually waits up to 10 s for ok.
func eventually(t *testing.T, what string, ok func() bool) {
	t.Helper()
	for deadline := time.Now().Add(10 * time.Second); !ok(); {
		if time.Now().After(deadline) {
			t.Fatalf("timed out waiting for %s", what)
		}
		time.Sleep(10 * time.Millisecond)
	}
}

func queued(t *testing.T, state string) []Unsent {
	t.Helper()
	out, err := ListUnsent(state)
	if err != nil {
		t.Fatal(err)
	}
	return out
}

// libraryHas is whether the library holds exactly these files, by folder and name,
// with these contents.
func libraryHas(lib *fakeLibrary, want map[string]string) bool {
	ok := true
	lib.locked(func() {
		got := map[string]string{}
		for id, f := range lib.files {
			got[strconv.Itoa(f.FolderID)+"/"+f.Name] = string(lib.data[id])
		}
		if len(got) != len(want) {
			ok = false
			return
		}
		for k, v := range want {
			if got[k] != v {
				ok = false
			}
		}
	})
	return ok
}

// conflictCopy returns the contents of the one file named like a conflict copy of name.
func conflictCopy(lib *fakeLibrary, name string) (string, bool) {
	stem, ext := splitExt(name)
	var data []string
	lib.locked(func() {
		for id, f := range lib.files {
			if strings.HasPrefix(f.Name, stem+" (conflict ") && strings.HasSuffix(f.Name, ")"+ext) {
				data = append(data, string(lib.data[id]))
			}
		}
	})
	if len(data) != 1 {
		return "", false
	}
	return data[0], true
}

// writeQueued puts an entry on disk as an earlier mount would have left it.
func writeQueued(t *testing.T, state string, seq int, rec record, contents string) string {
	t.Helper()
	dir := filepath.Join(state, "unsent", strconv.Itoa(seq))
	if err := os.MkdirAll(dir, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := writeRecord(dir, rec); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "contents"), []byte(contents), 0o600); err != nil {
		t.Fatal(err)
	}
	return dir
}

func TestAFailedUploadIsQueued(t *testing.T) {
	lib := newFakeLibrary()
	lib.folder(4, "Top", 0)
	lib.down = true
	state := t.TempDir()
	dir, released := releases(t, lib, Options{StateDir: state})
	log := logs(t)
	path := filepath.Join(dir, "Top", "a.3mf")
	if err := os.WriteFile(path, []byte("abc"), 0o644); err != nil {
		t.Fatalf("close of a queued write: %v", err)
	}
	waitRelease(t, released)

	if got := names(t, filepath.Join(dir, "Top")); !slices.Equal(got, []string{"a.3mf"}) {
		t.Fatalf("listed: %v", got)
	}
	if info, err := os.Stat(path); err != nil || info.Size() != 3 {
		t.Fatalf("stat: %v %v", info, err)
	}
	if data, err := os.ReadFile(path); err != nil || string(data) != "abc" {
		t.Fatalf("read: %q %v", data, err)
	}
	q := queued(t, state)
	if len(q) != 1 || q[0].FolderID != 4 || q[0].Name != "a.3mf" || q[0].Size != 3 || q[0].Attempts != 1 || !strings.Contains(q[0].LastError, "502") {
		t.Fatalf("queue: %+v", q)
	}
	if data, err := os.ReadFile(filepath.Join(q[0].Path, "contents")); err != nil || string(data) != "abc" {
		t.Fatalf("contents: %q %v", data, err)
	}
	rec, err := readRecord(q[0].Path)
	if err != nil || len(rec.Bases) != 0 || rec.SavedAt.IsZero() {
		t.Fatalf("record: %+v %v", rec, err)
	}
	if got := log.String(); !strings.Contains(got, q[0].Path) {
		t.Fatalf("log: %q", got)
	}
	lib.locked(func() {
		if lib.uploads != 0 {
			t.Fatalf("uploads %d", lib.uploads)
		}
	})
}

func TestTheSavePatternLandsUnderTheFinalName(t *testing.T) {
	lib := newFakeLibrary()
	lib.folder(4, "Top", 0)
	lib.down = true
	state := t.TempDir()
	dir, released := retrying(t, lib, Options{StateDir: state})
	logs(t)
	top := filepath.Join(dir, "Top")
	if err := os.WriteFile(filepath.Join(top, "x.3mf.tmp"), []byte("data"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Rename(filepath.Join(top, "x.3mf.tmp"), filepath.Join(top, "x.3mf")); err != nil {
		t.Fatal(err)
	}
	waitRelease(t, released)
	if got := names(t, top); !slices.Equal(got, []string{"x.3mf"}) {
		t.Fatalf("listed: %v", got)
	}
	if q := queued(t, state); len(q) != 1 || q[0].Name != "x.3mf" {
		t.Fatalf("queue: %+v", q)
	}

	lib.setDown(false)
	eventually(t, "the upload", func() bool { return libraryHas(lib, map[string]string{"4/x.3mf": "data"}) })
	eventually(t, "the queue to empty", func() bool { return len(queued(t, state)) == 0 })
	if got := names(t, top); !slices.Equal(got, []string{"x.3mf"}) {
		t.Fatalf("listed after: %v", got)
	}
	if data, err := os.ReadFile(filepath.Join(top, "x.3mf")); err != nil || string(data) != "data" {
		t.Fatalf("read after: %q %v", data, err)
	}
}

func TestUnlinkOfAQueuedWrite(t *testing.T) {
	lib := newFakeLibrary()
	lib.down = true
	state := t.TempDir()
	dir, released := releases(t, lib, Options{StateDir: state})
	logs(t)
	path := filepath.Join(dir, "a.3mf")
	if err := os.WriteFile(path, []byte("abc"), 0o644); err != nil {
		t.Fatal(err)
	}
	waitRelease(t, released)
	entry := queued(t, state)[0].Path
	if err := os.Remove(path); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(entry); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("entry left: %v", err)
	}
	if _, err := os.Stat(path); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("stat: %v", err)
	}
	if got := names(t, dir); len(got) != 0 {
		t.Fatalf("listed: %v", got)
	}
}

func TestQueuedWritesSurviveARestart(t *testing.T) {
	lib := newFakeLibrary()
	lib.folder(4, "Top", 0)
	lib.file(10, "b.3mf", 4, []byte("old"))
	lib.down = true
	state := t.TempDir()
	dir, released := releases(t, lib, Options{StateDir: state})
	logs(t)
	for name, data := range map[string]string{"a.3mf": "abc", "b.3mf": "new"} {
		if err := os.WriteFile(filepath.Join(dir, "Top", name), []byte(data), 0o644); err != nil {
			t.Fatal(err)
		}
		waitRelease(t, released)
	}
	var before record
	for _, u := range queued(t, state) {
		if u.Name == "b.3mf" {
			before, _ = readRecord(u.Path)
		}
	}
	if len(before.Bases) != 1 || before.Bases[0].ID != 10 || before.Bases[0].Created.IsZero() || before.Bases[0].Name != "b.3mf" {
		t.Fatalf("b's bases: %+v", before.Bases)
	}

	// A new mount over the same state directory, with a cache directory of its own.
	again, _ := retrying(t, lib, Options{StateDir: state, CacheDir: t.TempDir()})
	info, err := os.Stat(filepath.Join(again, "Top", "a.3mf"))
	if err != nil || info.Sys().(*syscall.Stat_t).Ino&(1<<62) == 0 {
		t.Fatalf("reloaded entry: %v %v", info, err)
	}
	if got := names(t, filepath.Join(again, "Top")); !slices.Equal(got, []string{"a.3mf", "b.3mf"}) {
		t.Fatalf("listed: %v", got)
	}
	if data, err := os.ReadFile(filepath.Join(again, "Top", "b.3mf")); err != nil || string(data) != "new" {
		t.Fatalf("read: %q %v", data, err)
	}
	for _, u := range queued(t, state) {
		if rec, _ := readRecord(u.Path); u.Name == "b.3mf" && !slices.Equal(rec.Bases, before.Bases) {
			t.Fatalf("bases after the restart: %+v", rec.Bases)
		}
	}

	lib.setDown(false)
	eventually(t, "the uploads", func() bool {
		return libraryHas(lib, map[string]string{"4/a.3mf": "abc", "4/b.3mf": "new"})
	})
	eventually(t, "the queue to empty", func() bool { return len(queued(t, state)) == 0 })
	lib.locked(func() {
		if !lib.deleted[10] {
			t.Fatal("the replaced file was not deleted")
		}
	})
}

func TestACorruptRecordIsSkippedAndKept(t *testing.T) {
	lib := newFakeLibrary()
	state := t.TempDir()
	bad := filepath.Join(state, "unsent", "7")
	if err := os.MkdirAll(bad, 0o700); err != nil {
		t.Fatal(err)
	}
	for name, data := range map[string]string{"record.json": "{not json", "contents": "x"} {
		if err := os.WriteFile(filepath.Join(bad, name), []byte(data), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	writeQueued(t, state, 8, record{Name: "good.3mf", Size: 4, Attempts: 1}, "good")
	log := logs(t)
	retrying(t, lib, Options{StateDir: state})
	eventually(t, "the good entry", func() bool { return libraryHas(lib, map[string]string{"0/good.3mf": "good"}) })
	eventually(t, "the good entry to leave the queue", func() bool { return len(queued(t, state)) == 1 })
	for _, name := range []string{"record.json", "contents"} {
		if _, err := os.Stat(filepath.Join(bad, name)); err != nil {
			t.Fatalf("corrupt entry touched: %v", err)
		}
	}
	if q := queued(t, state); q[0].Path != bad || q[0].Err == nil {
		t.Fatalf("queue: %+v", q)
	}
	if got := log.String(); !strings.Contains(got, bad) {
		t.Fatalf("log: %q", got)
	}
}

func TestBackoffSchedule(t *testing.T) {
	q := newQueue(t.TempDir())
	clock := time.Date(2026, 10, 13, 12, 0, 0, 0, time.UTC)
	q.now = func() time.Time { return clock }
	jitter := 0.5
	q.jitter = func() float64 { return jitter }
	for attempts, want := range []time.Duration{1: 15 * time.Second, 30 * time.Second, time.Minute, 2 * time.Minute, 4 * time.Minute, 8 * time.Minute, 10 * time.Minute, 10 * time.Minute} {
		if attempts > 0 {
			if got := q.backoff(attempts); got != want {
				t.Errorf("attempt %d: %v, want %v", attempts, got, want)
			}
		}
	}
	for _, c := range []struct {
		jitter   float64
		attempts int
		want     time.Duration
	}{{0, 1, 12 * time.Second}, {1, 1, 18 * time.Second}, {1, 6, 576 * time.Second}, {1, 7, 10 * time.Minute}} {
		jitter = c.jitter
		if got := q.backoff(c.attempts); got != c.want {
			t.Errorf("jitter %v attempt %d: %v, want %v", c.jitter, c.attempts, got, c.want)
		}
	}
	jitter = 0.5

	// A reloaded entry is due at once; after each failure, a backoff later.
	writeQueued(t, filepath.Dir(q.dir), 1, record{Name: "a.3mf"}, "a")
	q.load()
	if len(q.due()) != 1 {
		t.Fatal("not due at load")
	}
	e := q.due()[0]
	for attempts, wait := range map[int]time.Duration{1: 15 * time.Second, 2: 30 * time.Second} {
		if err := q.update(e, func(r *record) { r.Attempts, r.LastAttempt = attempts, clock }); err != nil {
			t.Fatal(err)
		}
		q.reschedule(e)
		if len(q.due()) != 0 || q.untilNext() != wait {
			t.Fatalf("after %d: due %v, next in %v", attempts, q.due(), q.untilNext())
		}
		clock = clock.Add(wait - time.Second)
		if len(q.due()) != 0 {
			t.Fatalf("after %d: due early", attempts)
		}
		clock = clock.Add(time.Second)
		if len(q.due()) != 1 {
			t.Fatalf("after %d: not due", attempts)
		}
	}
	// A held entry is left alone.
	e.held = 1
	if len(q.due()) != 0 || q.untilNext() != time.Hour {
		t.Fatalf("held: due %v next %v", q.due(), q.untilNext())
	}
}

func TestAnUnchangedBaseIsReplaced(t *testing.T) {
	lib := newFakeLibrary()
	lib.file(10, "a.3mf", 0, []byte("old"))
	lib.down = true
	state := t.TempDir()
	dir, released := retrying(t, lib, Options{StateDir: state})
	logs(t)
	if err := os.WriteFile(filepath.Join(dir, "a.3mf"), []byte("new"), 0o644); err != nil {
		t.Fatal(err)
	}
	waitRelease(t, released)
	if data, err := os.ReadFile(filepath.Join(dir, "a.3mf")); err != nil || string(data) != "new" {
		t.Fatalf("read while queued: %q %v", data, err)
	}
	if got := names(t, dir); !slices.Equal(got, []string{"a.3mf"}) {
		t.Fatalf("listed: %v", got)
	}
	lib.setDown(false)
	eventually(t, "the replace", func() bool { return libraryHas(lib, map[string]string{"0/a.3mf": "new"}) })
	eventually(t, "the queue to empty", func() bool { return len(queued(t, state)) == 0 })
}

func TestANewerFileUnderTheNameGetsAConflictCopy(t *testing.T) {
	for _, existing := range []bool{true, false} {
		lib := newFakeLibrary()
		lib.folder(4, "Top", 0)
		if existing {
			lib.file(10, "a.3mf", 4, []byte("old"))
		}
		lib.down = true
		state := t.TempDir()
		dir, released := retrying(t, lib, Options{StateDir: state})
		log := logs(t)
		if err := os.WriteFile(filepath.Join(dir, "Top", "a.3mf"), []byte("mine"), 0o644); err != nil {
			t.Fatal(err)
		}
		waitRelease(t, released)
		// Someone else saves a.3mf while ours waits.
		lib.remove(10)
		lib.file(20, "a.3mf", 4, []byte("theirs"))
		lib.setDown(false)
		eventually(t, "the conflict copy "+log.String(), func() bool {
			data, ok := conflictCopy(lib, "a.3mf")
			return ok && data == "mine"
		})
		eventually(t, "the queue to empty", func() bool { return len(queued(t, state)) == 0 })
		lib.locked(func() {
			if lib.files[20].Name != "a.3mf" || string(lib.data[20]) != "theirs" || lib.deleted[20] || len(lib.files) != 2 {
				t.Fatalf("existing %v: %+v", existing, lib.files)
			}
		})
		if !strings.Contains(log.String(), "conflict copy") {
			t.Fatalf("log: %q", log.String())
		}
	}
}

func TestTwoQueuedSavesOfOneNameSendOnlyTheLater(t *testing.T) {
	lib := newFakeLibrary()
	lib.down = true
	state := t.TempDir()
	dir, released := retrying(t, lib, Options{StateDir: state})
	logs(t)
	for _, data := range []string{"one", "two"} {
		if err := os.WriteFile(filepath.Join(dir, "x.3mf.tmp"), []byte(data), 0o644); err != nil {
			t.Fatal(err)
		}
		if err := os.Rename(filepath.Join(dir, "x.3mf.tmp"), filepath.Join(dir, "x.3mf")); err != nil {
			t.Fatalf("save %s: %v", data, err)
		}
		waitRelease(t, released)
	}
	if q := queued(t, state); len(q) != 1 || q[0].Name != "x.3mf" || q[0].Size != 3 {
		t.Fatalf("queue: %+v", q)
	}
	if data, err := os.ReadFile(filepath.Join(dir, "x.3mf")); err != nil || string(data) != "two" {
		t.Fatalf("read: %q %v", data, err)
	}
	lib.setDown(false)
	eventually(t, "the later save", func() bool { return libraryHas(lib, map[string]string{"0/x.3mf": "two"}) })
	eventually(t, "the queue to empty", func() bool { return len(queued(t, state)) == 0 })
	lib.locked(func() {
		if lib.uploads != 1 {
			t.Fatalf("uploads %d", lib.uploads)
		}
	})
}

func TestAQueuedWriteWhoseFolderIsGoneGoesToTheTop(t *testing.T) {
	lib := newFakeLibrary()
	lib.folder(4, "Top", 0)
	lib.down = true
	state := t.TempDir()
	dir, released := retrying(t, lib, Options{StateDir: state})
	log := logs(t)
	if err := os.WriteFile(filepath.Join(dir, "Top", "a.3mf"), []byte("abc"), 0o644); err != nil {
		t.Fatal(err)
	}
	waitRelease(t, released)
	if err := lib.DeleteFolder(context.Background(), 4); err != nil {
		t.Fatal(err)
	}
	lib.setDown(false)
	eventually(t, "the upload to the top level", func() bool { return libraryHas(lib, map[string]string{"0/a.3mf": "abc"}) })
	if !strings.Contains(log.String(), "top level") {
		t.Fatalf("log: %q", log.String())
	}
}

func TestQueueInodesAreTheirOwnSpace(t *testing.T) {
	for _, id := range []int{0, 1, 1<<31 - 1} {
		for _, seq := range []uint64{1, 2, 1 << 40} {
			if ino := queueIno(seq); ino == dirIno(id) || ino == fileIno(id) || ino >= 1<<63 {
				t.Fatalf("id %d seq %d: %x", id, seq, ino)
			}
		}
	}
}

func TestListUnsent(t *testing.T) {
	state := t.TempDir()
	if q, err := ListUnsent(state); err != nil || len(q) != 0 {
		t.Fatalf("no queue: %v %v", q, err)
	}
	rec := record{FolderID: 4, Name: "a.3mf", Size: 3, Attempts: 2, LastError: "upload: 502 Bad Gateway", Bases: []base{{ID: 10}}}
	path := writeQueued(t, state, 3, rec, "abc")
	data, _ := os.ReadFile(filepath.Join(path, "record.json"))
	var raw map[string]any
	if err := json.Unmarshal(data, &raw); err != nil || raw["folder_id"] != 4.0 || raw["saved_at"] == nil || raw["bases"].([]any)[0].(map[string]any)["id"] != 10.0 {
		t.Fatalf("record.json: %s %v", data, err)
	}
	q, err := ListUnsent(state)
	want := Unsent{Path: path, FolderID: 4, Name: "a.3mf", Size: 3, Attempts: 2, LastError: "upload: 502 Bad Gateway"}
	if err != nil || len(q) != 1 || q[0] != want {
		t.Fatalf("%+v %v", q, err)
	}
}

// The save an app makes with a temp file and a rename, over a file the library has.
func saveByRename(t *testing.T, dir, data string) {
	t.Helper()
	tmp := filepath.Join(dir, "X.3mf.tmp")
	if err := os.WriteFile(tmp, []byte(data), 0o644); err != nil {
		t.Fatalf("write: %v", err)
	}
	if err := syscall.Rename(tmp, filepath.Join(dir, "X.3mf")); err != nil {
		t.Fatalf("rename: %v", err)
	}
}

func TestASaveByRenameReplacesTheStoredFile(t *testing.T) {
	lib := newFakeLibrary()
	lib.folder(4, "Top", 0)
	lib.file(10, "X.3mf", 4, []byte("old"))
	dir, released := retrying(t, lib, Options{StateDir: t.TempDir()})
	top := filepath.Join(dir, "Top")
	if _, err := os.Stat(filepath.Join(top, "X.3mf")); err != nil {
		t.Fatal(err)
	}
	saveByRename(t, top, "new")
	waitRelease(t, released)
	if !libraryHas(lib, map[string]string{"4/X.3mf": "new"}) {
		lib.locked(func() { t.Fatalf("%+v", lib.files) })
	}
	lib.locked(func() {
		if !lib.deleted[10] {
			t.Fatal("old file kept")
		}
	})
	if data, err := os.ReadFile(filepath.Join(top, "X.3mf")); err != nil || string(data) != "new" {
		t.Fatalf("%q %v", data, err)
	}
}

func TestASaveByRenameWhileTheLibraryIsDownIsQueuedOverTheStoredFile(t *testing.T) {
	lib := newFakeLibrary()
	lib.folder(4, "Top", 0)
	lib.file(10, "X.3mf", 4, []byte("old"))
	lib.down, lib.failStatus = true, http.StatusNotFound
	state := t.TempDir()
	dir, released := retrying(t, lib, Options{StateDir: state})
	logs(t)
	top := filepath.Join(dir, "Top")
	if _, err := os.Stat(filepath.Join(top, "X.3mf")); err != nil {
		t.Fatal(err)
	}
	saveByRename(t, top, "new")
	waitRelease(t, released)
	if data, err := os.ReadFile(filepath.Join(top, "X.3mf")); err != nil || string(data) != "new" {
		t.Fatalf("read: %q %v", data, err)
	}
	if got := names(t, top); !slices.Equal(got, []string{"X.3mf"}) {
		t.Fatalf("listed: %v", got)
	}
	if info, err := os.Stat(filepath.Join(top, "X.3mf")); err != nil || info.Size() != 3 {
		t.Fatalf("stat: %v %v", info, err)
	}
	q := queued(t, state)
	if len(q) != 1 || q[0].Name != "X.3mf" || q[0].FolderID != 4 {
		t.Fatalf("queue: %+v", q)
	}
	if rec, err := readRecord(q[0].Path); err != nil || len(rec.Bases) != 1 || rec.Bases[0].ID != 10 || rec.Bases[0].Created.IsZero() {
		t.Fatalf("bases: %+v %v", rec.Bases, err)
	}
	lib.setDown(false)
	eventually(t, "the replace", func() bool { return libraryHas(lib, map[string]string{"4/X.3mf": "new"}) })
	eventually(t, "the queue to empty", func() bool { return len(queued(t, state)) == 0 })
	lib.locked(func() {
		if !lib.deleted[10] {
			t.Fatal("old file kept")
		}
	})
}

func TestMovingAQueuedSaveOverAnotherReplacesBothBases(t *testing.T) {
	lib := newFakeLibrary()
	lib.file(10, "x.3mf", 0, []byte("A"))
	lib.file(11, "y.3mf", 0, []byte("B"))
	lib.down = true
	state := t.TempDir()
	dir, released := retrying(t, lib, Options{StateDir: state})
	logs(t)
	for name, data := range map[string]string{"x.3mf": "x2", "y.3mf": "y2"} {
		if err := os.WriteFile(filepath.Join(dir, name), []byte(data), 0o644); err != nil {
			t.Fatal(err)
		}
		waitRelease(t, released)
	}
	if err := os.Rename(filepath.Join(dir, "x.3mf"), filepath.Join(dir, "y.3mf")); err != nil {
		t.Fatal(err)
	}
	if got := names(t, dir); !slices.Equal(got, []string{"y.3mf"}) {
		t.Fatalf("listed: %v", got)
	}
	if q := queued(t, state); len(q) != 1 || q[0].Name != "y.3mf" {
		t.Fatalf("queue: %+v", q)
	}
	lib.setDown(false)
	eventually(t, "the upload", func() bool { return libraryHas(lib, map[string]string{"0/y.3mf": "x2"}) })
	lib.locked(func() {
		if !lib.deleted[10] || !lib.deleted[11] {
			t.Fatalf("deleted %v", lib.deleted)
		}
	})
}

func TestWritingAQueuedSaveLeavesTheQueuedCopyAlone(t *testing.T) {
	lib := newFakeLibrary()
	lib.down = true
	state := t.TempDir()
	dir, released := retrying(t, lib, Options{StateDir: state})
	logs(t)
	path := filepath.Join(dir, "a.3mf")
	if err := os.WriteFile(path, []byte("abc"), 0o644); err != nil {
		t.Fatal(err)
	}
	waitRelease(t, released)
	first := queued(t, state)[0].Path
	f, err := os.OpenFile(path, os.O_RDWR|os.O_TRUNC, 0)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	if _, err := f.Write([]byte("Z")); err != nil {
		t.Fatal(err)
	}
	if err := f.Sync(); err != nil {
		t.Fatalf("fsync: %v", err)
	}
	// Until the new bytes are durable, the queued copy is untouched.
	if data, err := os.ReadFile(filepath.Join(first, "contents")); err != nil || string(data) != "abc" {
		t.Fatalf("queued copy while writing: %q %v", data, err)
	}
	if err := f.Close(); err != nil {
		t.Fatal(err)
	}
	waitRelease(t, released)
	q := queued(t, state)
	if len(q) != 1 || q[0].Path == first || q[0].Size != 1 {
		t.Fatalf("queue: %+v", q)
	}
	if _, err := os.Stat(first); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("superseded copy: %v", err)
	}
	if data, err := os.ReadFile(path); err != nil || string(data) != "Z" {
		t.Fatalf("read: %q %v", data, err)
	}
	lib.setDown(false)
	eventually(t, "the upload", func() bool { return libraryHas(lib, map[string]string{"0/a.3mf": "Z"}) })
}

func TestAQueuedWriteTheLibraryRefusesIsKeptAside(t *testing.T) {
	lib := newFakeLibrary()
	lib.down = true
	state := t.TempDir()
	dir, released := retrying(t, lib, Options{StateDir: state})
	logs(t)
	if err := os.WriteFile(filepath.Join(dir, "a.3mf"), []byte("abc"), 0o644); err != nil {
		t.Fatal(err)
	}
	waitRelease(t, released)
	lib.locked(func() { lib.failStatus = http.StatusUnprocessableEntity })
	eventually(t, "the refusal", func() bool {
		q := queued(t, state)
		return len(q) == 1 && q[0].Refused
	})
	q := queued(t, state)
	if data, err := os.ReadFile(filepath.Join(q[0].Path, "contents")); err != nil || string(data) != "abc" || !strings.Contains(q[0].LastError, "422") {
		t.Fatalf("kept: %q %v %+v", data, err, q[0])
	}
	if got := names(t, dir); len(got) != 0 {
		t.Fatalf("listed: %v", got)
	}
}

func TestOfTwoReloadedSavesOfOneNameTheLaterIsSent(t *testing.T) {
	lib := newFakeLibrary()
	state := t.TempDir()
	writeQueued(t, state, 1, record{Name: "x.3mf", Size: 3, Attempts: 1, Bases: []base{{ID: 10, Name: "x.3mf"}}}, "one")
	writeQueued(t, state, 2, record{Name: "x.3mf", Size: 3, Attempts: 1}, "two")
	lib.file(10, "x.3mf", 0, []byte("old"))
	logs(t)
	retrying(t, lib, Options{StateDir: state})
	eventually(t, "the later save", func() bool { return libraryHas(lib, map[string]string{"0/x.3mf": "two"}) })
	lib.locked(func() {
		if lib.uploads != 1 || !lib.deleted[10] {
			t.Fatalf("uploads %d deleted %v", lib.uploads, lib.deleted)
		}
	})
}

func TestAStoredFileRenamedWhileOpenIsStillReplaced(t *testing.T) {
	for _, down := range []bool{false, true} {
		lib := newFakeLibrary()
		lib.file(10, "a.3mf", 0, []byte("old"))
		lib.down = down
		state := t.TempDir()
		dir, released := retrying(t, lib, Options{StateDir: state})
		logs(t)
		f, err := os.OpenFile(filepath.Join(dir, "a.3mf"), os.O_WRONLY|os.O_TRUNC, 0)
		if err != nil {
			t.Fatal(err)
		}
		defer f.Close()
		if _, err := f.Write([]byte("new")); err != nil {
			t.Fatal(err)
		}
		if err := os.Rename(filepath.Join(dir, "a.3mf"), filepath.Join(dir, "b.3mf")); err != nil {
			t.Fatal(err)
		}
		if err := f.Close(); err != nil {
			t.Fatalf("down %v: close: %v", down, err)
		}
		waitRelease(t, released)
		if down {
			q := queued(t, state)
			if len(q) != 1 || q[0].Name != "b.3mf" {
				t.Fatalf("queue: %+v", q)
			}
			if rec, err := readRecord(q[0].Path); err != nil || len(rec.Bases) != 1 || rec.Bases[0].ID != 10 || rec.Bases[0].Name != "b.3mf" {
				t.Fatalf("bases: %+v %v", rec.Bases, err)
			}
			lib.setDown(false)
		}
		eventually(t, "the replace", func() bool { return libraryHas(lib, map[string]string{"0/b.3mf": "new"}) })
		lib.locked(func() {
			if !lib.deleted[10] {
				t.Fatalf("down %v: the old file was kept: %+v", down, lib.files)
			}
		})
	}
}

func TestARenameDuringTheUploadThatFailsIsRetriedWithoutSendingAgain(t *testing.T) {
	lib := newFakeLibrary()
	lib.file(10, "y.3mf", 0, []byte("old"))
	lib.down = true
	state := t.TempDir()
	dir, released := retrying(t, lib, Options{StateDir: state})
	log := logs(t)
	if err := os.WriteFile(filepath.Join(dir, "x.3mf"), []byte("new"), 0o644); err != nil {
		t.Fatal(err)
	}
	waitRelease(t, released)
	var once sync.Once
	lib.locked(func() {
		lib.down, lib.failUpdates = false, 1
		lib.uploading = func(int) {
			once.Do(func() {
				// The rename lands while the upload runs, and its follow-up fails once.
				if err := os.Rename(filepath.Join(dir, "x.3mf"), filepath.Join(dir, "y.3mf")); err != nil {
					t.Errorf("rename: %v", err)
				}
			})
		}
	})
	eventually(t, "the renamed upload", func() bool { return libraryHas(lib, map[string]string{"0/y.3mf": "new"}) })
	eventually(t, "the queue to empty", func() bool { return len(queued(t, state)) == 0 })
	lib.locked(func() {
		if lib.uploads != 1 || !lib.deleted[10] {
			t.Fatalf("uploads %d deleted %v", lib.uploads, lib.deleted)
		}
	})
	if !strings.Contains(log.String(), "renamed on the next pass") {
		t.Fatalf("log: %q", log.String())
	}
}

func TestAWriterOfAQueuedSaveKeepsWorkingWhenTheSaveIsRemoved(t *testing.T) {
	lib := newFakeLibrary()
	lib.down = true
	state := t.TempDir()
	dir, released := retrying(t, lib, Options{StateDir: state})
	logs(t)
	path := filepath.Join(dir, "a.3mf")
	if err := os.WriteFile(path, []byte("abc"), 0o644); err != nil {
		t.Fatal(err)
	}
	waitRelease(t, released)
	f, err := os.OpenFile(path, os.O_RDWR, 0)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	if err := os.Remove(path); err != nil {
		t.Fatal(err)
	}
	if _, err := f.WriteAt([]byte("Z"), 0); err != nil {
		t.Fatalf("write after the removal: %v", err)
	}
	buf := make([]byte, 3)
	if _, err := f.ReadAt(buf, 0); err != nil || string(buf) != "Zbc" {
		t.Fatalf("read: %q %v", buf, err)
	}
	if err := f.Close(); err != nil {
		t.Fatalf("close: %v", err)
	}
	waitRelease(t, released)
	if q := queued(t, state); len(q) != 0 {
		t.Fatalf("queue: %+v", q)
	}
}

func TestRemovingAnUploadedFileGoneFromTheLibraryClearsIt(t *testing.T) {
	lib := newFakeLibrary()
	dir, released := releases(t, lib, Options{})
	logs(t)
	path := filepath.Join(dir, "notes.txt")
	if err := os.WriteFile(path, []byte("n"), 0o644); err != nil {
		t.Fatal(err)
	}
	waitRelease(t, released)
	if got := names(t, dir); !slices.Equal(got, []string{"notes.txt"}) {
		t.Fatalf("listed: %v", got)
	}
	lib.remove(101)
	if err := os.Remove(path); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("rm: %v", err)
	}
	if got := names(t, dir); len(got) != 0 {
		t.Fatalf("listed after: %v", got)
	}
}

// renameDuringUpload queues x.3mf with the library down, then brings the library back
// and, during the upload, renames x.3mf to the path to (relative to the mount) and runs
// then.
func renameDuringUpload(t *testing.T, lib *fakeLibrary, opts Options, to string, then func()) string {
	t.Helper()
	lib.down = true
	dir, released := retrying(t, lib, opts)
	if err := os.WriteFile(filepath.Join(dir, "x.3mf"), []byte("new"), 0o644); err != nil {
		t.Fatal(err)
	}
	waitRelease(t, released)
	var once sync.Once
	lib.locked(func() {
		lib.down = false
		lib.uploading = func(int) {
			once.Do(func() {
				if err := os.Rename(filepath.Join(dir, "x.3mf"), filepath.Join(dir, to)); err != nil {
					t.Errorf("rename: %v", err)
				}
				then()
			})
		}
	})
	return dir
}

func TestAWriteRenamedToAnUnlistedNameAfterAFailedRenameReadsBack(t *testing.T) {
	lib := newFakeLibrary()
	state := t.TempDir()
	logs(t)
	dir := renameDuringUpload(t, lib, Options{StateDir: state}, "y.txt", func() {
		lib.locked(func() { lib.failUpdates = 1 })
	})
	eventually(t, "the rename", func() bool { return libraryHas(lib, map[string]string{"0/y.txt": "new"}) })
	eventually(t, "the queue to empty", func() bool { return len(queued(t, state)) == 0 })
	path := filepath.Join(dir, "y.txt")
	if info, err := os.Stat(path); err != nil || info.Size() != 3 || info.ModTime().Year() < 2000 {
		t.Fatalf("stat: %v %v", info, err)
	}
	if data, err := os.ReadFile(path); err != nil || string(data) != "new" {
		t.Fatalf("read: %q %v", data, err)
	}
}

func TestARenameIntoAFolderGoneMeanwhileGoesToTheTop(t *testing.T) {
	lib := newFakeLibrary()
	lib.folder(4, "Top", 0)
	state := t.TempDir()
	log := logs(t)
	renameDuringUpload(t, lib, Options{StateDir: state}, filepath.Join("Top", "x.3mf"), func() {
		if err := lib.DeleteFolder(context.Background(), 4); err != nil {
			t.Error(err)
		}
	})
	eventually(t, "the queue to empty", func() bool { return len(queued(t, state)) == 0 })
	if !libraryHas(lib, map[string]string{"0/x.3mf": "new"}) {
		lib.locked(func() { t.Fatalf("%+v", lib.files) })
	}
	lib.locked(func() {
		if lib.uploads != 1 {
			t.Fatalf("uploads %d", lib.uploads)
		}
	})
	if !strings.Contains(log.String(), "moved to the top level") {
		t.Fatalf("log: %q", log.String())
	}
}

func TestASentWriteGoneBeforeItsRenameIsSentAgain(t *testing.T) {
	lib := newFakeLibrary()
	state := t.TempDir()
	logs(t)
	renameDuringUpload(t, lib, Options{StateDir: state}, "y.3mf", func() {
		lib.locked(func() {
			lib.failUpdates = 1
			lib.updating = func(n int) {
				if n == 2 {
					// Between the failed rename and its retry, the sent file is deleted.
					lib.remove(101)
				}
			}
		})
	})
	eventually(t, "the second upload", func() bool { return libraryHas(lib, map[string]string{"0/y.3mf": "new"}) })
	eventually(t, "the queue to empty", func() bool { return len(queued(t, state)) == 0 })
	lib.locked(func() {
		if lib.uploads != 2 {
			t.Fatalf("uploads %d", lib.uploads)
		}
	})
}

// A deploy's minutes of 503s: listings and uploads both fail. The last listings are
// served, so a slicer's save (write X.3mf.tmp, close, rename over the stored X.3mf)
// succeeds and queues over the stored file, and replaces it once ScadBuddy is back.
func TestASaveByRenameWhileScadBuddyIsDownIsQueuedAndSentLater(t *testing.T) {
	lib := newFakeLibrary()
	lib.folder(4, "Top", 0)
	lib.file(10, "X.3mf", 4, []byte("old"))
	lib.file(11, "Y.3mf", 4, []byte("other"))
	state := t.TempDir()
	dir, released := retrying(t, lib, Options{StateDir: state})
	log := logs(t)
	top := filepath.Join(dir, "Top")
	if got := names(t, top); !slices.Equal(got, []string{"X.3mf", "Y.3mf"}) {
		t.Fatalf("listed: %v", got)
	}
	lib.setAllDown(true)
	// The TTL is 0: every lookup and listing below asks ScadBuddy, and is refused.
	if got := names(t, top); !slices.Equal(got, []string{"X.3mf", "Y.3mf"}) {
		t.Fatalf("listed while down: %v", got)
	}
	if data, err := os.ReadFile(filepath.Join(top, "Y.3mf")); err != nil || string(data) != "other" {
		t.Fatalf("read while down: %q %v", data, err)
	}
	saveByRename(t, top, "new")
	waitRelease(t, released)
	if data, err := os.ReadFile(filepath.Join(top, "X.3mf")); err != nil || string(data) != "new" {
		t.Fatalf("read back: %q %v", data, err)
	}
	if got := names(t, top); !slices.Equal(got, []string{"X.3mf", "Y.3mf"}) {
		t.Fatalf("listed after the save: %v", got)
	}
	q := queued(t, state)
	if len(q) != 1 || q[0].Name != "X.3mf" || q[0].FolderID != 4 {
		t.Fatalf("queue: %+v", q)
	}
	if rec, err := readRecord(q[0].Path); err != nil || len(rec.Bases) != 1 || rec.Bases[0].ID != 10 {
		t.Fatalf("bases: %+v %v", rec.Bases, err)
	}
	if n := strings.Count(log.String(), "ScadBuddy is not answering"); n != 1 {
		t.Fatalf("outage logged %d times: %s", n, log.String())
	}
	lib.setAllDown(false)
	eventually(t, "the replace", func() bool { return libraryHas(lib, map[string]string{"4/X.3mf": "new", "4/Y.3mf": "other"}) })
	eventually(t, "the queue to empty", func() bool { return len(queued(t, state)) == 0 })
	lib.locked(func() {
		if !lib.deleted[10] {
			t.Fatal("old file kept")
		}
	})
	if got := names(t, top); !slices.Equal(got, []string{"X.3mf", "Y.3mf"}) {
		t.Fatalf("listed after: %v", got)
	}
	if !strings.Contains(log.String(), "ScadBuddy answers again") {
		t.Fatalf("recovery not logged: %s", log.String())
	}
}

// At a start with ScadBuddy down there is no listing to serve: the top level is EIO.
func TestAFolderNeverListedIsEIOWhileScadBuddyIsDown(t *testing.T) {
	lib := newFakeLibrary()
	lib.setAllDown(true)
	dir, _ := retrying(t, lib, Options{StateDir: t.TempDir()})
	logs(t)
	if _, err := os.ReadDir(dir); !errors.Is(err, syscall.EIO) {
		t.Fatalf("%v", err)
	}
}

// The upload runs but its answer is lost (a deploy cuts the connection): the write is
// queued with its Idempotency-Key, and the retry sends the same request with it, so
// the route answers with the first upload's file, which then replaces the old one.
func TestARetryAfterALostAnswerDoesNotUploadTwice(t *testing.T) {
	lib := newFakeLibrary()
	lib.folder(4, "Top", 0)
	lib.file(10, "X.3mf", 4, []byte("old"))
	lib.loseAnswers = 1
	state := t.TempDir()
	dir, released := retrying(t, lib, Options{StateDir: state})
	log := logs(t)
	if err := os.WriteFile(filepath.Join(dir, "Top", "X.3mf"), []byte("new"), 0o644); err != nil {
		t.Fatal(err)
	}
	waitRelease(t, released)
	eventually(t, "the replace", func() bool { return libraryHas(lib, map[string]string{"4/X.3mf": "new"}) })
	eventually(t, "the queue to empty", func() bool { return len(queued(t, state)) == 0 })
	lib.locked(func() {
		if lib.uploads != 1 || !lib.deleted[10] {
			t.Fatalf("uploads %d, old deleted %v: %+v", lib.uploads, lib.deleted[10], lib.files)
		}
		if len(lib.keys) < 2 || slices.ContainsFunc(lib.keys, func(k string) bool { return k != lib.keys[0] }) || len(lib.keys[0]) != 32 {
			t.Fatalf("keys: %q", lib.keys)
		}
	})
	if strings.Contains(log.String(), "conflict copy") {
		t.Fatalf("log: %s", log.String())
	}
}

// A record from a build before the key gets one when it is loaded, kept on disk and
// sent with every attempt.
func TestAnOlderRecordGetsAKeyWhenLoaded(t *testing.T) {
	lib := newFakeLibrary()
	lib.folder(4, "Top", 0)
	lib.down = true
	state := t.TempDir()
	path := writeQueued(t, state, 1, record{FolderID: 4, Name: "a.3mf", Size: 3, SavedAt: time.Now()}, "abc")
	retrying(t, lib, Options{StateDir: state})
	logs(t)
	rec, err := readRecord(path)
	if err != nil || rec.Attempt == nil || len(rec.Attempt.Key) != 32 {
		t.Fatalf("record: %+v %v", rec, err)
	}
	eventually(t, "two attempts", func() bool {
		var n int
		lib.locked(func() { n = len(lib.keys) })
		return n >= 2
	})
	lib.setDown(false)
	eventually(t, "the upload", func() bool { return libraryHas(lib, map[string]string{"4/a.3mf": "abc"}) })
	lib.locked(func() {
		for _, k := range lib.keys {
			if k != rec.Attempt.Key {
				t.Fatalf("keys %q, record's %q", lib.keys, rec.Attempt.Key)
			}
		}
	})
}

// The file a lost answer made is deleted before the retry: the route answers the
// repeated key with that gone file, so the write goes again under a new key and the
// old file is replaced, never left as the only copy deleted (review N-1).
func TestARetryWhoseLostUploadWasDeletedSendsAgain(t *testing.T) {
	lib := newFakeLibrary()
	lib.folder(4, "Top", 0)
	lib.file(10, "X.3mf", 4, []byte("old"))
	lib.loseAnswers = 1
	lib.uploading = func(attempt int) {
		if attempt != 2 {
			return
		}
		var lost int
		lib.locked(func() {
			for id, f := range lib.files {
				if f.Name == "X.3mf" && id != 10 {
					lost = id
				}
			}
		})
		if err := lib.DeleteFiles(context.Background(), lost); err != nil {
			t.Errorf("deleting the lost upload %d: %v", lost, err)
		}
	}
	state := t.TempDir()
	dir, released := retrying(t, lib, Options{StateDir: state})
	logs(t)
	if err := os.WriteFile(filepath.Join(dir, "Top", "X.3mf"), []byte("new"), 0o644); err != nil {
		t.Fatal(err)
	}
	waitRelease(t, released)
	eventually(t, "the replace", func() bool { return libraryHas(lib, map[string]string{"4/X.3mf": "new"}) })
	eventually(t, "the queue to empty", func() bool { return len(queued(t, state)) == 0 })
	lib.locked(func() {
		if lib.uploads != 2 || !lib.deleted[10] {
			t.Fatalf("uploads %d, old deleted %v: %+v", lib.uploads, lib.deleted[10], lib.files)
		}
		if n := len(lib.keys); n < 3 || lib.keys[n-1] == lib.keys[0] {
			t.Fatalf("keys: %q", lib.keys)
		}
	})
}
