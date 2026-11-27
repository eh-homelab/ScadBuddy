package labelprint

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"math"
	"net/http"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"time"
)

// replyLimit is the most bytes core reads from a CUPS reply.
const replyLimit = 1 << 20

// messageLimit is the most characters of a status-message core keeps.
const messageLimit = 300

// ErrUnreachable means the label server did not answer with an IPP message.
var ErrUnreachable = errors.New("label server unreachable")

// ErrQueueUnknown means the label server has no such queue.
var ErrQueueUnknown = errors.New("label queue unknown")

// RefusedError is an IPP error status from the label server.
type RefusedError struct {
	Status  uint16
	Message string
}

func (e *RefusedError) Error() string { return e.Message }

// Queue is one CUPS print queue.
type Queue struct {
	Name         string   `json:"name"`
	Info         string   `json:"info"`
	MakeAndModel string   `json:"make_and_model"`
	State        string   `json:"state"`
	StateMessage string   `json:"state_message"`
	Media        string   `json:"media"`
	StateReasons []string `json:"state_reasons"`
	Accepting    bool     `json:"accepting"`
	WidthMM      *float64 `json:"width_mm"`
	HeightMM     *float64 `json:"height_mm"`
}

// Options are the PPD choice names of a job. "" is the default.
type Options struct{ MediaType, Darkness, PrintRate string }

// Job is a job the label server accepted.
type Job struct {
	Queue        string   `json:"queue"`
	ID           int32    `json:"job_id"`
	State        string   `json:"job_state"`
	StateReasons []string `json:"job_state_reasons"`
}

// Default timeouts of a Client. There is no way to turn a timeout off.
const (
	defaultListTimeout  = 5 * time.Second
	defaultPrintTimeout = 30 * time.Second
)

// queueName is the shape of a queue name core sends to CUPS.
var queueName = regexp.MustCompile(`^[A-Za-z0-9_-]{1,127}$`)

// ValidQueue reports whether name is a queue name core sends to CUPS.
func ValidQueue(name string) bool { return queueName.MatchString(name) }

// Client talks IPP to a CUPS server. Base may carry a path prefix. A nil HTTP
// means a client that ignores the proxy environment variables. A zero
// ListTimeout is 5 s and a zero PrintTimeout is 30 s.
type Client struct {
	Base                      *url.URL
	HTTP                      *http.Client
	ListTimeout, PrintTimeout time.Duration
}

func (c *Client) listTimeout() time.Duration {
	if c.ListTimeout > 0 {
		return c.ListTimeout
	}
	return defaultListTimeout
}

func (c *Client) printTimeout() time.Duration {
	if c.PrintTimeout > 0 {
		return c.PrintTimeout
	}
	return defaultPrintTimeout
}

// directHTTP is the client used when Client.HTTP is nil. Its transport never
// uses a proxy, so the label server is never reached through one.
var directHTTP = func() *http.Client {
	t := http.DefaultTransport.(*http.Transport).Clone()
	t.Proxy = nil
	return &http.Client{Transport: t}
}()

func (c *Client) client() *http.Client {
	if c.HTTP != nil {
		return c.HTTP
	}
	return directHTTP
}

var queueAttrs = []string{
	"printer-name", "printer-info", "printer-make-and-model", "printer-state",
	"printer-state-reasons", "printer-state-message", "printer-is-accepting-jobs", "media-default",
}

var printerStates = map[int32]string{3: "idle", 4: "processing", 5: "stopped"}

// https://www.rfc-editor.org/rfc/rfc8011#section-5.3.7
var jobStates = map[int32]string{
	3: "pending", 4: "pending-held", 5: "processing", 6: "processing-stopped",
	7: "canceled", 8: "aborted", 9: "completed",
}

// Queues lists the queues of the server, in its order.
func (c *Client) Queues(ctx context.Context) ([]Queue, error) {
	req := Message{Version: [2]byte{1, 1}, Code: 0x4002, RequestID: 1, Attrs: []Attr{
		StrAttr(TagOperation, "attributes-charset", TagCharset, "utf-8"),
		StrAttr(TagOperation, "attributes-natural-language", TagLanguage, "en"),
		StrAttr(TagOperation, "requested-attributes", TagKeyword, queueAttrs...),
	}}
	reply, err := c.do(ctx, c.listTimeout(), c.path("/"), req)
	if err != nil {
		return nil, err
	}
	if err := statusError(reply); err != nil {
		return nil, err
	}
	var out []Queue
	for _, g := range reply.Groups(TagPrinter) {
		p := Message{Attrs: g}
		first := func(name string) string {
			if s := p.Strings(TagPrinter, name); len(s) > 0 {
				return s[0]
			}
			return ""
		}
		q := Queue{
			Name:         first("printer-name"),
			Info:         first("printer-info"),
			MakeAndModel: first("printer-make-and-model"),
			StateMessage: first("printer-state-message"),
			Media:        first("media-default"),
			StateReasons: p.Strings(TagPrinter, "printer-state-reasons"),
		}
		if q.StateReasons == nil {
			q.StateReasons = []string{}
		}
		if s, ok := p.Int(TagPrinter, "printer-state"); ok {
			q.State = printerStates[s]
		}
		if v, ok := p.Int(TagPrinter, "printer-is-accepting-jobs"); ok {
			q.Accepting = v != 0
		}
		if w, h, ok := SizeOf(q.Media); ok {
			q.WidthMM, q.HeightMM = &w, &h
		}
		out = append(out, q)
	}
	return out, nil
}

