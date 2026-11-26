package sbfleet

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"os"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

// The service-token exchange (README "The service-token exchange", #5494; spec
// 2026-11-05-service-credentials-without-db-design §6.2, §6.3). It mirrors the agent's
// ExchangeCredential (agent/src/api/serviceCredential.ts), with one addition: the Store
// resends a request once after a 401 with a renewed credential, which the agent does not.
const (
	exchangePath = "/api/v1/auth/service-token"
	grantType    = "urn:ietf:params:oauth:grant-type:token-exchange"
	// A failed exchange is tried again after 5 s, doubling to 60 s.
	exchangeRetryFirst = 5 * time.Second
	exchangeRetryMax   = 60 * time.Second
	// A refused credential exchanges again at once, but never sooner than this after the
	// last exchange, so a credential core keeps refusing cannot make a loop of exchanges.
	reexchangeMin   = 10 * time.Second
	exchangeTimeout = 10 * time.Second
)

// Proof is how the fleet proves who it is at the exchange.
type Proof string

const (
	// ProofKubernetes is a projected ServiceAccount token (ServiceAccount scadbuddy-fleet,
	// audience scadbuddy), read again before each exchange: the kubelet rotates it.
	ProofKubernetes Proof = "urn:ietf:params:oauth:token-type:jwt"
	// ProofBootstrap is a random secret of at least 32 characters (compose, a laptop),
	// whose hash the API holds as the file "fleet" in SCADBUDDY_SERVICE_BOOTSTRAP_DIR.
	ProofBootstrap Proof = "urn:scadbuddy:params:token-type:bootstrap"
)

var credentialPattern = regexp.MustCompile(`^sbsvc_[0-9a-f]{16}\.[A-Za-z0-9_-]{43}$`)

// Exchange holds the `fleet` service credential. It exchanges the proof at core's
// POST /api/v1/auth/service-token, renews at half-life, and exchanges again when a
// request is refused with the credential (Invalidate). Pass Token to NewStore and NewBus
// and Invalidate to them with OnUnauthorized. It never logs the proof or the credential.
type Exchange struct {
	url       string
	proofFile string
	proof     Proof
	http      *http.Client
	now       func() time.Time

	mu          sync.Mutex
	token       string
	expiresAt   time.Time
	renewAt     time.Time
	nextTry     time.Time
	exchangedAt time.Time
	delay       time.Duration
	lastErr     error
}

// NewExchange returns an Exchange of the proof in proofFile at coreURL. Nothing is sent
// until the first Token.
func NewExchange(coreURL, proofFile string, proof Proof) *Exchange {
	return &Exchange{
		url:       strings.TrimRight(coreURL, "/") + exchangePath,
		proofFile: proofFile,
		proof:     proof,
		http:      &http.Client{Timeout: exchangeTimeout},
		now:       time.Now,
		delay:     exchangeRetryFirst,
	}
}

// WrapTransport wraps the transport of the exchange's requests, as the Option of the same
// name does for NewStore and NewBus.
func (e *Exchange) WrapTransport(wrap func(http.RoundTripper) http.RoundTripper) {
	e.http.Transport = wrap(http.DefaultTransport)
}

// Token returns the credential, exchanging the proof first when none is held, at
// half-life, and after Invalidate. While a renewal fails, the credential it holds serves
// until it expires; with none, Token returns the last exchange's error.
func (e *Exchange) Token() (string, error) {
	e.mu.Lock()
	defer e.mu.Unlock()
	now := e.now()
	if e.token != "" && now.Before(e.renewAt) {
		return e.token, nil
	}
	if !now.Before(e.nextTry) {
		e.exchange()
	}
	if e.token != "" && e.now().Before(e.expiresAt) {
		return e.token, nil
	}
	if e.lastErr != nil {
		return "", e.lastErr
	}
	return "", errors.New("no fleet service credential yet")
}

