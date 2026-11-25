package fsys

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"
)

// newTree is the tree NewRoot makes, unmounted.
func newTree(t *testing.T, lib Library, ttl time.Duration) *tree {
	t.Helper()
	return NewRoot(lib, Options{TTL: ttl, CacheDir: t.TempDir(), StateDir: t.TempDir()}).(*dir).tree
}

// age makes a folder's listing older than any TTL, as if listed long ago.
func age(tr *tree, folderID int) {
	tr.mu.Lock()
	l := tr.listings[folderID]
	l.at = time.Now().Add(-24 * time.Hour)
	tr.listings[folderID] = l
	tr.mu.Unlock()
}

// holdLists has every List from the n'th on wait until the returned function runs.
func holdLists(lib *fakeLibrary, from int) (release func()) {
	gate := make(chan struct{})
	lib.locked(func() {
		lib.listing = func(n int) {
			if n >= from {
				<-gate
			}
		}
	})
	var once sync.Once
	return func() { once.Do(func() { close(gate) }) }
}

func lists(lib *fakeLibrary) int {
	n, _ := lib.counts()
	return n
}

func fileNames(l listing) []string {
	return slices.Sorted(func(yield func(string) bool) {
		for name := range l.files {
			if !yield(name) {
				return
			}
		}
	})
}

// listAll lists folderID from n goroutines at once and returns their answers.
func listAll(tr *tree, folderID, n int) <-chan []string {
	out := make(chan []string, n)
	for range n {
		go func() {
			l, errno := tr.list(context.Background(), folderID, anyAge)
			if errno != 0 {
				out <- []string{errno.Error()}
				return
			}
			out <- fileNames(l)
		}()
	}
	return out
}

// Concurrent uses of a folder never listed wait for one request and share its answer.
func TestConcurrentListsOfANewFolderShareOneRequest(t *testing.T) {
	lib := newFakeLibrary()
	lib.file(10, "a.3mf", 0, []byte("a"))
	tr := newTree(t, lib, time.Minute)
	release := holdLists(lib, 1)
	defer release()
	got := listAll(tr, 0, 8)
	eventually(t, "the first list", func() bool { return lists(lib) == 1 })
	time.Sleep(50 * time.Millisecond)
	select {
	case names := <-got:
		t.Fatalf("answered before the listing: %v", names)
	default:
	}
	release()
	for range 8 {
		if names := <-got; !slices.Equal(names, []string{"a.3mf"}) {
			t.Fatalf("listed: %v", names)
		}
	}
	if n := lists(lib); n != 1 {
		t.Fatalf("listed %d times", n)
	}
}

// A listing older than the TTL is served at once, while one background refresh runs
// however many use it, and the refresh is a span and counted.
func TestAStaleListingIsServedWhileOneRefreshRuns(t *testing.T) {
	spans, reader := recording(t)
	lib := newFakeLibrary()
	lib.file(10, "a.3mf", 0, []byte("a"))
	tr := newTree(t, lib, time.Minute)
	if _, errno := tr.list(context.Background(), 0, anyAge); errno != 0 {
		t.Fatal(errno)
	}
	age(tr, 0)
	lib.file(11, "b.3mf", 0, []byte("b"))
	release := holdLists(lib, 2)
	defer release()
	got := listAll(tr, 0, 8)
	for range 8 {
		select {
		case names := <-got:
			if !slices.Equal(names, []string{"a.3mf"}) {
				t.Fatalf("served: %v", names)
			}
		case <-time.After(5 * time.Second):
			t.Fatal("a stale listing waited for the server")
		}
	}
	eventually(t, "the refresh", func() bool { return lists(lib) == 2 })
	for range 4 {
		if _, errno := tr.list(context.Background(), 0, anyAge); errno != 0 {
			t.Fatal(errno)
		}
	}
	time.Sleep(50 * time.Millisecond)
	if n := lists(lib); n != 2 {
		t.Fatalf("listed %d times", n)
	}
	release()
	eventually(t, "the refreshed listing", func() bool {
		l, errno := tr.list(context.Background(), 0, anyAge)
		return errno == 0 && slices.Equal(fileNames(l), []string{"a.3mf", "b.3mf"})
	})
	if n := lists(lib); n != 2 {
		t.Fatalf("listed %d times after the refresh", n)
	}
	eventually(t, "the refresh span", func() bool { return spanNames(spans)["listing.refresh"] == 1 })
	for _, s := range spans.Ended() {
		if s.Name() == "listing.refresh" && !hasAttr(s.Attributes(), "bambuddy_fs.folder.id", "0") {
			t.Fatalf("refresh attributes: %v", s.Attributes())
		}
	}
	attrs, values := collect(t, reader)
	if values["bambuddy_fs.listing.refreshes"] != 1 || len(attrs["bambuddy_fs.listing.refreshes"]) != 1 ||
		attrs["bambuddy_fs.listing.refreshes"][0][0].Value.AsString() != "ok" {
		t.Fatalf("refreshes: %v %v", attrs["bambuddy_fs.listing.refreshes"], values)
	}
}

