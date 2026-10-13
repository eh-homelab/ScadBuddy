package fsys

import (
	"errors"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/eh-homelab/ScadBuddy/tools/go/bambuddy-fs/internal/api"
)

func TestWriteUploadsOnceOnClose(t *testing.T) {
	lib := newFakeLibrary()
	dir := mount(t, lib, Options{})
	f, err := os.Create(filepath.Join(dir, "new.3mf"))
	if err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 3; i++ {
		_, _ = f.Write([]byte("abc"))
		_ = f.Sync()
	}
	if err := f.Close(); err != nil {
		t.Fatal(err)
	}
	lib.locked(func() {
		if lib.uploads != 1 {
			t.Fatalf("uploads %d", lib.uploads)
		}
	})
	data, _ := os.ReadFile(filepath.Join(dir, "new.3mf"))
	if string(data) != "abcabcabc" {
		t.Fatalf("%q", data)
	}
}

func TestSeveralFlushesUploadOnce(t *testing.T) {
	lib := newFakeLibrary()
	dir := mount(t, lib, Options{})
	f, err := os.Create(filepath.Join(dir, "new.3mf"))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := f.Write([]byte("abc")); err != nil {
		t.Fatal(err)
	}
	// Closing a duplicate descriptor flushes the open file without releasing it.
	for i := 0; i < 2; i++ {
		fd, err := syscall.Dup(int(f.Fd()))
		if err != nil {
			t.Fatal(err)
		}
		if err := syscall.Close(fd); err != nil {
			t.Fatal(err)
		}
	}
	if err := f.Close(); err != nil {
		t.Fatal(err)
	}
	// Release runs after close returns, so give it time to (wrongly) upload again.
	time.Sleep(100 * time.Millisecond)
	lib.locked(func() {
		if lib.uploads != 1 {
			t.Fatalf("uploads %d", lib.uploads)
		}
	})
}

func TestCreatingAnEmptyFileUploadsIt(t *testing.T) {
	lib := newFakeLibrary()
	dir := mount(t, lib, Options{})
	if err := os.WriteFile(filepath.Join(dir, "empty.3mf"), nil, 0o644); err != nil {
		t.Fatal(err)
	}
	info, err := os.Stat(filepath.Join(dir, "empty.3mf"))
	if err != nil || info.Size() != 0 {
		t.Fatalf("%v %v", info, err)
	}
}

func TestOverwritingANameReplacesTheFile(t *testing.T) {
	lib := newFakeLibrary()
	lib.file(10, "a.3mf", 0, []byte("old"))
	dir := mount(t, lib, Options{})
	if err := os.WriteFile(filepath.Join(dir, "a.3mf"), []byte("new"), 0o644); err != nil {
		t.Fatal(err)
	}
	lib.locked(func() {
		if !lib.deleted[10] {
			t.Fatal("old file not deleted after the upload")
		}
		if lib.uploads != 1 {
			t.Fatalf("uploads %d", lib.uploads)
		}
	})
	data, _ := os.ReadFile(filepath.Join(dir, "a.3mf"))
	if string(data) != "new" {
		t.Fatalf("%q", data)
	}
	if _, downloads := lib.counts(); downloads != 0 {
		t.Fatalf("a truncating open downloaded the old contents %d times", downloads)
	}
}

func TestWritingIntoAFileKeepsTheRestOfIt(t *testing.T) {
	lib := newFakeLibrary()
	lib.file(10, "a.3mf", 4, []byte("hello"))
	lib.folder(4, "Top", 0)
	dir := mount(t, lib, Options{})
	f, err := os.OpenFile(filepath.Join(dir, "Top", "a.3mf"), os.O_RDWR, 0)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := f.WriteAt([]byte("J"), 0); err != nil {
		t.Fatal(err)
	}
	if err := f.Close(); err != nil {
		t.Fatal(err)
	}
	lib.locked(func() {
		if !lib.deleted[10] || len(lib.files) != 1 {
			t.Fatalf("deleted %v files %v", lib.deleted, lib.files)
		}
		for id, f := range lib.files {
			if f.Name != "a.3mf" || f.FolderID != 4 || string(lib.data[id]) != "Jello" {
				t.Fatalf("%+v %q", f, lib.data[id])
			}
		}
	})
}

