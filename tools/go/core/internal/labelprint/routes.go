package labelprint

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/auth"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
	"github.com/eh-homelab/ScadBuddy/tools/go/telemetry"
)

const (
	// Area names the routes (#7170).
	Area = "label-printing"
	// DefaultServer is the cluster's own CUPS server, used when the setting has no value.
	DefaultServer = "http://cups.cups.svc:8631"
	// ServerSetting is the stored setting that names the CUPS server.
	ServerSetting = "filament_label_server_url"
	// MaxBody is the most bytes of a label PDF the print route takes.
	MaxBody = 8 << 20

	queuesPath = "/api/v1/label-printing/queues"
	jobsPath   = queuesPath + "/{queue}/jobs"
)

// Service is the area's routes. Server, when not nil, gives the CUPS server; nil
// means the stored setting, read from Pool. A nil HTTP is a client that uses no proxy.
type Service struct {
	Pool                      *pgxpool.Pool
	Server                    func(ctx context.Context) (*url.URL, error)
	HTTP                      *http.Client
	ListTimeout, PrintTimeout time.Duration
}

// New is the Service over the pool, its requests traced as peer "cups". The list
// times out after 5 s and the print after 30 s.
func New(pool *pgxpool.Pool, tel *telemetry.Telemetry) *Service {
	return &Service{
		Pool:         pool,
		HTTP:         &http.Client{Transport: tel.Transport(httpx.Outside(directHTTP.Transport), "cups")},
		ListTimeout:  defaultListTimeout,
		PrintTimeout: defaultPrintTimeout,
	}
}

// Public are the two routes, class "api". newRouter serves them behind the gate.
func (s *Service) Public() []auth.Public {
	return []auth.Public{
		{
			Pattern: "GET " + queuesPath,
			Route:   auth.Route{Class: "api", Template: queuesPath},
			Handler: http.HandlerFunc(s.serveQueues),
		},
		{
			Pattern: "POST " + jobsPath,
			Route:   auth.Route{Class: "api", Template: jobsPath},
			Handler: http.HandlerFunc(s.servePrint),
		},
	}
}

// errBadServer means the stored server URL breaks the rule of ParseServer.
// It never carries the stored value: the value may hold a secret.
var errBadServer = errors.New("the stored label server URL is not valid")

// ParseServer checks a CUPS server's URL: http or https, a host, no user, no query, no
// fragment, and a path of "/" or none. It is the rule of Python's label_printing.server_url.
func ParseServer(raw string) (*url.URL, error) {
	u, err := url.Parse(strings.TrimSpace(raw))
	if err != nil {
		return nil, errBadServer
	}
	bad := (u.Scheme != "http" && u.Scheme != "https") || u.Opaque != "" || u.Hostname() == "" ||
		u.User != nil || u.RawQuery != "" || u.ForceQuery || u.Fragment != "" ||
		(u.Path != "" && u.Path != "/")
	if !bad && u.Port() != "" {
		port, err := strconv.Atoi(u.Port())
		bad = err != nil || port > 65535
	}
	if bad {
		return nil, errBadServer
	}
	u.Path, u.RawPath = "", ""
	return u, nil
}

// server is the CUPS server to use: Server's, or the stored setting's.
func (s *Service) server(ctx context.Context) (*url.URL, error) {
	if s.Server != nil {
		return s.Server(ctx)
	}
	var value json.RawMessage
	err := s.Pool.QueryRow(ctx, `SELECT value FROM settings WHERE name = $1`, ServerSetting).Scan(&value)
	if errors.Is(err, pgx.ErrNoRows) {
		return ParseServer(DefaultServer)
	}
	if err != nil {
		return nil, err
	}
	var stored *string
	if err := json.Unmarshal(value, &stored); err != nil {
		return nil, errBadServer
	}
	if stored == nil {
		return ParseServer(DefaultServer)
	}
	return ParseServer(*stored)
}

func (s *Service) client(ctx context.Context) (*Client, error) {
	base, err := s.server(ctx)
	if err != nil {
		return nil, err
	}
	return &Client{Base: base, HTTP: s.HTTP, ListTimeout: s.ListTimeout, PrintTimeout: s.PrintTimeout}, nil
}

