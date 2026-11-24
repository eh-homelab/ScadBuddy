package fsys

import (
	"errors"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
)

// Without --write-back, close waits for the upload and returns its error.
func TestWithoutWriteBackCloseWaitsForTheUpload(t *testing.T) {
	lib := newFakeLibrary()
	lib.failUploads, lib.failStatus = 1, http.StatusRequestEntityTooLarge
	state := t.TempDir()
	dir, released := releases(t, lib, Options{StateDir: state})
	logs(t)
	if err := os.WriteFile(filepath.Join(dir, "a.3mf"), []byte("abc"), 0o644); !errors.Is(err, syscall.EFBIG) {
		t.Fatalf("close: %v", err)
	}
	waitRelease(t, released)
	if err := os.WriteFile(filepath.Join(dir, "b.3mf"), []byte("def"), 0o644); err != nil {
		t.Fatalf("close: %v", err)
	}
	lib.locked(func() {
		if lib.uploads != 2 {
			t.Fatalf("uploads at close %d", lib.uploads)
		}
	})
	waitRelease(t, released)
	if q := queued(t, state); len(q) != 0 {
		t.Fatalf("queue: %+v", q)
	}
}

// With --write-back, close returns once the contents are queued, before any upload,
// and the retrier sends them at once.
func TestWriteBackCloseQueuesAndTheRetrierSends(t *testing.T) {
	lib := newFakeLibrary()
	lib.folder(4, "Top", 0)
	block := make(chan struct{})
	lib.uploading = func(int) { <-block }
	state := t.TempDir()
	dir, released := retrying(t, lib, Options{StateDir: state, WriteBack: true})
	logs(t)
	path := filepath.Join(dir, "Top", "a.3mf")
	if err := os.WriteFile(path, []byte("abc"), 0o644); err != nil {
		close(block)
		t.Fatalf("close: %v", err)
	}
	q := queued(t, state)
	close(block)
	if len(q) != 1 || q[0].FolderID != 4 || q[0].Name != "a.3mf" || q[0].Size != 3 || q[0].Attempts != 0 || q[0].LastError != "" {
		t.Fatalf("queue at close: %+v", q)
	}
	if data, err := os.ReadFile(path); err != nil || string(data) != "abc" {
		t.Fatalf("read while queued: %q %v", data, err)
	}
	waitRelease(t, released)
	eventually(t, "the upload", func() bool { return libraryHas(lib, map[string]string{"4/a.3mf": "abc"}) })
	eventually(t, "the queue to empty", func() bool { return len(queued(t, state)) == 0 })
	lib.locked(func() {
		if lib.uploads != 1 {
			t.Fatalf("uploads %d", lib.uploads)
		}
	})
}

// With --write-back, a failed upload is not close's error: the retrier retries a
// transient failure until it is sent, and keeps a refusal under refused/.
func TestWriteBackFailedUploadsAreTheQueuesOnly(t *testing.T) {
	lib := newFakeLibrary()
	lib.setDown(true)
	state := t.TempDir()
	dir, _ := retrying(t, lib, Options{StateDir: state, WriteBack: true})
	log := logs(t)
	if err := os.WriteFile(filepath.Join(dir, "a.3mf"), []byte("abc"), 0o644); err != nil {
		t.Fatalf("close: %v", err)
	}
	eventually(t, "a failed attempt", func() bool {
		q := queued(t, state)
		return len(q) == 1 && q[0].Attempts >= 1 && strings.Contains(q[0].LastError, "502")
	})
	if got := log.String(); !strings.Contains(got, "sending an unsent write failed") {
		t.Fatalf("log: %q", got)
	}
	lib.setDown(false)
	eventually(t, "the upload", func() bool { return libraryHas(lib, map[string]string{"0/a.3mf": "abc"}) })
	eventually(t, "the queue to empty", func() bool { return len(queued(t, state)) == 0 })

	lib.locked(func() { lib.failUploads, lib.failStatus = 1, http.StatusRequestEntityTooLarge })
	if err := os.WriteFile(filepath.Join(dir, "b.3mf"), []byte("def"), 0o644); err != nil {
		t.Fatalf("close of a write the library refuses: %v", err)
	}
	eventually(t, "the refusal kept aside", func() bool {
		q := queued(t, state)
		return len(q) == 1 && q[0].Refused && q[0].Name == "b.3mf" && strings.Contains(q[0].LastError, "413")
	})
}

