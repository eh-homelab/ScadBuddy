package fsys

import (
	"bytes"
	"errors"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"

	"github.com/hanwen/go-fuse/v2/fs"
)

// mount mounts lib on a temporary directory with the kernel's entry and attribute
// timeouts at opts.TTL, as the CLI does, and unmounts it when the test ends. It
// skips without /dev/fuse; with it, a failed mount fails the test.
// A test closes (defer) every file it opens on the mount: one left open when a test
// fails would be flushed at exit into this process's own stopped server, and hang.
func mount(t *testing.T, lib Library, opts Options) string {
	t.Helper()
	dir, _ := mountRoot(t, lib, opts)
	return dir
}

// mountRoot mounts as mount does and also returns the root.
func mountRoot(t *testing.T, lib Library, opts Options) (string, *dir) {
	t.Helper()
	if _, err := os.Stat("/dev/fuse"); err != nil {
		t.Skip("no /dev/fuse")
	}
	mnt := t.TempDir()
	if opts.CacheDir == "" {
		opts.CacheDir = t.TempDir()
	}
	if opts.StateDir == "" {
		opts.StateDir = t.TempDir()
	}
	ttl := opts.TTL
	root := NewRoot(lib, opts).(*dir)
	server, err := fs.Mount(mnt, root, &fs.Options{EntryTimeout: &ttl, AttrTimeout: &ttl})
	if err != nil {
		t.Fatalf("mount: %v", err)
	}
	// Registered after t.TempDir, so it runs before the directory is removed.
	t.Cleanup(func() {
		if err := server.Unmount(); err != nil {
			t.Errorf("unmount: %v", err)
		}
	})
	return mnt, root
}

// logs sends slog's output to a buffer until the test ends.
func logs(t *testing.T) *lockedBuffer {
	t.Helper()
	buf := &lockedBuffer{}
	prev := slog.Default()
	slog.SetDefault(slog.New(slog.NewTextHandler(buf, nil)))
	t.Cleanup(func() { slog.SetDefault(prev) })
	return buf
}

// lockedBuffer is written by the FUSE server's goroutines and read by the test.
type lockedBuffer struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (b *lockedBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.Write(p)
}

func (b *lockedBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.String()
}

func names(t *testing.T, dir string) []string {
	t.Helper()
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	var out []string
	for _, e := range entries {
		out = append(out, e.Name())
	}
	return out
}

func TestReadOnlyTree(t *testing.T) {
	lib := newFakeLibrary()
	lib.folder(4, "Top", 0)
	lib.file(10, "a.3mf", 4, []byte("hello"))
	lib.file(11, "a.3mf", 4, []byte("world"))
	dir := mount(t, lib, Options{TTL: 0})
	entries, err := os.ReadDir(filepath.Join(dir, "Top"))
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 2 {
		t.Fatalf("%v", entries)
	}
	data, err := os.ReadFile(filepath.Join(dir, "Top", "a.3mf"))
	if err != nil || string(data) != "hello" {
		t.Fatalf("%q %v", data, err)
	}
	info, err := os.Stat(filepath.Join(dir, "Top", "a (id 11).3mf"))
	if err != nil {
		t.Fatal(err)
	}
	if info.Size() != 5 {
		t.Fatalf("size %d", info.Size())
	}
}

func TestAFileGoneFromTheLibraryIsENOENT(t *testing.T) {
	lib := newFakeLibrary()
	lib.file(10, "a.3mf", 0, []byte("x"))
	dir := mount(t, lib, Options{TTL: 0})
	lib.remove(10)
	if _, err := os.Stat(filepath.Join(dir, "a.3mf")); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("%v", err)
	}
}

func TestSubfoldersComeFromParentID(t *testing.T) {
	lib := newFakeLibrary()
	lib.folder(4, "Top", 0)
	lib.folder(5, "Inner", 4)
	lib.folder(6, "Other", 0)
	lib.file(10, "deep.3mf", 5, []byte("d"))
	lib.file(12, "root.3mf", 0, []byte("r"))
	dir := mount(t, lib, Options{TTL: 0})
	if got := names(t, dir); !slices.Equal(got, []string{"Other", "Top", "root.3mf"}) {
		t.Fatalf("top level: %v", got)
	}
	if got := names(t, filepath.Join(dir, "Top")); !slices.Equal(got, []string{"Inner"}) {
		t.Fatalf("Top: %v", got)
	}
	data, err := os.ReadFile(filepath.Join(dir, "Top", "Inner", "deep.3mf"))
	if err != nil || string(data) != "d" {
		t.Fatalf("%q %v", data, err)
	}
}