// Print sends the document to the queue as one Print-Job.
func (c *Client) Print(ctx context.Context, queue string, doc []byte, o Options) (Job, error) {
	if !ValidQueue(queue) {
		return Job{}, ErrQueueUnknown
	}
	req := Message{Version: [2]byte{1, 1}, Code: 0x0002, RequestID: 1, Data: doc, Attrs: []Attr{
		StrAttr(TagOperation, "attributes-charset", TagCharset, "utf-8"),
		StrAttr(TagOperation, "attributes-natural-language", TagLanguage, "en"),
		StrAttr(TagOperation, "printer-uri", TagURI, "ipp://"+c.Base.Host+c.basePath()+"/printers/"+queue),
		StrAttr(TagOperation, "requesting-user-name", TagName, "scadbuddy"),
		StrAttr(TagOperation, "job-name", TagName, "ScadBuddy labels"),
		StrAttr(TagOperation, "document-format", TagMimeType, "application/pdf"),
	}}
	for _, opt := range []struct{ name, value string }{
		{"MediaType", o.MediaType}, {"Darkness", o.Darkness}, {"zePrintRate", o.PrintRate},
	} {
		if opt.value != "" {
			req.Attrs = append(req.Attrs, StrAttr(TagJob, opt.name, TagName, opt.value))
		}
	}
	reply, err := c.do(ctx, c.printTimeout(), c.path("/printers/"+queue), req)
	if err != nil {
		return Job{}, err
	}
	if err := statusError(reply); err != nil {
		return Job{}, err
	}
	id, ok := reply.Int(TagJob, "job-id")
	if !ok {
		return Job{}, fmt.Errorf("%w: the reply has no job-id", ErrUnreachable)
	}
	job := Job{Queue: queue, ID: id, StateReasons: reply.Strings(TagJob, "job-state-reasons")}
	if job.StateReasons == nil {
		job.StateReasons = []string{}
	}
	if s, ok := reply.Int(TagJob, "job-state"); ok {
		job.State = jobStates[s]
	}
	return job, nil
}

// basePath is the escaped path of Base without a trailing slash.
func (c *Client) basePath() string { return strings.TrimSuffix(c.Base.EscapedPath(), "/") }

// path is the request URL: the base with the given escaped path below it.
func (c *Client) path(escaped string) *url.URL {
	u := *c.Base
	u.RawPath = c.basePath() + escaped
	u.Path, _ = url.PathUnescape(u.RawPath)
	u.RawQuery, u.Fragment = "", ""
	return &u
}

func (c *Client) do(ctx context.Context, timeout time.Duration, u *url.URL, req Message) (Message, error) {
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	var body bytes.Buffer
	if err := req.Encode(&body); err != nil {
		return Message{}, err
	}
	hr, err := http.NewRequestWithContext(ctx, http.MethodPost, u.String(), &body)
	if err != nil {
		return Message{}, err
	}
	hr.Header.Set("Content-Type", "application/ipp")
	hc := *c.client()
	hc.CheckRedirect = func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }
	resp, err := hc.Do(hr)
	if err != nil {
		return Message{}, fmt.Errorf("%w: %v", ErrUnreachable, err)
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode != http.StatusOK {
		_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, replyLimit))
		return Message{}, fmt.Errorf("%w: HTTP %d", ErrUnreachable, resp.StatusCode)
	}
	reply, err := Decode(resp.Body, replyLimit)
	if err != nil {
		return Message{}, fmt.Errorf("%w: %v", ErrUnreachable, err)
	}
	if reply.RequestID != req.RequestID {
		return Message{}, fmt.Errorf("%w: the reply answers request %d, not %d", ErrUnreachable, reply.RequestID, req.RequestID)
	}
	return reply, nil
}

// statusError turns an IPP status above 0x00FF into an error.
// https://www.rfc-editor.org/rfc/rfc8011#section-4.1.6
func statusError(m Message) error {
	switch {
	case m.Code <= 0x00FF:
		return nil
	case m.Code == 0x0406:
		return ErrQueueUnknown
	}
	msg := ""
	if s := m.Strings(TagOperation, "status-message"); len(s) > 0 {
		msg = s[0]
	}
	if r := []rune(msg); len(r) > messageLimit {
		msg = string(r[:messageLimit])
	}
	if msg == "" {
		msg = fmt.Sprintf("the label server refused the request (IPP status %#04x)", m.Code)
	}
	return &RefusedError{Status: m.Code, Message: msg}
}

var sizePart = regexp.MustCompile(`^(\d+(?:\.\d+)?)x(\d+(?:\.\d+)?)(in|mm)$`)

// SizeOf reads the size, in mm, from the last part of a PWG self-describing
// media name, such as oe_2x1-label_2x1in.
// https://ftp.pwg.org/pub/pwg/candidates/cs-pwgmsn10-20020226-5101.1.pdf
func SizeOf(media string) (w, h float64, ok bool) {
	m := sizePart.FindStringSubmatch(media[strings.LastIndex(media, "_")+1:])
	if m == nil {
		return 0, 0, false
	}
	w, _ = strconv.ParseFloat(m[1], 64)
	h, _ = strconv.ParseFloat(m[2], 64)
	if m[3] == "in" {
		w, h = w*25.4, h*25.4
	}
	if w <= 0 || h <= 0 || math.IsInf(w, 0) || math.IsInf(h, 0) {
		return 0, 0, false
	}
	round := func(v float64) float64 { return math.Round(v*1000) / 1000 }
	return round(w), round(h), true
}
