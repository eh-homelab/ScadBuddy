// Package ghwebhook is the fleet server's receiver of the "ScadBuddy Fleet" GitHub App's
// webhook deliveries (#8014), served at /github/webhook. It checks X-Hub-Signature-256
// before anything else, then logs and counts the installation and
// installation_repositories events, by counts and ids only. It answers every other event
// with 204 and does not parse it. README.md ("Package ghwebhook") has the answers.
package ghwebhook

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"strings"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/metric"
	"go.opentelemetry.io/otel/metric/noop"
)

const (
	// MaxBody is the largest delivery read, in bytes. GitHub caps a payload at 25 MB, but an
	// installation event with every repository of a large account is far smaller.
	MaxBody = 1 << 20

	eventsMetric = "fleet.github.webhook.installation_events"
	scope        = "github.com/eh-homelab/ScadBuddy/tools/go/fleet/ghwebhook"
)

// Options are the handler's logger and meter.
type Options struct {
	// Logger is slog.Default() when nil.
	Logger *slog.Logger
	// Meter is a no-op meter when nil.
	Meter metric.MeterProvider
}

type handler struct {
	secret []byte
	log    *slog.Logger
	events metric.Int64Counter
}

// New returns the receiver. secret is the webhook secret set on the App; it is never logged.
func New(secret []byte, o Options) (http.Handler, error) {
	if len(secret) == 0 {
		return nil, errors.New("ghwebhook: the webhook secret is empty")
	}
	log := o.Logger
	if log == nil {
		log = slog.Default()
	}
	mp := o.Meter
	if mp == nil {
		mp = noop.NewMeterProvider()
	}
	events, err := mp.Meter(scope).Int64Counter(eventsMetric,
		metric.WithDescription("The installation and installation_repositories deliveries with a valid signature, by event and action."),
		metric.WithUnit("{delivery}"))
	if err != nil {
		return nil, err
	}
	return &handler{secret: secret, log: log, events: events}, nil
}

func (h *handler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	// Pomerium cannot limit the method on a public route, so the handler does.
	if r.Method != http.MethodPost {
		w.Header().Set("Allow", http.MethodPost)
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	sig, ok := strings.CutPrefix(r.Header.Get("X-Hub-Signature-256"), "sha256=")
	want, err := hex.DecodeString(sig)
	if !ok || err != nil || len(want) != sha256.Size {
		http.Error(w, "no valid signature", http.StatusUnauthorized)
		return
	}
	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, MaxBody))
	if err != nil {
		var tooLarge *http.MaxBytesError
		if errors.As(err, &tooLarge) {
			http.Error(w, "the delivery is too large", http.StatusRequestEntityTooLarge)
			return
		}
		http.Error(w, "the delivery could not be read", http.StatusBadRequest)
		return
	}
	// https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries
	mac := hmac.New(sha256.New, h.secret)
	mac.Write(body)
	if !hmac.Equal(mac.Sum(nil), want) {
		http.Error(w, "no valid signature", http.StatusUnauthorized)
		return
	}
	event := r.Header.Get("X-GitHub-Event")
	if event != "installation" && event != "installation_repositories" {
		w.WriteHeader(http.StatusNoContent)
		return
	}
	var p payload
	if err := json.Unmarshal(body, &p); err != nil {
		http.Error(w, "the delivery is not JSON", http.StatusBadRequest)
		return
	}
	attrs := []any{"event", event, "action", p.Action, "delivery", r.Header.Get("X-GitHub-Delivery"),
		"installation_id", p.Installation.ID}
	if event == "installation" {
		attrs = append(attrs, "account_id", p.Installation.Account.ID, "repositories", len(p.Repositories))
	} else {
		attrs = append(attrs, "repository_selection", p.RepositorySelection,
			"repositories_added", len(p.RepositoriesAdded), "added_ids", ids(p.RepositoriesAdded),
			"repositories_removed", len(p.RepositoriesRemoved), "removed_ids", ids(p.RepositoriesRemoved))
	}
	h.log.InfoContext(r.Context(), "github app installation event", attrs...)
	h.events.Add(r.Context(), 1, metric.WithAttributes(attribute.String("event", event), attribute.String("action", p.Action)))
	w.WriteHeader(http.StatusNoContent)
}

// payload is what is read of the two events; names and logins are left out.
// https://docs.github.com/en/webhooks/webhook-events-and-payloads#installation
type payload struct {
	Action       string `json:"action"`
	Installation struct {
		ID      int64 `json:"id"`
		Account struct {
			ID int64 `json:"id"`
		} `json:"account"`
	} `json:"installation"`
	Repositories        []repo `json:"repositories"`
	RepositorySelection string `json:"repository_selection"`
	RepositoriesAdded   []repo `json:"repositories_added"`
	RepositoriesRemoved []repo `json:"repositories_removed"`
}

type repo struct {
	ID int64 `json:"id"`
}

func ids(rs []repo) []int64 {
	out := make([]int64, len(rs))
	for i, r := range rs {
		out[i] = r.ID
	}
	return out
}