// A write queued before any attempt is due as soon as its writer lets it go, not a
// backoff later.
func TestAWriteQueuedBeforeAnyAttemptIsDueAtOnce(t *testing.T) {
	q := newQueue(t.TempDir())
	writeQueued(t, filepath.Dir(q.dir), 1, record{Name: "a.3mf"}, "a")
	q.load()
	e := q.due()[0]
	q.reschedule(e)
	if len(q.due()) != 1 || q.untilNext() != 0 {
		t.Fatalf("due %v, next in %v", q.due(), q.untilNext())
	}
}

// A flush that queues under --write-back says so on its span, and each queued write is
// counted by why it was queued.
func TestWriteBackIsTraced(t *testing.T) {
	spans, reader := recording(t)
	logs(t)
	lib := newFakeLibrary()
	lib.setDown(true)
	dir, released := releases(t, lib, Options{WriteBack: true})
	if err := os.WriteFile(filepath.Join(dir, "a.3mf"), []byte("abc"), 0o644); err != nil {
		t.Fatal(err)
	}
	waitRelease(t, released)
	flushed := false
	for _, s := range spans.Ended() {
		if s.Name() == "fuse.flush" && hasAttr(s.Attributes(), "bambuddy_fs.write_back", "true") {
			flushed = true
		}
	}
	if !flushed {
		t.Fatalf("no write-back flush span: %v", spanNames(spans))
	}
	attrs, values := collect(t, reader)
	if values["bambuddy_fs.unsent.queued"] != 1 || len(attrs["bambuddy_fs.unsent.queued"]) != 1 ||
		!hasAttr(attrs["bambuddy_fs.unsent.queued"][0], "reason", "write_back") {
		t.Fatalf("unsent.queued: %v %v", attrs["bambuddy_fs.unsent.queued"], values["bambuddy_fs.unsent.queued"])
	}
}

// A file removed while open under --write-back queues nothing, so its flush span does
// not say write_back.
func TestARemovedWriteBackFileIsNotTracedAsQueued(t *testing.T) {
	spans, _ := recording(t)
	logs(t)
	lib := newFakeLibrary()
	dir, released := releases(t, lib, Options{WriteBack: true})
	path := filepath.Join(dir, "a.3mf")
	f, err := os.Create(path)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := f.Write([]byte("abc")); err != nil {
		t.Fatal(err)
	}
	if err := os.Remove(path); err != nil {
		t.Fatal(err)
	}
	if err := f.Close(); err != nil {
		t.Fatal(err)
	}
	waitRelease(t, released)
	for _, s := range spans.Ended() {
		if s.Name() == "fuse.flush" && hasAttr(s.Attributes(), "bambuddy_fs.write_back", "true") {
			t.Fatalf("a removed file's flush says write_back: %v", s.Attributes())
		}
	}
}

// A write-back file not yet sent renames and removes as any queued write does: the
// retrier sends it under its final name, and a removed one is never sent.
func TestAWriteBackFileRenamesAndRemovesBeforeItIsSent(t *testing.T) {
	lib := newFakeLibrary()
	lib.setDown(true)
	state := t.TempDir()
	dir, _ := retrying(t, lib, Options{StateDir: state, WriteBack: true})
	logs(t)
	for _, name := range []string{"a.3mf", "c.3mf"} {
		if err := os.WriteFile(filepath.Join(dir, name), []byte("abc"), 0o644); err != nil {
			t.Fatalf("close %s: %v", name, err)
		}
	}
	eventually(t, "both queued", func() bool { return len(queued(t, state)) == 2 })
	if err := os.Rename(filepath.Join(dir, "a.3mf"), filepath.Join(dir, "b.3mf")); err != nil {
		t.Fatalf("rename: %v", err)
	}
	if err := os.Remove(filepath.Join(dir, "c.3mf")); err != nil {
		t.Fatalf("remove: %v", err)
	}
	eventually(t, "the record renamed and the removed one gone", func() bool {
		q := queued(t, state)
		return len(q) == 1 && q[0].Name == "b.3mf"
	})
	lib.setDown(false)
	eventually(t, "the upload under its final name", func() bool { return libraryHas(lib, map[string]string{"0/b.3mf": "abc"}) })
	eventually(t, "the queue to empty", func() bool { return len(queued(t, state)) == 0 })
}
