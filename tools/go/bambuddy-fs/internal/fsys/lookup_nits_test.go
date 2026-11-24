package fsys

import (
	"context"
	"syscall"
	"testing"
	"time"

	"github.com/hanwen/go-fuse/v2/fuse"
)

// With a TTL of 0 a lookup of a missing name lists the folder once.
func TestAMissWithNoTTLListsOnce(t *testing.T) {
	lib := newFakeLibrary()
	lib.file(10, "a.3mf", 0, []byte("a"))
	root := NewRoot(lib, Options{CacheDir: t.TempDir(), StateDir: t.TempDir()}).(*dir)
	if _, errno := root.Lookup(context.Background(), "missing", &fuse.EntryOut{}); errno != syscall.ENOENT {
		t.Fatal(errno)
	}
	if n := lists(lib); n != 1 {
		t.Fatalf("%d lists for one miss", n)
	}
}

// While ScadBuddy is down, an expired folder starts no new refresh right after one failed.
func TestADownScadBuddyIsNotRefreshedAtOnce(t *testing.T) {
	logs(t)
	lib := newFakeLibrary()
	lib.file(10, "a.3mf", 0, []byte("a"))
	tr := newTree(t, lib, time.Minute)
	if _, errno := tr.list(context.Background(), 0, anyAge); errno != 0 {
		t.Fatal(errno)
	}
	age(tr, 0)
	lib.setAllDown(true)
	tr.list(context.Background(), 0, anyAge)
	eventually(t, "the failed refresh", func() bool {
		tr.mu.Lock()
		defer tr.mu.Unlock()
		return tr.down && len(tr.fetches) == 0
	})
	before := lists(lib)
	for range 5 {
		if _, errno := tr.list(context.Background(), 0, anyAge); errno != 0 {
			t.Fatal(errno)
		}
	}
	time.Sleep(100 * time.Millisecond)
	if n := lists(lib); n != before {
		t.Fatalf("%d refreshes inside the backoff", n-before)
	}
}

// A miss in an expired listing waits for a current one, and that wait is traced.
func TestAMissThatWaitsForTheRetryHasASpan(t *testing.T) {
	spans, _ := recording(t)
	lib := newFakeLibrary()
	lib.file(10, "a.3mf", 0, []byte("a"))
	root := NewRoot(lib, Options{TTL: time.Minute, CacheDir: t.TempDir(), StateDir: t.TempDir()}).(*dir)
	if _, errno := root.tree.list(context.Background(), 0, anyAge); errno != 0 {
		t.Fatal(errno)
	}
	age(root.tree, 0)
	release := holdLists(lib, 2)
	defer release()
	done := make(chan syscall.Errno, 1)
	go func() {
		_, errno := root.Lookup(context.Background(), "missing", &fuse.EntryOut{})
		done <- errno
	}()
	eventually(t, "the refresh", func() bool { return lists(lib) >= 2 })
	release()
	if errno := <-done; errno != syscall.ENOENT {
		t.Fatal(errno)
	}
	if spanNames(spans)["fuse.lookup"] == 0 {
		t.Fatal("no lookup span for a lookup that waited")
	}
}
