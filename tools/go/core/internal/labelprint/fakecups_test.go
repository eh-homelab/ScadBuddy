package labelprint

import (
	"net/http"
	"net/http/httptest"
	"net/url"
	"sync"
	"testing"
)

// fakeQueue is one printer the fake CUPS server lists.
type fakeQueue struct {
	Name, Info, MakeAndModel, Message, Media string
	State                                    int32
	Reasons                                  []string
	NotAccepting                             bool
}

// fakeRequest is one request the fake server decoded.
type fakeRequest struct {
	Path        string
	ContentType string
	Msg         Message
}

// fakeCUPS is an IPP server on httptest. It answers CUPS-Get-Printers with
// its queues and Print-Job with job 7.
type fakeCUPS struct {
	*httptest.Server
	URL *url.URL

	// block is closed when the test ends. A handler waits on it when hang is set.
	block chan struct{}
	hang  bool // guarded by mu
	// hangPrint makes only Print-Job wait on block.
	hangPrint bool // guarded by mu

	mu           sync.Mutex
	queues       []fakeQueue
	requests     []fakeRequest
	printStatus  uint16   // status of the next Print-Job reply
	printMessage string   // its status-message
	listStatus   uint16   // status of every CUPS-Get-Printers reply
	replyID      *uint32  // when set, the request-id of every reply
	jobState     int32    // job-state of the next accepted job; 0 is 5 (processing)
	jobReasons   []string // its job-state-reasons; nil is job-printing
}

func newFakeCUPS(t *testing.T, queues []fakeQueue) *fakeCUPS {
	t.Helper()
	f := &fakeCUPS{queues: queues, block: make(chan struct{})}
	f.Server = httptest.NewServer(http.HandlerFunc(f.serve))
	t.Cleanup(f.Server.Close)
	t.Cleanup(func() { close(f.block) }) // runs first, so the handlers return before Close
	u, err := url.Parse(f.Server.URL)
	if err != nil {
		t.Fatal(err)
	}
	f.URL = u
	return f
}

func (f *fakeCUPS) setPrintReply(status uint16, message string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.printStatus, f.printMessage = status, message
}

func (f *fakeCUPS) setListStatus(status uint16) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.listStatus = status
}

func (f *fakeCUPS) setHang(v bool) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.hang = v
}

func (f *fakeCUPS) setHangPrint(v bool) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.hangPrint = v
}

func (f *fakeCUPS) setJobState(state int32, reasons ...string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.jobState, f.jobReasons = state, reasons
}

func (f *fakeCUPS) setReplyID(id uint32) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.replyID = &id
}

func (f *fakeCUPS) recorded() []fakeRequest {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]fakeRequest(nil), f.requests...)
}

func (f *fakeCUPS) serve(w http.ResponseWriter, r *http.Request) {
	msg, err := Decode(r.Body, 16<<20)
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	f.mu.Lock()
	f.requests = append(f.requests, fakeRequest{Path: r.URL.EscapedPath(), ContentType: r.Header.Get("Content-Type"), Msg: msg})
	status, message, hang, replyID := f.printStatus, f.printMessage, f.hang, f.replyID
	if msg.Code == 0x4002 {
		status = f.listStatus
	}
	hang = hang || (f.hangPrint && msg.Code == 0x0002)
	state, reasons := f.jobState, f.jobReasons
	f.mu.Unlock()
	if hang {
		select {
		case <-f.block:
		case <-r.Context().Done():
		}
		return
	}
	if replyID != nil {
		msg.RequestID = *replyID
	}
	f.reply(w, msg, status, message, state, reasons)
}

func (f *fakeCUPS) reply(w http.ResponseWriter, req Message, status uint16, message string, state int32, reasons []string) {
	if state == 0 {
		state = 5
	}
	if reasons == nil {
		reasons = []string{"job-printing"}
	}
	out := Message{Version: [2]byte{1, 1}, RequestID: req.RequestID}
	out.Attrs = []Attr{
		StrAttr(TagOperation, "attributes-charset", TagCharset, "utf-8"),
		StrAttr(TagOperation, "attributes-natural-language", TagLanguage, "en"),
	}
	switch req.Code {
	case 0x4002:
		out.Code = status
		for _, q := range f.queues {
			out.Attrs = append(out.Attrs, groupStart(TagPrinter),
				StrAttr(TagPrinter, "printer-name", TagName, q.Name),
				StrAttr(TagPrinter, "printer-info", TagText, q.Info),
				StrAttr(TagPrinter, "printer-make-and-model", TagText, q.MakeAndModel),
				IntAttr(TagPrinter, "printer-state", TagEnum, q.State),
				StrAttr(TagPrinter, "printer-state-reasons", TagKeyword, q.Reasons...),
				StrAttr(TagPrinter, "printer-state-message", TagText, q.Message),
				IntAttr(TagPrinter, "printer-is-accepting-jobs", TagBoolean, b2i(!q.NotAccepting)),
				StrAttr(TagPrinter, "media-default", TagKeyword, q.Media),
			)
		}
	case 0x0002:
		out.Code = status
		if message != "" {
			out.Attrs = append(out.Attrs, StrAttr(TagOperation, "status-message", TagText, message))
		}
		if status <= 0xFF {
			out.Attrs = append(out.Attrs,
				groupStart(TagJob),
				IntAttr(TagJob, "job-id", TagInteger, 7),
				IntAttr(TagJob, "job-state", TagEnum, state),
				StrAttr(TagJob, "job-state-reasons", TagKeyword, reasons...),
			)
		}
	}
	w.Header().Set("Content-Type", "application/ipp")
	_ = out.Encode(w)
}

func b2i(b bool) int32 {
	if b {
		return 1
	}
	return 0
}
