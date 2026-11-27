package labelprint

import (
	"bytes"
	"context"
	"errors"
	"math"
	"net/http"
	"net/http/httptest"
	"net/url"
	"reflect"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

const loadedMsg = "1.02 in labels are loaded; this queue prints 6.00 in labels. Load the right labels."

func twoQueues() []fakeQueue {
	return []fakeQueue{
		{Name: "S-EDCDU03-2x1", Info: "2x1", MakeAndModel: "Zebra ZPL", Media: "oe_2x1-label_2x1in", State: 3, Reasons: []string{"none"}},
		{Name: "S-EDCDU03-4x6", Info: "4x6", MakeAndModel: "Zebra ZPL", Media: "oe_4x6-label_4x6in", State: 4, Reasons: []string{"media-needed"}, Message: loadedMsg, NotAccepting: true},
	}
}

func newClient(f *fakeCUPS) *Client {
	return &Client{Base: f.URL, HTTP: f.Client(), ListTimeout: 5 * time.Second, PrintTimeout: 5 * time.Second}
}

func near(a, b float64) bool { return math.Abs(a-b) < 1e-9 }

func TestQueuesReadsCUPSList(t *testing.T) {
	f := newFakeCUPS(t, twoQueues())
	qs, err := newClient(f).Queues(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if len(qs) != 2 || qs[0].Name != "S-EDCDU03-2x1" || qs[1].Name != "S-EDCDU03-4x6" {
		t.Fatalf("queues = %+v", qs)
	}
	q := qs[0]
	if q.State != "idle" || !q.Accepting || q.Media != "oe_2x1-label_2x1in" || q.Info != "2x1" || q.MakeAndModel != "Zebra ZPL" {
		t.Errorf("first = %+v", q)
	}
	if q.WidthMM == nil || q.HeightMM == nil || !near(*q.WidthMM, 50.8) || !near(*q.HeightMM, 25.4) {
		t.Errorf("size = %v x %v", q.WidthMM, q.HeightMM)
	}
	s := qs[1]
	if s.State != "processing" || s.Accepting || s.StateMessage != loadedMsg || !reflect.DeepEqual(s.StateReasons, []string{"media-needed"}) {
		t.Errorf("second = %+v", s)
	}
	reqs := f.recorded()
	if len(reqs) != 1 || reqs[0].Msg.Code != 0x4002 || reqs[0].Path != "/" || reqs[0].ContentType != "application/ipp" {
		t.Fatalf("requests = %+v", reqs)
	}
	want := []string{"printer-name", "printer-info", "printer-make-and-model", "printer-state",
		"printer-state-reasons", "printer-state-message", "printer-is-accepting-jobs", "media-default"}
	if got := reqs[0].Msg.Strings(TagOperation, "requested-attributes"); !reflect.DeepEqual(got, want) {
		t.Errorf("requested-attributes = %v", got)
	}
}

func TestSizeOf(t *testing.T) {
	for _, c := range []struct {
		media string
		w, h  float64
	}{
		{"oe_2x1-label_2x1in", 50.8, 25.4},
		{"na_letter_8.5x11in", 215.9, 279.4},
		{"iso_a4_210x297mm", 210, 297},
	} {
		w, h, ok := SizeOf(c.media)
		if !ok || !near(w, c.w) || !near(h, c.h) {
			t.Errorf("SizeOf(%q) = %v, %v, %v", c.media, w, h, ok)
		}
	}
	for _, m := range []string{"custom", "x_1xin", "", "a_0x5mm"} {
		if _, _, ok := SizeOf(m); ok {
			t.Errorf("SizeOf(%q) is ok", m)
		}
	}
}

func TestPrintSendsThePDFAndOptions(t *testing.T) {
	f := newFakeCUPS(t, twoQueues())
	pdf := []byte("%PDF-1.4 label")
	job, err := newClient(f).Print(context.Background(), "S-EDCDU03-2x1", pdf, Options{MediaType: "Thermal", Darkness: "20"})
	if err != nil {
		t.Fatal(err)
	}
	want := Job{Queue: "S-EDCDU03-2x1", ID: 7, State: "processing", StateReasons: []string{"job-printing"}}
	if !reflect.DeepEqual(job, want) {
		t.Errorf("job = %+v", job)
	}
	reqs := f.recorded()
	if len(reqs) != 1 {
		t.Fatalf("requests = %d", len(reqs))
	}
	r := reqs[0]
	if r.Msg.Code != 0x0002 || r.Path != "/printers/S-EDCDU03-2x1" || r.ContentType != "application/ipp" {
		t.Errorf("request = %+v", r)
	}
	one := func(g Tag, name string) string {
		s := r.Msg.Strings(g, name)
		if len(s) != 1 {
			t.Fatalf("%s = %v", name, s)
		}
		return s[0]
	}
	if got := one(TagOperation, "printer-uri"); got != "ipp://"+f.URL.Host+"/printers/S-EDCDU03-2x1" {
		t.Errorf("printer-uri = %q", got)
	}
	for name, v := range map[string]string{"document-format": "application/pdf", "job-name": "ScadBuddy labels", "requesting-user-name": "scadbuddy"} {
		if got := one(TagOperation, name); got != v {
			t.Errorf("%s = %q", name, got)
		}
	}
	for _, a := range r.Msg.Attrs {
		if a.Group == TagJob && a.Tag != 0 && a.Tag != TagName {
			t.Errorf("job attr %s has tag %#x", a.Name, byte(a.Tag))
		}
	}
	if one(TagJob, "MediaType") != "Thermal" || one(TagJob, "Darkness") != "20" {
		t.Error("job options wrong")
	}
	if got := r.Msg.Strings(TagJob, "zePrintRate"); len(got) != 0 {
		t.Errorf("zePrintRate = %v", got)
	}
	if !bytes.Equal(r.Msg.Data, pdf) {
		t.Errorf("data = %q", r.Msg.Data)
	}
}

func TestPrintEscapesTheQueueInThePath(t *testing.T) {
	f := newFakeCUPS(t, nil)
	if _, err := newClient(f).Print(context.Background(), "a b/c", []byte("%PDF-"), Options{}); err != nil {
		t.Fatal(err)
	}
	if got := f.recorded()[0].Path; got != "/printers/a%20b%2Fc" {
		t.Errorf("path = %q", got)
	}
}

func TestPrintQueueGoneIsUnknown(t *testing.T) {
	f := newFakeCUPS(t, twoQueues())
	f.setPrintReply(0x0406, "")
	_, err := newClient(f).Print(context.Background(), "gone", []byte("%PDF-"), Options{})
	if !errors.Is(err, ErrQueueUnknown) {
		t.Fatalf("err = %v", err)
	}
}

func TestPrintRefusedCarriesTheMessage(t *testing.T) {
	f := newFakeCUPS(t, twoQueues())
	f.setPrintReply(0x0506, "Destination is not accepting jobs.")
	_, err := newClient(f).Print(context.Background(), "S-EDCDU03-2x1", []byte("%PDF-"), Options{})
	var re *RefusedError
	if !errors.As(err, &re) || re.Status != 0x0506 || re.Message != "Destination is not accepting jobs." || err.Error() != re.Message {
		t.Fatalf("err = %#v", err)
	}
}

func TestRefusedMessageIsCutTo300Characters(t *testing.T) {
	f := newFakeCUPS(t, nil)
	f.setPrintReply(0x0500, strings.Repeat("é", 400))
	_, err := newClient(f).Print(context.Background(), "q", []byte("%PDF-"), Options{})
	var re *RefusedError
	if !errors.As(err, &re) || len([]rune(re.Message)) != 300 {
		t.Fatalf("err = %#v", err)
	}
}

func TestNotIPPIsUnreachable(t *testing.T) {
	html := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/html")
		_, _ = w.Write([]byte("<html>"))
	}))
	defer html.Close()
	var hits atomic.Int32
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { hits.Add(1) }))
	defer target.Close()
	redirect := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, target.URL, http.StatusFound)
	}))
	defer redirect.Close()
	for name, srv := range map[string]*httptest.Server{"html": html, "redirect": redirect} {
		base, _ := url.Parse(srv.URL)
		c := &Client{Base: base, ListTimeout: 5 * time.Second, PrintTimeout: 5 * time.Second}
		if _, err := c.Queues(context.Background()); !errors.Is(err, ErrUnreachable) {
			t.Errorf("%s Queues: %v", name, err)
		}
		if _, err := c.Print(context.Background(), "q", []byte("%PDF-"), Options{}); !errors.Is(err, ErrUnreachable) {
			t.Errorf("%s Print: %v", name, err)
		}
	}
	if hits.Load() != 0 {
		t.Errorf("the redirect target got %d requests", hits.Load())
	}
}

func TestReplyPastTheLimitIsUnreachable(t *testing.T) {
	big := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/ipp")
		_, _ = w.Write(make([]byte, 2<<20))
	}))
	defer big.Close()
	base, _ := url.Parse(big.URL)
	c := &Client{Base: base, ListTimeout: 5 * time.Second}
	if _, err := c.Queues(context.Background()); !errors.Is(err, ErrUnreachable) {
		t.Fatalf("err = %v", err)
	}
}

func TestDeadlineIsUnreachable(t *testing.T) {
	f := newFakeCUPS(t, nil)
	f.hang = true
	c := newClient(f)
	c.ListTimeout = 50 * time.Millisecond
	c.PrintTimeout = 50 * time.Millisecond
	if _, err := c.Queues(context.Background()); !errors.Is(err, ErrUnreachable) {
		t.Errorf("Queues: %v", err)
	}
	if _, err := c.Print(context.Background(), "q", []byte("%PDF-"), Options{}); !errors.Is(err, ErrUnreachable) {
		t.Errorf("Print: %v", err)
	}
}
