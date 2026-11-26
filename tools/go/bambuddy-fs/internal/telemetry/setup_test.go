package telemetry

import (
	"context"
	"testing"
	"time"
)

func TestDisabledBuildsNothing(t *testing.T) {
	before := rec()
	stop, err := Start(Config{Disabled: true})
	if err != nil {
		t.Fatal(err)
	}
	if rec() != before {
		t.Fatal("a recorder was installed")
	}
	stop(time.Second)
}

// An unresolvable or refusing collector, as off the LAN, delays neither the start nor
// an operation, and the stop is bounded.
func TestAnUnreachableCollectorDelaysNothing(t *testing.T) {
	const within = 300 * time.Millisecond
	for _, endpoint := range []string{"https://bambuddy-fs-test.invalid", "http://127.0.0.1:1"} {
		started := time.Now()
		stop, err := Start(Config{Endpoint: endpoint})
		if err != nil {
			t.Fatal(err)
		}
		if d := time.Since(started); d > 500*time.Millisecond {
			t.Fatalf("%s: start took %v", endpoint, d)
		}
		started = time.Now()
		for range 10000 {
			_, op := StartOp(context.Background(), "mkdir", FolderID(1))
			op.End(0)
		}
		if d := time.Since(started); d > 2*time.Second {
			t.Fatalf("%s: operations took %v", endpoint, d)
		}
		started = time.Now()
		stop(within)
		if d := time.Since(started); d > within+time.Second {
			t.Fatalf("%s: stop took %v", endpoint, d)
		}
	}
}
