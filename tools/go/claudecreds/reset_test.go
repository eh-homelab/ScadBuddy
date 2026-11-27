package claudecreds

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	"time"
)

// The shared vectors: credentialErrors.ts rateLimitResetFromText and agent-durable's
// rate_limit_reset are held to the same file (#4121 review, #7986).
const vectorsPath = "../../../agent/test/fixtures/rate-limit-reset-vectors.json"

type resetCase struct {
	Text string  `json:"text"`
	Now  string  `json:"now"`
	At   *string `json:"at"`
}

func TestResetFromTextVectors(t *testing.T) {
	raw, err := os.ReadFile(filepath.FromSlash(vectorsPath))
	if err != nil {
		t.Fatal(err)
	}
	var file struct {
		Cases []resetCase `json:"cases"`
	}
	if err := json.Unmarshal(raw, &file); err != nil {
		t.Fatal(err)
	}
	if len(file.Cases) == 0 {
		t.Fatal("no cases in the vectors")
	}
	for _, c := range file.Cases {
		t.Run(c.Text+"@"+c.Now, func(t *testing.T) {
			now, err := time.Parse(time.RFC3339Nano, c.Now)
			if err != nil {
				t.Fatal(err)
			}
			got, ok := ResetFromText(c.Text, now)
			if c.At == nil {
				if ok {
					t.Fatalf("got %s, want none", got.UTC().Format(time.RFC3339))
				}
				return
			}
			want, err := time.Parse(time.RFC3339Nano, *c.At)
			if err != nil {
				t.Fatal(err)
			}
			if !ok {
				t.Fatalf("got none, want %s", want.Format(time.RFC3339))
			}
			if !got.Equal(want) {
				t.Fatalf("got %s, want %s", got.UTC().Format(time.RFC3339), want.Format(time.RFC3339))
			}
			if got.Location() != time.UTC {
				t.Fatalf("got location %s, want UTC", got.Location())
			}
		})
	}
}

// A zone directory that holds a name in another case refuses it, as a case-insensitive
// filesystem (macOS) would otherwise open it (#8023). The directory here is a stand-in
// for /usr/share/zoneinfo, so the test gives the same answer on Linux and macOS.
func TestAZoneNameInAnotherCaseIsRefused(t *testing.T) {
	dir := t.TempDir()
	if err := os.MkdirAll(filepath.Join(dir, "America"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "America", "New_York"), nil, 0o644); err != nil {
		t.Fatal(err)
	}
	saved := zoneDirs
	zoneDirs = nil
	t.Cleanup(func() { zoneDirs = saved })
	// $ZONEINFO is read first, so the stand-in directory is checked there too.
	t.Setenv("ZONEINFO", dir)

	for name, want := range map[string]bool{
		"America/New_York": true,
		"america/new_york": false,
		"America/new_york": false,
		"Asia/Kolkata":     true,
	} {
		if got := writtenAsFiled(name); got != want {
			t.Errorf("writtenAsFiled(%q) = %v, want %v", name, got, want)
		}
	}
	if _, ok := ResetFromText("resets 1pm (america/new_york)", time.Date(2026, 10, 17, 22, 16, 5, 0, time.UTC)); ok {
		t.Error("a lower-case zone name was read")
	}
}
