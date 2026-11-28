//go:build linux

package watch

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

func wait(t *testing.T, w *Watcher, what string) {
	t.Helper()
	select {
	case <-w.Changes():
	case <-time.After(5 * time.Second):
		t.Fatalf("no change after %s", what)
	}
}

func TestWatch(t *testing.T) {
	root := t.TempDir()
	dir := filepath.Join(root, "sessions")
	w, err := New([]string{dir})
	if err != nil {
		t.Fatal(err)
	}
	defer w.Close()
	if w.Watched() != 0 {
		t.Fatal("a missing dir is watched")
	}
	if err := os.Mkdir(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	if !w.Ensure() || w.Watched() != 1 {
		t.Fatal("Ensure did not add the new dir")
	}

	if err := os.WriteFile(filepath.Join(dir, "1.json"), []byte("{}"), 0o600); err != nil {
		t.Fatal(err)
	}
	wait(t, w, "a write")
	// Drain the burst, then a rename in.
	time.Sleep(50 * time.Millisecond)
	select {
	case <-w.Changes():
	default:
	}
	tmp := filepath.Join(root, "tmp.json")
	_ = os.WriteFile(tmp, []byte("{}"), 0o600)
	if err := os.Rename(tmp, filepath.Join(dir, "2.json")); err != nil {
		t.Fatal(err)
	}
	wait(t, w, "a rename")
	if err := os.Remove(filepath.Join(dir, "1.json")); err != nil {
		t.Fatal(err)
	}
	wait(t, w, "a remove")

	// The dir goes and comes back: it is watched again.
	if err := os.RemoveAll(dir); err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Add(5 * time.Second)
	for w.Watched() != 0 && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	if w.Watched() != 0 {
		t.Fatal("a removed dir is still counted")
	}
	_ = os.Mkdir(dir, 0o755)
	if !w.Ensure() {
		t.Fatal("not watched again")
	}
}

func TestADirRenamedAwayIsNoLongerWatched(t *testing.T) {
	root := t.TempDir()
	dir := filepath.Join(root, "sessions")
	if err := os.Mkdir(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	w, err := New([]string{dir})
	if err != nil {
		t.Fatal(err)
	}
	defer w.Close()
	if w.Watched() != 1 {
		t.Fatal("the dir is not watched")
	}
	away := filepath.Join(root, "away")
	if err := os.Rename(dir, away); err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Add(5 * time.Second)
	for w.Watched() != 0 && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	if w.Watched() != 0 {
		t.Fatal("a dir renamed away is still counted")
	}
	// Drain the move's signals, then write in the moved dir: no change is signalled.
	time.Sleep(50 * time.Millisecond)
	select {
	case <-w.Changes():
	default:
	}
	if err := os.WriteFile(filepath.Join(away, "1.json"), []byte("{}"), 0o600); err != nil {
		t.Fatal(err)
	}
	select {
	case <-w.Changes():
		t.Fatal("a write in the dir renamed away was signalled")
	case <-time.After(300 * time.Millisecond):
	}
	// A new dir at the path is watched again.
	if err := os.Mkdir(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	if !w.Ensure() || w.Watched() != 1 {
		t.Fatal("the new dir is not watched")
	}
	if err := os.WriteFile(filepath.Join(dir, "2.json"), []byte("{}"), 0o600); err != nil {
		t.Fatal(err)
	}
	wait(t, w, "a write in the new dir")
}

func TestCloseEndsRead(t *testing.T) {
	w, err := New([]string{t.TempDir()})
	if err != nil {
		t.Fatal(err)
	}
	if err := w.Close(); err != nil {
		t.Fatal(err)
	}
}