// Invalidate drops token when it is the one held: core refused it (401). The next Token
// exchanges again, at once unless the last exchange was under reexchangeMin ago.
func (e *Exchange) Invalidate(token string) {
	e.mu.Lock()
	defer e.mu.Unlock()
	if token == "" || token != e.token {
		return
	}
	e.token = ""
	e.lastErr = errors.New("core refused the fleet service credential")
	e.nextTry = e.exchangedAt.Add(reexchangeMin)
	if now := e.now(); now.After(e.nextTry) {
		e.nextTry = now
	}
	slog.Info("sbfleet: core refused the fleet service credential; exchanging again")
}

// failed records a failed exchange and sets the back-off. reason never holds a secret.
func (e *Exchange) failed(reason string) {
	e.lastErr = fmt.Errorf("could not exchange the fleet service credential: %s", reason)
	slog.Warn("sbfleet: could not exchange the fleet service credential", "reason", reason, "retry_in", e.delay)
	e.nextTry = e.now().Add(e.delay)
	e.delay = min(2*e.delay, exchangeRetryMax)
}

// exchange runs one exchange, with e.mu held.
func (e *Exchange) exchange() {
	data, err := os.ReadFile(e.proofFile)
	if err != nil {
		e.failed("the proof file cannot be read: " + errorKind(err))
		return
	}
	proof := strings.TrimSpace(string(data))
	if proof == "" {
		e.failed("the proof file is empty")
		return
	}
	form := url.Values{
		"grant_type":         {grantType},
		"subject_token":      {proof},
		"subject_token_type": {string(e.proof)},
		// Never believed: the API refuses a proof of another caller sent with it.
		"client_id": {"fleet"},
	}
	res, err := e.http.PostForm(e.url, form)
	if errors.Is(err, syscall.ECONNREFUSED) {
		// The API is not up yet (core rolling): no back-off, the next call tries again,
		// as the agent's backendNotUp (#5868).
		e.lastErr = fmt.Errorf("could not exchange the fleet service credential: the API is not up yet")
		return
	}
	if err != nil {
		// The error names the URL and the network failure, never the form.
		e.failed(err.Error())
		return
	}
	defer res.Body.Close()
	now := e.now()
	switch res.StatusCode {
	case http.StatusOK:
		var body struct {
			AccessToken string  `json:"access_token"`
			ExpiresIn   float64 `json:"expires_in"`
		}
		if err := json.NewDecoder(io.LimitReader(res.Body, 64<<10)).Decode(&body); err != nil ||
			!credentialPattern.MatchString(body.AccessToken) || body.ExpiresIn <= 0 {
			e.failed("an answer without a credential")
			return
		}
		lifetime := time.Duration(body.ExpiresIn * float64(time.Second))
		e.token = body.AccessToken
		e.exchangedAt = now
		e.expiresAt = now.Add(lifetime)
		e.renewAt = now.Add(lifetime / 2)
		e.nextTry = e.renewAt
		e.delay = exchangeRetryFirst
		e.lastErr = nil
		slog.Info("sbfleet: exchanged the fleet service credential", "expires_in", lifetime)
	case http.StatusTooManyRequests:
		_, _ = io.Copy(io.Discard, io.LimitReader(res.Body, 64<<10))
		wait := e.delay
		if s, err := strconv.Atoi(strings.TrimSpace(res.Header.Get("Retry-After"))); err == nil && s >= 1 {
			wait = time.Duration(s) * time.Second
		}
		e.lastErr = errors.New("could not exchange the fleet service credential: rate limited")
		slog.Warn("sbfleet: the fleet service-token exchange was rate limited", "retry_in", wait)
		e.nextTry = now.Add(wait)
	default:
		_, _ = io.Copy(io.Discard, io.LimitReader(res.Body, 64<<10))
		e.failed("HTTP " + strconv.Itoa(res.StatusCode))
	}
}

// errorKind names a file error without its path's content.
func errorKind(err error) string {
	switch {
	case errors.Is(err, os.ErrNotExist):
		return "not found"
	case errors.Is(err, os.ErrPermission):
		return "permission denied"
	}
	return "read error"
}