func TestAReplaceWhoseOldFileIsKeptFailsTheClose(t *testing.T) {
	lib := newFakeLibrary()
	lib.file(10, "a.3mf", 0, []byte("old"))
	lib.busy[10] = true
	dir := mount(t, lib, Options{})
	log := logs(t)
	err := os.WriteFile(filepath.Join(dir, "a.3mf"), []byte("new"), 0o644)
	if !errors.Is(err, syscall.EBUSY) {
		t.Fatalf("%v", err)
	}
	lib.locked(func() {
		if lib.uploads != 1 || len(lib.files) != 2 {
			t.Fatalf("uploads %d files %v", lib.uploads, lib.files)
		}
	})
	// The old file keeps its name; the new one is listed beside it.
	if got := names(t, dir); !slices.Equal(got, []string{"a (id 101).3mf", "a.3mf"}) {
		t.Fatalf("%v", got)
	}
	if got := log.String(); !strings.Contains(got, "queued for printing") {
		t.Fatalf("log: %q", got)
	}
}

func TestRenameOntoAnExistingNameIsEEXIST(t *testing.T) {
	lib := newFakeLibrary()
	lib.file(10, "a.3mf", 0, nil)
	lib.file(11, "b.3mf", 0, nil)
	dir := mount(t, lib, Options{})
	err := os.Rename(filepath.Join(dir, "a.3mf"), filepath.Join(dir, "b.3mf"))
	if !errors.Is(err, syscall.EEXIST) {
		t.Fatalf("%v", err)
	}
}

func TestMoveAndMkdir(t *testing.T) {
	lib := newFakeLibrary()
	lib.file(10, "a.3mf", 0, nil)
	dir := mount(t, lib, Options{})
	if err := os.Mkdir(filepath.Join(dir, "Parts"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Rename(filepath.Join(dir, "a.3mf"), filepath.Join(dir, "Parts", "c.3mf")); err != nil {
		t.Fatal(err)
	}
	lib.locked(func() {
		if lib.files[10].Name != "c.3mf" || lib.files[10].FolderID == 0 {
			t.Fatalf("%+v", lib.files[10])
		}
	})
	if _, err := os.Stat(filepath.Join(dir, "Parts", "c.3mf")); err != nil {
		t.Fatal(err)
	}
}

func TestRenameInPlaceIsOneUpdateOfTheName(t *testing.T) {
	lib := newFakeLibrary()
	lib.file(10, "a.3mf", 0, nil)
	dir := mount(t, lib, Options{})
	if err := os.Rename(filepath.Join(dir, "a.3mf"), filepath.Join(dir, "A.3mf")); err != nil {
		t.Fatal(err)
	}
	lib.locked(func() {
		if len(lib.updates) != 1 {
			t.Fatalf("%+v", lib.updates)
		}
		u := lib.updates[0]
		if u.folder || u.id != 10 || u.name == nil || *u.name != "A.3mf" || u.parent != nil {
			t.Fatalf("%+v", u)
		}
	})
}

func TestMovingToTheTopLevelSendsZero(t *testing.T) {
	lib := newFakeLibrary()
	lib.folder(4, "Top", 0)
	lib.file(10, "a.3mf", 4, nil)
	dir := mount(t, lib, Options{})
	if err := os.Rename(filepath.Join(dir, "Top", "a.3mf"), filepath.Join(dir, "a.3mf")); err != nil {
		t.Fatal(err)
	}
	lib.locked(func() {
		u := lib.updates[0]
		if u.name != nil || u.parent == nil || *u.parent != 0 || lib.files[10].FolderID != 0 {
			t.Fatalf("%+v %+v", u, lib.files[10])
		}
	})
}

func TestMovingAFolderSetsItsParent(t *testing.T) {
	lib := newFakeLibrary()
	lib.folder(4, "Top", 0)
	lib.folder(5, "Inner", 0)
	lib.file(10, "deep.3mf", 5, []byte("d"))
	dir := mount(t, lib, Options{})
	if err := os.Rename(filepath.Join(dir, "Inner"), filepath.Join(dir, "Top", "Inner")); err != nil {
		t.Fatal(err)
	}
	lib.locked(func() {
		u := lib.updates[0]
		if len(lib.updates) != 1 || !u.folder || u.id != 5 || u.name != nil || u.parent == nil || *u.parent != 4 {
			t.Fatalf("%+v", lib.updates)
		}
	})
	if data, err := os.ReadFile(filepath.Join(dir, "Top", "Inner", "deep.3mf")); err != nil || string(data) != "d" {
		t.Fatalf("%q %v", data, err)
	}
}

func TestUnlink(t *testing.T) {
	lib := newFakeLibrary()
	lib.file(10, "a.3mf", 0, []byte("a"))
	dir := mount(t, lib, Options{})
	if err := os.Remove(filepath.Join(dir, "a.3mf")); err != nil {
		t.Fatal(err)
	}
	lib.locked(func() {
		if !lib.deleted[10] {
			t.Fatal("not deleted")
		}
	})
	if _, err := os.Stat(filepath.Join(dir, "a.3mf")); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("%v", err)
	}
}

func TestUnlinkOfABusyFileIsEBUSY(t *testing.T) {
	lib := newFakeLibrary()
	lib.file(10, "a.3mf", 0, []byte("a"))
	lib.busy[10] = true
	dir := mount(t, lib, Options{})
	logs(t)
	if err := os.Remove(filepath.Join(dir, "a.3mf")); !errors.Is(err, syscall.EBUSY) {
		t.Fatalf("%v", err)
	}
}

func TestUnlinkAndRmdirOfAFullFolder(t *testing.T) {
	lib := newFakeLibrary()
	lib.folder(4, "Top", 0)
	lib.file(10, "a.3mf", 4, nil)
	dir := mount(t, lib, Options{})
	if err := os.Remove(filepath.Join(dir, "Top")); err != nil {
		t.Fatalf("rmdir of a full folder: %v", err)
	}
	lib.locked(func() {
		if !lib.folderDeleted[4] {
			t.Fatal("folder delete not called")
		}
	})
	if _, err := os.Stat(filepath.Join(dir, "Top")); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("still listed")
	}
}