func TestAttributes(t *testing.T) {
	lib := newFakeLibrary()
	lib.folder(4, "Top", 0)
	lib.file(10, "a.3mf", 4, []byte("hello"))
	dir := mount(t, lib, Options{TTL: 0})

	info, err := os.Stat(filepath.Join(dir, "Top"))
	if err != nil {
		t.Fatal(err)
	}
	if !info.IsDir() || info.Mode().Perm() != 0o755 || info.Sys().(*syscall.Stat_t).Ino != 4<<1 {
		t.Fatalf("dir: %v ino %d", info.Mode(), info.Sys().(*syscall.Stat_t).Ino)
	}

	info, err = os.Stat(filepath.Join(dir, "Top", "a.3mf"))
	if err != nil {
		t.Fatal(err)
	}
	st := info.Sys().(*syscall.Stat_t)
	created := time.Date(2026, 10, 1, 12, 0, 0, 0, time.UTC)
	if !info.Mode().IsRegular() || info.Mode().Perm() != 0o644 || st.Ino != 10<<1|1 {
		t.Fatalf("file: %v ino %d", info.Mode(), st.Ino)
	}
	if !info.ModTime().Equal(created) || st.Ctim.Sec != created.Unix() {
		t.Fatalf("times: mtime %v ctime %d", info.ModTime(), st.Ctim.Sec)
	}
}

func TestAllIsPassedToTheLibrary(t *testing.T) {
	lib := newFakeLibrary()
	lib.file(10, "a.3mf", 0, []byte("a"))
	lib.file(11, "notes.txt", 0, []byte("n"))
	if got := names(t, mount(t, lib, Options{})); !slices.Equal(got, []string{"a.3mf"}) {
		t.Fatalf("without all: %v", got)
	}
	if got := names(t, mount(t, lib, Options{All: true})); !slices.Equal(got, []string{"a.3mf", "notes.txt"}) {
		t.Fatalf("with all: %v", got)
	}
}

func TestListingsAreCachedForTheTTL(t *testing.T) {
	lib := newFakeLibrary()
	lib.file(10, "a.3mf", 0, []byte("a"))
	dir := mount(t, lib, Options{TTL: time.Minute})
	before, _ := lib.counts()
	names(t, dir)
	names(t, dir)
	if _, err := os.Stat(filepath.Join(dir, "a.3mf")); err != nil {
		t.Fatal(err)
	}
	if lists, _ := lib.counts(); lists-before > 1 {
		t.Fatalf("listed %d times", lists-before)
	}
}

func TestContentIsDownloadedOnceAndReadAtRandom(t *testing.T) {
	lib := newFakeLibrary()
	lib.file(10, "a.3mf", 0, []byte("0123456789"))
	cache := t.TempDir()
	dir := mount(t, lib, Options{TTL: 0, CacheDir: cache})

	f, err := os.Open(filepath.Join(dir, "a.3mf"))
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	buf := make([]byte, 3)
	if _, err := f.ReadAt(buf, 6); err != nil || string(buf) != "678" {
		t.Fatalf("%q %v", buf, err)
	}
	if _, err := f.ReadAt(buf, 1); err != nil || string(buf) != "123" {
		t.Fatalf("%q %v", buf, err)
	}
	if n, err := f.ReadAt(buf, 9); n != 1 || !errors.Is(err, io.EOF) || buf[0] != '9' {
		t.Fatalf("at the end: %d %q %v", n, buf, err)
	}
	_ = f.Close()

	if data, err := os.ReadFile(filepath.Join(dir, "a.3mf")); err != nil || string(data) != "0123456789" {
		t.Fatalf("%q %v", data, err)
	}
	if _, downloads := lib.counts(); downloads != 1 {
		t.Fatalf("downloaded %d times", downloads)
	}
	if data, err := os.ReadFile(filepath.Join(cache, "10")); err != nil || string(data) != "0123456789" {
		t.Fatalf("cache: %q %v", data, err)
	}
	if _, err := os.Stat(filepath.Join(cache, "10.part")); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("part left behind: %v", err)
	}
}

func TestACachedFileOfTheWrongSizeIsDownloadedAgain(t *testing.T) {
	lib := newFakeLibrary()
	lib.file(10, "a.3mf", 0, []byte("hello"))
	cache := t.TempDir()
	if err := os.WriteFile(filepath.Join(cache, "10"), []byte("hel"), 0o600); err != nil {
		t.Fatal(err)
	}
	dir := mount(t, lib, Options{TTL: 0, CacheDir: cache})
	if data, err := os.ReadFile(filepath.Join(dir, "a.3mf")); err != nil || string(data) != "hello" {
		t.Fatalf("%q %v", data, err)
	}
	if _, downloads := lib.counts(); downloads != 1 {
		t.Fatalf("downloaded %d times", downloads)
	}
}

func TestA404OnOpenDropsTheEntry(t *testing.T) {
	lib := newFakeLibrary()
	lib.file(10, "a.3mf", 0, []byte("x"))
	dir := mount(t, lib, Options{TTL: time.Minute})
	log := logs(t)
	path := filepath.Join(dir, "a.3mf")
	if _, err := os.Stat(path); err != nil {
		t.Fatal(err)
	}
	lib.remove(10)
	// The listing and the kernel's entry are both still fresh, so only the download
	// finds the file gone.
	if _, err := os.ReadFile(path); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("open: %v", err)
	}
	if _, err := os.Stat(path); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("stat after: %v", err)
	}
	if got := log.String(); !strings.Contains(got, "open failed") || !strings.Contains(got, "404") {
		t.Fatalf("log: %q", got)
	}
}
