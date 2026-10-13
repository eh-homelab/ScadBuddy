package fsys

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
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
	if err != nil || rec.Base != nil || rec.SavedAt.IsZero() {
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
	if before.Base == nil || before.Base.ID != 10 || before.Base.Created.IsZero() {
		t.Fatalf("b's base: %+v", before.Base)
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
		if rec, _ := readRecord(u.Path); u.Name == "b.3mf" && (rec.Base == nil || *rec.Base != *before.Base) {
			t.Fatalf("base after the restart: %+v", rec.Base)
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
	q := newQueue(filepath.Join(t.TempDir(), "unsent"))
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
	rec := record{FolderID: 4, Name: "a.3mf", Size: 3, Attempts: 2, LastError: "upload: 502 Bad Gateway", Base: &base{ID: 10}}
	path := writeQueued(t, state, 3, rec, "abc")
	data, _ := os.ReadFile(filepath.Join(path, "record.json"))
	var raw map[string]any
	if err := json.Unmarshal(data, &raw); err != nil || raw["folder_id"] != 4.0 || raw["saved_at"] == nil || raw["base"].(map[string]any)["id"] != 10.0 {
		t.Fatalf("record.json: %s %v", data, err)
	}
	q, err := ListUnsent(state)
	want := Unsent{Path: path, FolderID: 4, Name: "a.3mf", Size: 3, Attempts: 2, LastError: "upload: 502 Bad Gateway"}
	if err != nil || len(q) != 1 || q[0] != want {
		t.Fatalf("%+v %v", q, err)
	}
}