// A write forgets its folder: the next use waits for a listing asked for after it,
// and an answer from before the write, still in flight, never replaces that one.
func TestAForgottenListingIsNotServedStale(t *testing.T) {
	lib := newFakeLibrary()
	lib.file(10, "a.3mf", 0, []byte("a"))
	tr := newTree(t, lib, time.Minute)
	if _, errno := tr.list(context.Background(), 0, anyAge); errno != 0 {
		t.Fatal(errno)
	}
	age(tr, 0)
	gate := make(chan struct{})
	lib.locked(func() {
		lib.listing = func(n int) {
			if n == 2 {
				<-gate
			}
		}
	})
	defer close(gate)
	// The refresh (list 2) reads the library before the write and answers after it.
	if _, errno := tr.list(context.Background(), 0, anyAge); errno != 0 {
		t.Fatal(errno)
	}
	eventually(t, "the refresh", func() bool { return lists(lib) == 2 })
	lib.file(11, "b.3mf", 0, []byte("b"))
	tr.forget(0)
	l, errno := tr.list(context.Background(), 0, anyAge)
	if errno != 0 || !slices.Equal(fileNames(l), []string{"a.3mf", "b.3mf"}) {
		t.Fatalf("after the write: %v %v", fileNames(l), errno)
	}
	gate <- struct{}{}
	time.Sleep(50 * time.Millisecond)
	tr.mu.Lock()
	kept := fileNames(tr.listings[0])
	tr.mu.Unlock()
	if !slices.Equal(kept, []string{"a.3mf", "b.3mf"}) {
		t.Fatalf("the answer from before the write replaced the listing: %v", kept)
	}
}

// While ScadBuddy does not answer, the last listing is served, stale or forgotten.
func TestTheLastListingIsServedWhileScadBuddyIsDown(t *testing.T) {
	log := logs(t)
	lib := newFakeLibrary()
	lib.file(10, "a.3mf", 0, []byte("a"))
	tr := newTree(t, lib, time.Minute)
	if _, errno := tr.list(context.Background(), 0, anyAge); errno != 0 {
		t.Fatal(errno)
	}
	age(tr, 0)
	lib.setAllDown(true)
	if l, errno := tr.list(context.Background(), 0, anyAge); errno != 0 || !slices.Equal(fileNames(l), []string{"a.3mf"}) {
		t.Fatalf("stale while down: %v %v", fileNames(l), errno)
	}
	eventually(t, "the failed refresh", func() bool {
		tr.mu.Lock()
		defer tr.mu.Unlock()
		return tr.down && len(tr.fetches) == 0
	})
	tr.forget(0)
	if l, errno := tr.list(context.Background(), 0, anyAge); errno != 0 || !slices.Equal(fileNames(l), []string{"a.3mf"}) {
		t.Fatalf("forgotten while down: %v %v", fileNames(l), errno)
	}
	// The refresh logs the outage after it releases the tree's lock, which the wait above
	// takes: wait for the line, then count it (#7589).
	eventually(t, "the outage logged", func() bool { return strings.Contains(log.String(), "ScadBuddy is not answering") })
	if n := strings.Count(log.String(), "ScadBuddy is not answering"); n != 1 {
		t.Fatalf("logged %d times:\n%s", n, log)
	}
	if _, errno := tr.list(context.Background(), 4, anyAge); errno == 0 {
		t.Fatal("a folder never listed answered while down")
	}
	lib.setAllDown(false)
	tr.forget(0)
	if _, errno := tr.list(context.Background(), 0, anyAge); errno != 0 {
		t.Fatal(errno)
	}
	eventually(t, "the recovery logged", func() bool { return strings.Contains(log.String(), "ScadBuddy answers again") })
}

