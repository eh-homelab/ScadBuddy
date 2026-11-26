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