func (s *Service) serveQueues(w http.ResponseWriter, r *http.Request) {
	c, err := s.client(r.Context())
	if err != nil {
		fail(w, r, err)
		return
	}
	qs, err := c.Queues(r.Context())
	if err != nil {
		fail(w, r, err)
		return
	}
	if qs == nil {
		qs = []Queue{}
	}
	if err := httpx.WriteJSON(w, map[string]any{"queues": qs}); err != nil {
		fail(w, r, err)
	}
}

func (s *Service) servePrint(w http.ResponseWriter, r *http.Request) {
	doc, err := io.ReadAll(http.MaxBytesReader(w, r.Body, MaxBody))
	if err != nil {
		var tooLarge *http.MaxBytesError
		if errors.As(err, &tooLarge) {
			problem(w, r, http.StatusRequestEntityTooLarge, "A label PDF is at most 8 MiB.", "label_too_large")
			return
		}
		fail(w, r, err)
		return
	}
	if !bytes.HasPrefix(doc, []byte("%PDF-")) {
		problem(w, r, http.StatusUnsupportedMediaType, "The body is not a PDF.", "label_not_pdf")
		return
	}
	opts, ok := options(r.URL.Query())
	if !ok {
		problem(w, r, http.StatusUnprocessableEntity,
			"media_type is Saved, Thermal or Direct; darkness is -1 or 1 to 30; print_rate is Default or 1 to 12.",
			"label_option_invalid")
		return
	}
	queue := r.PathValue("queue")
	if !ValidQueue(queue) {
		fail(w, r, ErrQueueUnknown)
		return
	}
	c, err := s.client(r.Context())
	if err != nil {
		fail(w, r, err)
		return
	}
	qs, err := c.Queues(r.Context())
	if err != nil {
		fail(w, r, err)
		return
	}
	if !slices.ContainsFunc(qs, func(q Queue) bool { return q.Name == queue }) {
		fail(w, r, ErrQueueUnknown)
		return
	}
	job, err := c.Print(r.Context(), queue, doc, opts)
	if err != nil {
		fail(w, r, err)
		return
	}
	slog.InfoContext(r.Context(), "label job", "queue", queue, "job_id", job.ID, "bytes", len(doc),
		"caller", auth.CallerOf(r.Context()).Kind)
	if err := httpx.WriteJSONStatus(w, http.StatusAccepted, job); err != nil {
		fail(w, r, err)
	}
}

var (
	darkness  = regexp.MustCompile(`^([1-9]|[12][0-9]|30)$`)
	printRate = regexp.MustCompile(`^([1-9]|1[0-2])$`)
)

// options reads the query's job options. A missing value, or the PPD's default choice, is "".
func options(q url.Values) (Options, bool) {
	var o Options
	switch v := q.Get("media_type"); v {
	case "", "Saved":
	case "Thermal", "Direct":
		o.MediaType = v
	default:
		return o, false
	}
	switch v := q.Get("darkness"); {
	case v == "" || v == "-1":
	case darkness.MatchString(v):
		o.Darkness = v
	default:
		return o, false
	}
	switch v := q.Get("print_rate"); {
	case v == "" || v == "Default":
	case printRate.MatchString(v):
		o.PrintRate = v
	default:
		return o, false
	}
	return o, true
}

func problem(w http.ResponseWriter, r *http.Request, status int, detail, reason string) {
	httpx.WriteProblem(w, r, httpx.WithReason(status, detail, reason))
}

// fail answers an error as design R8 maps it.
func fail(w http.ResponseWriter, r *http.Request, err error) {
	var refused *RefusedError
	switch {
	case errors.Is(err, ErrQueueUnknown):
		problem(w, r, http.StatusUnprocessableEntity, "The label print server has no such queue.", "label_queue_unknown")
	case errors.As(err, &refused):
		p := httpx.WithReason(http.StatusBadGateway, refused.Message, "label_print_refused")
		p.Extensions = append(p.Extensions, httpx.Field{Key: "ipp_status", Value: refused.Status})
		httpx.WriteProblem(w, r, p)
	case errors.Is(err, errBadServer), errors.Is(err, ErrUnreachable):
		slog.WarnContext(r.Context(), "the label print server is out of reach", "path", r.URL.Path, "error", err.Error())
		problem(w, r, http.StatusBadGateway, "The label print server did not answer.", "label_server_unreachable")
	default:
		httpx.WriteProblem(w, r, crud.ProblemOf(r, err))
	}
}
