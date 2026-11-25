package packages

import (
	"context"
	"net/http/httptest"
	"testing"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/testdb"
)

// A request that ends after its write commits still announces the write: Python's outbox
// is not tied to the request.
func TestAWriteIsAnnouncedAfterItsRequestEnds(t *testing.T) {
	pool := testdb.New(t)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	r := httptest.NewRequest("POST", "/", nil).WithContext(ctx)
	(&Packages{Pool: pool}).announced(r, "greeter")
	var n int
	if err := pool.QueryRow(context.Background(), `SELECT count(*) FROM events
		WHERE kind = 'plugin_packages.changed' AND payload->>'name' = 'greeter'`).Scan(&n); err != nil {
		t.Fatal(err)
	}
	if n != 1 {
		t.Fatalf("got %d events, want 1", n)
	}
}
