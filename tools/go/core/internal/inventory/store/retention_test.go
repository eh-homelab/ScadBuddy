package store

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	"time"
)

// TestRequestRetentionIsPythons reads testdata/retention.json, which backend
// tests/crud/test_idempotency.py test_the_inventory_retention_is_gos reads too. The prune
// keeps an operation a day past requestRetention, so a longer Python retention would let
// it delete a row whose ledger answer still replays (#8448).
func TestRequestRetentionIsPythons(t *testing.T) {
	raw, err := os.ReadFile(filepath.Join("testdata", "retention.json"))
	if err != nil {
		t.Fatal(err)
	}
	var file struct {
		Seconds int64 `json:"inventory_request_retention_seconds"`
	}
	if err := json.Unmarshal(raw, &file); err != nil || file.Seconds == 0 {
		t.Fatal(file.Seconds, err)
	}
	if got := time.Duration(file.Seconds) * time.Second; got != requestRetention {
		t.Fatalf("requestRetention is %s, the vector %s", requestRetention, got)
	}
}
