package claudecreds

import (
	"os"
	"path/filepath"
	"testing"
)

func appendFile(t *testing.T, path string, data ...[]byte) {
	t.Helper()
	f, err := os.OpenFile(path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o600)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	for _, d := range data {
		if _, err := f.Write(d); err != nil {
			t.Fatal(err)
		}
	}
}

func line(b []byte) []byte { return append(append([]byte(nil), b...), '\n') }

func poll(t *testing.T, tl *Tail) []string {
	t.Helper()
	events, err := tl.Poll()
	if err != nil {
		t.Fatal(err)
	}
	var cats []string
	for _, e := range events {
		cats = append(cats, e.Category)
	}
	return cats
}

func wantCats(t *testing.T, got []string, want ...string) {
	t.Helper()
	if len(got) != len(want) {
		t.Fatalf("events %v, want %v", got, want)
	}
	for i := range got {
		if got[i] != want[i] {
			t.Fatalf("events %v, want %v", got, want)
		}
	}
}

func newTail(t *testing.T, path string) *Tail {
	tl := NewTail(path)
	t.Cleanup(func() { tl.Close() })
	return tl
}

func TestTailStartsAtEndAndAppends(t *testing.T) {
	path := filepath.Join(t.TempDir(), "s.jsonl")
	billing, rate, user := fixture(t, "billing_error.jsonl"), fixture(t, "rate_limit_usage_limit.jsonl"), fixture(t, "user.jsonl")
	appendFile(t, path, line(billing))
	tl := newTail(t, path)
	wantCats(t, poll(t, tl)) // the old line is skipped
	appendFile(t, path, line(user), line(rate), line(billing))
	wantCats(t, poll(t, tl), "rate_limit", "billing_error")
	wantCats(t, poll(t, tl))
}

func TestTailMissingFileIsReadFromStart(t *testing.T) {
	path := filepath.Join(t.TempDir(), "s.jsonl")
	tl := newTail(t, path)
	wantCats(t, poll(t, tl))
	appendFile(t, path, line(fixture(t, "server_error.jsonl")))
	wantCats(t, poll(t, tl), "server_error")
}

func TestTailPartialLine(t *testing.T) {
	path := filepath.Join(t.TempDir(), "s.jsonl")
	appendFile(t, path)
	tl := newTail(t, path)
	wantCats(t, poll(t, tl))
	rate := fixture(t, "rate_limit_plain_429.jsonl")
	half := len(rate) / 2
	appendFile(t, path, rate[:half])
	wantCats(t, poll(t, tl))
	appendFile(t, path, rate[half:])
	wantCats(t, poll(t, tl)) // no newline yet
	appendFile(t, path, []byte("\n"), line(fixture(t, "billing_error.jsonl")))
	wantCats(t, poll(t, tl), "rate_limit", "billing_error")
}

func TestTailTruncate(t *testing.T) {
	path := filepath.Join(t.TempDir(), "s.jsonl")
	appendFile(t, path, line(fixture(t, "assistant.jsonl")), line(fixture(t, "assistant.jsonl")))
	tl := newTail(t, path)
	wantCats(t, poll(t, tl))
	if err := os.Truncate(path, 0); err != nil {
		t.Fatal(err)
	}
	appendFile(t, path, line(fixture(t, "server_error.jsonl")))
	wantCats(t, poll(t, tl), "server_error")
}

func TestTailRename(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "s.jsonl")
	appendFile(t, path, line(fixture(t, "user.jsonl")))
	tl := newTail(t, path)
	wantCats(t, poll(t, tl))
	// A line lands in the old file, which is then renamed, and a new file takes the path.
	appendFile(t, path, line(fixture(t, "billing_error.jsonl")))
	if err := os.Rename(path, filepath.Join(dir, "old.jsonl")); err != nil {
		t.Fatal(err)
	}
	wantCats(t, poll(t, tl), "billing_error")
	appendFile(t, path, line(fixture(t, "user.jsonl")), line(fixture(t, "rate_limit_usage_limit.jsonl")))
	wantCats(t, poll(t, tl), "rate_limit")
	// A new file renamed over the path, while the open one is still there.
	other := filepath.Join(dir, "next.jsonl")
	appendFile(t, other, line(fixture(t, "server_error.jsonl")))
	appendFile(t, path, line(fixture(t, "billing_error.jsonl")))
	if err := os.Rename(other, path); err != nil {
		t.Fatal(err)
	}
	wantCats(t, poll(t, tl), "billing_error", "server_error")
}