func TestRmdirRefusedByAQueuedPrintIsEBUSYAndKeepsEverything(t *testing.T) {
	lib := newFakeLibrary()
	lib.folder(4, "Top", 0)
	lib.file(10, "a.3mf", 4, nil)
	lib.busy[10] = true
	dir := mount(t, lib, Options{})
	logs(t)
	err := os.Remove(filepath.Join(dir, "Top"))
	if !errors.Is(err, syscall.EBUSY) {
		t.Fatalf("%v", err)
	}
	if _, err := os.Stat(filepath.Join(dir, "Top", "a.3mf")); err != nil {
		t.Fatal("file gone after a refused rmdir")
	}
}

func TestReadOnlyRefusesWrites(t *testing.T) {
	lib := newFakeLibrary()
	lib.folder(4, "Top", 0)
	lib.file(10, "a.3mf", 0, nil)
	dir := mount(t, lib, Options{ReadOnly: true})
	if err := os.WriteFile(filepath.Join(dir, "x.3mf"), nil, 0o644); !errors.Is(err, syscall.EROFS) {
		t.Fatalf("%v", err)
	}
	if err := os.Remove(filepath.Join(dir, "a.3mf")); !errors.Is(err, syscall.EROFS) {
		t.Fatalf("%v", err)
	}
	if err := os.WriteFile(filepath.Join(dir, "a.3mf"), nil, 0o644); !errors.Is(err, syscall.EROFS) {
		t.Fatalf("overwrite: %v", err)
	}
	if err := os.Mkdir(filepath.Join(dir, "New"), 0o755); !errors.Is(err, syscall.EROFS) {
		t.Fatalf("mkdir: %v", err)
	}
	if err := os.Rename(filepath.Join(dir, "a.3mf"), filepath.Join(dir, "b.3mf")); !errors.Is(err, syscall.EROFS) {
		t.Fatalf("rename: %v", err)
	}
	if err := os.Remove(filepath.Join(dir, "Top")); !errors.Is(err, syscall.EROFS) {
		t.Fatalf("rmdir: %v", err)
	}
	for _, name := range []string{"Top", "a.3mf"} {
		info, err := os.Stat(filepath.Join(dir, name))
		if err != nil || info.Mode().Perm()&0o222 != 0 {
			t.Fatalf("%s: %v %v", name, info.Mode(), err)
		}
	}
}

func TestWritableModes(t *testing.T) {
	lib := newFakeLibrary()
	lib.folder(4, "Top", 0)
	lib.file(10, "a.3mf", 0, nil)
	dir := mount(t, lib, Options{})
	for name, want := range map[string]os.FileMode{"Top": 0o755, "a.3mf": 0o644} {
		info, err := os.Stat(filepath.Join(dir, name))
		if err != nil || info.Mode().Perm() != want {
			t.Fatalf("%s: %v %v", name, info.Mode(), err)
		}
	}
}

func TestChmodIsENOTSUP(t *testing.T) {
	lib := newFakeLibrary()
	lib.file(10, "a.3mf", 0, nil)
	dir := mount(t, lib, Options{})
	if err := os.Chmod(filepath.Join(dir, "a.3mf"), 0o600); !errors.Is(err, syscall.ENOTSUP) {
		t.Fatalf("%v", err)
	}
}

func TestTruncatingAStoredFileIsENOTSUP(t *testing.T) {
	lib := newFakeLibrary()
	lib.file(10, "a.3mf", 0, []byte("abc"))
	dir := mount(t, lib, Options{})
	if err := os.Truncate(filepath.Join(dir, "a.3mf"), 0); !errors.Is(err, syscall.ENOTSUP) {
		t.Fatalf("%v", err)
	}
}