// With a TTL that keeps listings, each write through the mount shows at the writer's
// next lookup and listing.
func TestWritesShowAtOnceUnderALongTTL(t *testing.T) {
	lib := newFakeLibrary()
	lib.file(10, "a.3mf", 0, []byte("a"))
	dir := mount(t, lib, Options{TTL: time.Hour})
	if got := names(t, dir); !slices.Equal(got, []string{"a.3mf"}) {
		t.Fatalf("listed: %v", got)
	}
	if err := os.WriteFile(filepath.Join(dir, "b.3mf"), []byte("b"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(filepath.Join(dir, "Sub"), 0o755); err != nil {
		t.Fatal(err)
	}
	if got := names(t, dir); !slices.Equal(got, []string{"Sub", "a.3mf", "b.3mf"}) {
		t.Fatalf("after create and mkdir: %v", got)
	}
	if err := os.Rename(filepath.Join(dir, "b.3mf"), filepath.Join(dir, "Sub", "c.3mf")); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(dir, "Sub", "c.3mf")); err != nil {
		t.Fatal(err)
	}
	if err := os.Remove(filepath.Join(dir, "a.3mf")); err != nil {
		t.Fatal(err)
	}
	if got := names(t, dir); !slices.Equal(got, []string{"Sub"}) {
		t.Fatalf("after rename and unlink: %v", got)
	}
	if err := os.Remove(filepath.Join(dir, "Sub", "c.3mf")); err != nil {
		t.Fatal(err)
	}
	if err := os.Remove(filepath.Join(dir, "Sub")); err != nil {
		t.Fatal(err)
	}
	if got := names(t, dir); len(got) != 0 {
		t.Fatalf("after rmdir: %v", got)
	}
}

// renameIn renames a file in the library, as the ScadBuddy UI does.
func renameIn(lib *fakeLibrary, id int, name string) {
	lib.locked(func() {
		f := lib.files[id]
		f.Name = name
		lib.files[id] = f
	})
}

func hasFile(lib *fakeLibrary, id int) (ok bool) {
	lib.locked(func() { _, ok = lib.files[id] })
	return ok
}

// The checks a write makes before it changes the library wait for a listing younger
// than the TTL: an expired one may name a file renamed or added since.
func TestWritesWaitForACurrentListing(t *testing.T) {
	ctx := context.Background()
	lib := newFakeLibrary()
	lib.file(10, "a.3mf", 0, []byte("a"))
	tr := newTree(t, lib, time.Hour)
	root := &dir{tree: tr}
	if _, errno := tr.list(ctx, 0, anyAge); errno != 0 {
		t.Fatal(errno)
	}

	age(tr, 0)
	renameIn(lib, 10, "b.3mf")
	if errno := root.Unlink(ctx, "a.3mf"); errno != syscall.ENOENT {
		t.Fatalf("unlink of a name renamed since: %v", errno)
	}
	if !hasFile(lib, 10) {
		t.Fatal("unlink deleted the renamed file")
	}

	age(tr, 0)
	lib.file(11, "x.3mf", 0, []byte("x"))
	if errno := root.Rename(ctx, "b.3mf", root, "x.3mf", renameNoReplace); errno != syscall.EEXIST {
		t.Fatalf("rename over a file added since: %v", errno)
	}
	age(tr, 0)
	renameIn(lib, 10, "c.3mf")
	if errno := root.Rename(ctx, "b.3mf", root, "d.3mf", 0); errno != syscall.ENOENT {
		t.Fatalf("rename of a name renamed since: %v", errno)
	}

	age(tr, 0)
	lib.file(12, "y.3mf", 0, []byte("y"))
	if errno := root.free(ctx, root, "y.3mf"); errno != syscall.EEXIST {
		t.Fatalf("create over a file added since: %v", errno)
	}

	age(tr, 0)
	lib.folder(5, "Sub", 0)
	if errno := root.Rmdir(ctx, "Sub"); errno != 0 {
		t.Fatalf("rmdir of a folder added since: %v", errno)
	}

	// A write joins the refresh in flight rather than asking again.
	age(tr, 0)
	before := lists(lib) + 1
	release := holdLists(lib, before)
	defer release()
	if _, errno := tr.list(ctx, 0, anyAge); errno != 0 {
		t.Fatal(errno)
	}
	eventually(t, "the refresh", func() bool { return lists(lib) == before })
	done := make(chan syscall.Errno, 1)
	go func() { done <- root.free(ctx, root, "z.3mf") }()
	select {
	case errno := <-done:
		t.Fatalf("answered from the expired listing: %v", errno)
	case <-time.After(50 * time.Millisecond):
	}
	release()
	if errno := <-done; errno != 0 {
		t.Fatal(errno)
	}
	if n := lists(lib); n != before {
		t.Fatalf("listed %d times, not %d", n, before)
	}
}

// Through the mount: a lookup that misses in an expired listing waits for a current
// one, so the kernel keeps no negative entry for a file added since, and an unlink
// never deletes a file renamed since under its old name.
func TestAStaleListingThroughTheMount(t *testing.T) {
	lib := newFakeLibrary()
	lib.file(10, "a.3mf", 0, []byte("a"))
	mnt, root := mountRoot(t, lib, Options{TTL: time.Hour})
	if got := names(t, mnt); !slices.Equal(got, []string{"a.3mf"}) {
		t.Fatalf("listed: %v", got)
	}
	age(root.tree, 0)
	lib.file(11, "b.3mf", 0, []byte("b"))
	if _, err := os.Stat(filepath.Join(mnt, "b.3mf")); err != nil {
		t.Fatalf("a file added since: %v", err)
	}
	age(root.tree, 0)
	renameIn(lib, 10, "c.3mf")
	if err := os.Remove(filepath.Join(mnt, "a.3mf")); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("unlink of a name renamed since: %v", err)
	}
	if !hasFile(lib, 10) {
		t.Fatal("unlink deleted the renamed file")
	}
}
