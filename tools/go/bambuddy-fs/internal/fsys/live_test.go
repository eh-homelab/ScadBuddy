package fsys

import (
	"bytes"
	"crypto/rand"
	"net/http"
	"os"
	"path/filepath"
	"slices"
	"testing"
	"time"

	"github.com/eh-homelab/ScadBuddy/tools/go/bambuddy-fs/internal/api"
)

// TestLiveMount mounts a running ScadBuddy's print library (BAMBUDDY_FS_LIVE_API, its
// API root) and walks one file through it. CI runs it against both library backends
// (.github/scripts/bambuddy-fs-live.sh); without the variable it skips.
func TestLiveMount(t *testing.T) {
	base := os.Getenv("BAMBUDDY_FS_LIVE_API")
	if base == "" {
		t.Skip("BAMBUDDY_FS_LIVE_API is not set")
	}
	client, err := api.New(base, http.DefaultClient)
	if err != nil {
		t.Fatal(err)
	}
	dir := mount(t, client, Options{All: true, TTL: time.Second})
	dinos := filepath.Join(dir, "Dinos")
	if err := os.Mkdir(dinos, 0o755); err != nil {
		t.Fatal(err)
	}
	first := randomBytes(t)
	writeFile(t, filepath.Join(dinos, "rex.3mf"), first)
	if got := names(t, dinos); !slices.Equal(got, []string{"rex.3mf"}) {
		t.Fatalf("ls Dinos: %v", got)
	}
	readSame(t, filepath.Join(dinos, "rex.3mf"), first)
	if err := os.Rename(filepath.Join(dinos, "rex.3mf"), filepath.Join(dinos, "trex.3mf")); err != nil {
		t.Fatal(err)
	}
	top := filepath.Join(dir, "trex.3mf")
	if err := os.Rename(filepath.Join(dinos, "trex.3mf"), top); err != nil {
		t.Fatal(err)
	}
	second := randomBytes(t)
	writeFile(t, top, second)
	readSame(t, top, second)
	if err := os.Remove(top); err != nil {
		t.Fatal(err)
	}
	if err := os.Remove(dinos); err != nil {
		t.Fatal(err)
	}
	for _, name := range names(t, dir) {
		if name == "Dinos" || name == "trex.3mf" {
			t.Fatalf("top level still holds %s", name)
		}
	}
}

func randomBytes(t *testing.T) []byte {
	t.Helper()
	b := make([]byte, 1<<20)
	if _, err := rand.Read(b); err != nil {
		t.Fatal(err)
	}
	return b
}

// writeFile writes data over path (creating it, or truncating the stored file) and
// closes it, which sends it.
func writeFile(t *testing.T, path string, data []byte) {
	t.Helper()
	f, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, 0o644)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	if _, err := f.Write(data); err != nil {
		t.Fatal(err)
	}
	if err := f.Close(); err != nil {
		t.Fatal(err)
	}
}

func readSame(t *testing.T, path string, want []byte) {
	t.Helper()
	got, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(got, want) {
		t.Fatalf("%s: read %d bytes that differ from the %d written", path, len(got), len(want))
	}
}
