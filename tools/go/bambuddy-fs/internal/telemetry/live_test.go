package telemetry

import (
	"bytes"
	"context"
	"log/slog"
	"os"
	"testing"
	"time"
)

// TestLiveExport sends one span and one metric to the real collector (the default
// endpoint, or OTEL_EXPORTER_OTLP_ENDPOINT) and fails if the SDK reported an error.
// It runs only with BAMBUDDY_FS_TEST_OTLP_LIVE=1, as it needs the LAN; the series
// is bambuddy_fs_fuse_operations_total{job="bambuddy-fs",op="live_check"}.
func TestLiveExport(t *testing.T) {
	if os.Getenv("BAMBUDDY_FS_TEST_OTLP_LIVE") != "1" {
		t.Skip("BAMBUDDY_FS_TEST_OTLP_LIVE=1 sends to the real collector")
	}
	var buf bytes.Buffer
	prev := slog.Default()
	slog.SetDefault(slog.New(slog.NewTextHandler(&buf, nil)))
	t.Cleanup(func() { slog.SetDefault(prev) })
	prevTimeout := shutdownTimeout
	shutdownTimeout = 15 * time.Second
	t.Cleanup(func() { shutdownTimeout = prevTimeout })

	stop, err := Start(Config{})
	if err != nil {
		t.Fatal(err)
	}
	_, op := StartOp(context.Background(), "live_check", FolderID(0))
	op.End(0)
	stop()
	if buf.Len() != 0 {
		t.Fatalf("export failed: %s", buf.String())
	}
}
