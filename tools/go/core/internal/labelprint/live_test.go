package labelprint

import (
	"context"
	"errors"
	"math"
	"net/url"
	"os"
	"testing"
)

// TestLiveCUPS checks the client against a real cupsd: ci.yml's `cups-image` job
// runs it against the image's `cups` container, with the queues of its smoke step.
// It skips unless SCADBUDDY_TEST_CUPS_URL is set. SCADBUDDY_TEST_CUPS_PDF is the PDF
// it prints; the job then checks that the ZPL carries Darkness and MediaType.
func TestLiveCUPS(t *testing.T) {
	raw := os.Getenv("SCADBUDDY_TEST_CUPS_URL")
	if raw == "" {
		t.Skip("SCADBUDDY_TEST_CUPS_URL is not set")
	}
	base, err := url.Parse(raw)
	if err != nil {
		t.Fatal(err)
	}
	pdf, err := os.ReadFile(os.Getenv("SCADBUDDY_TEST_CUPS_PDF"))
	if err != nil {
		t.Fatal(err)
	}
	c := &Client{Base: base}
	ctx := context.Background()

	qs, err := c.Queues(ctx)
	if err != nil {
		t.Fatal(err)
	}
	var small *Queue
	for i, q := range qs {
		t.Logf("queue %s: media-default %q", q.Name, q.Media)
		if q.Name == "S-EDCDU03-2x1" {
			small = &qs[i]
		}
	}
	if small == nil {
		t.Fatalf("no queue S-EDCDU03-2x1 in %+v", qs)
	}
	if small.WidthMM == nil || small.HeightMM == nil ||
		math.Abs(*small.WidthMM-50.8) > 1 || math.Abs(*small.HeightMM-25.4) > 1 {
		t.Fatalf("S-EDCDU03-2x1 size from %q = %v x %v, want 50.8 x 25.4 mm", small.Media, small.WidthMM, small.HeightMM)
	}
	if small.MakeAndModel != "Zebra ZPL Label Printer" {
		t.Fatalf("S-EDCDU03-2x1 make and model = %q", small.MakeAndModel)
	}

	job, err := c.Print(ctx, "S-EDCDU03-2x1", pdf, Options{MediaType: "Thermal", Darkness: "30"})
	if err != nil {
		t.Fatal(err)
	}
	if job.ID <= 0 {
		t.Fatalf("job = %+v", job)
	}
	t.Logf("job %d: %s %v", job.ID, job.State, job.StateReasons)

	if _, err := c.Print(ctx, "Nope", pdf, Options{}); !errors.Is(err, ErrQueueUnknown) {
		t.Fatalf("print to Nope: err = %v, want ErrQueueUnknown", err)
	}
}