func TestAGeneratedNameNeverHidesARealOne(t *testing.T) {
	lib := newFakeLibrary()
	lib.file(10, "a.3mf", 0, []byte("ten"))
	lib.file(12, "a.3mf", 0, []byte("twelve"))
	lib.file(11, "a (id 12).3mf", 0, []byte("eleven"))
	dir := mount(t, lib, Options{})
	want := map[string]string{"a.3mf": "ten", "a (id 12).3mf": "eleven", "a (id 12) (2).3mf": "twelve"}
	if got := names(t, dir); len(got) != len(want) {
		t.Fatalf("%v", got)
	}
	for name, content := range want {
		if data, err := os.ReadFile(filepath.Join(dir, name)); err != nil || string(data) != content {
			t.Fatalf("%s: %q %v", name, data, err)
		}
	}
}

func TestAFileNamedLikeAFolderIsReachable(t *testing.T) {
	lib := newFakeLibrary()
	lib.folder(4, "Parts", 0)
	lib.file(10, "Parts", 0, []byte("p"))
	dir := mount(t, lib, Options{All: true})
	if got := names(t, dir); !slices.Equal(got, []string{"Parts", "Parts (id 10)"}) {
		t.Fatalf("%v", got)
	}
	if info, err := os.Stat(filepath.Join(dir, "Parts")); err != nil || !info.IsDir() {
		t.Fatalf("%v %v", info, err)
	}
	if data, err := os.ReadFile(filepath.Join(dir, "Parts (id 10)")); err != nil || string(data) != "p" {
		t.Fatalf("%q %v", data, err)
	}
}

func TestRenamingAShownNameSendsTheRealOne(t *testing.T) {
	lib := newFakeLibrary()
	lib.folder(4, "Top", 0)
	lib.file(10, "a.3mf", 0, nil)
	lib.file(11, "a.3mf", 0, nil)
	dir := mount(t, lib, Options{})
	// Moving "a (id 11).3mf" keeps its real name, which is free in Top.
	if err := os.Rename(filepath.Join(dir, "a (id 11).3mf"), filepath.Join(dir, "Top", "a.3mf")); err != nil {
		t.Fatal(err)
	}
	lib.locked(func() {
		u := lib.updates[0]
		if u.name != nil || *u.parent != 4 || lib.files[11] != (api.File{ID: 11, Name: "a.3mf", FolderID: 4, Created: lib.files[10].Created}) {
			t.Fatalf("%+v %+v", u, lib.files[11])
		}
	})
}

func TestAReplacedFileIsSeenAtOnceWhileEntriesAreCached(t *testing.T) {
	lib := newFakeLibrary()
	lib.file(10, "a.3mf", 0, []byte("old"))
	dir := mount(t, lib, Options{TTL: time.Minute})
	path := filepath.Join(dir, "a.3mf")
	if _, err := os.Stat(path); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte("newer"), 0o644); err != nil {
		t.Fatal(err)
	}
	info, err := os.Stat(path)
	if err != nil || info.Sys().(*syscall.Stat_t).Ino != 101<<1|1 || info.Size() != 5 {
		t.Fatalf("%v %v", info, err)
	}
}

func TestAFileBeingWrittenCanBeFoundRenamedAndRemoved(t *testing.T) {
	lib := newFakeLibrary()
	dir := mount(t, lib, Options{})
	f, err := os.Create(filepath.Join(dir, "draft.3mf"))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := f.Write([]byte("abc")); err != nil {
		t.Fatal(err)
	}
	if info, err := os.Stat(filepath.Join(dir, "draft.3mf")); err != nil || info.Size() != 3 {
		t.Fatalf("stat while writing: %v %v", info, err)
	}
	if got := names(t, dir); !slices.Equal(got, []string{"draft.3mf"}) {
		t.Fatalf("%v", got)
	}
	if err := os.Rename(filepath.Join(dir, "draft.3mf"), filepath.Join(dir, "final.3mf")); err != nil {
		t.Fatal(err)
	}
	if err := f.Close(); err != nil {
		t.Fatal(err)
	}
	lib.locked(func() {
		if len(lib.files) != 1 || lib.files[101].Name != "final.3mf" || len(lib.updates) != 0 {
			t.Fatalf("%v %+v", lib.files, lib.updates)
		}
	})

	g, err := os.Create(filepath.Join(dir, "scratch.3mf"))
	if err != nil {
		t.Fatal(err)
	}
	if err := os.Remove(filepath.Join(dir, "scratch.3mf")); err != nil {
		t.Fatal(err)
	}
	if err := g.Close(); err != nil {
		t.Fatal(err)
	}
	lib.locked(func() {
		if lib.uploads != 1 {
			t.Fatalf("uploads %d", lib.uploads)
		}
	})
}
