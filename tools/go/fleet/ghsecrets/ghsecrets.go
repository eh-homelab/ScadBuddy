// Package ghsecrets writes GitHub Actions secrets as a GitHub App (#8012). Each write
// mints an installation token scoped down to that one write, reads the target's public
// key, seals the value in a libsodium sealed box and puts it. It also reads a secret's
// updated_at, because GitHub sends no webhook when a secret changes.
//
// It never caches, logs or returns an installation token, the App's JWT, the private key,
// the value or the sealed value. It imports nothing else from this repository, so fleetd
// can use it too. The calls are in README.md ("Package ghsecrets").
package ghsecrets

import (
	"bytes"
	"context"
	"crypto"
	"crypto/rand"
	"crypto/rsa"
	"crypto/sha256"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"

	"golang.org/x/crypto/nacl/box"
)

// DefaultBaseURL is GitHub's REST API.
const DefaultBaseURL = "https://api.github.com"

const (
	apiVersion     = "2022-11-28"
	requestTimeout = 30 * time.Second
	// The JWT starts 60 s in the past against clock drift and lasts 9 minutes; GitHub
	// refuses one that lasts more than 10.
	jwtBackdate = 60 * time.Second
	jwtLifetime = 9 * time.Minute
	// maxAnswer caps what is read of an answer from GitHub.
	maxAnswer = 1 << 20
)

// secretName is GitHub's rule for a secret's name: letters, digits and underscores, not
// first a digit, and not GITHUB_ first.
var secretName = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_]*$`)

// Config is the App the writer acts as.
type Config struct {
	AppID          int64
	InstallationID int64
	PrivateKey     *rsa.PrivateKey
	// BaseURL is DefaultBaseURL when empty.
	BaseURL string
	// Transport is http.DefaultTransport when nil.
	Transport http.RoundTripper
	// WrapTransport, when set, wraps the transport (such as telemetry's, for a CLIENT
	// span). The writer strips trace context from every request after the wrap, so no
	// traceparent, tracestate or baggage reaches GitHub.
	WrapTransport func(http.RoundTripper) http.RoundTripper
	// Logger is slog.Default() when nil.
	Logger *slog.Logger
	// Now is time.Now when nil.
	Now func() time.Time
}

// Target is one secret: a repository's (Repo set) or an organisation's (Repo empty).
type Target struct {
	Owner string
	Repo  string
	Name  string
	// Visibility is for an organisation secret only: "all", "private" or "selected"
	// ("private" when empty).
	Visibility string
	// SelectedRepositoryIDs are the repositories that may read a "selected" secret.
	SelectedRepositoryIDs []int64
}

func (t Target) org() bool { return t.Repo == "" }

func (t Target) String() string {
	if t.org() {
		return "org " + t.Owner + " secret " + t.Name
	}
	return "repo " + t.Owner + "/" + t.Repo + " secret " + t.Name
}

func (t Target) check() error {
	if t.Owner == "" {
		return errors.New("ghsecrets: the target has no owner")
	}
	if !secretName.MatchString(t.Name) || strings.HasPrefix(strings.ToUpper(t.Name), "GITHUB_") {
		return fmt.Errorf("ghsecrets: %q is not a valid secret name", t.Name)
	}
	if !t.org() && (t.Visibility != "" || len(t.SelectedRepositoryIDs) > 0) {
		return errors.New("ghsecrets: visibility and selected repositories are for an organisation secret only")
	}
	switch t.Visibility {
	case "", "all", "private":
		if len(t.SelectedRepositoryIDs) > 0 {
			return errors.New(`ghsecrets: selected repositories need the visibility "selected"`)
		}
	case "selected":
	default:
		return fmt.Errorf("ghsecrets: %q is not a visibility (all, private or selected)", t.Visibility)
	}
	return nil
}

// base is the secret's path under the API: /repos/{o}/{r}/actions/secrets or
// /orgs/{o}/actions/secrets.
func (t Target) base() string {
	if t.org() {
		return "/orgs/" + url.PathEscape(t.Owner) + "/actions/secrets"
	}
	return "/repos/" + url.PathEscape(t.Owner) + "/" + url.PathEscape(t.Repo) + "/actions/secrets"
}

// Writer writes secrets as the App. It is safe for concurrent use.
type Writer struct {
	cfg  Config
	http *http.Client
	log  *slog.Logger
}

// New returns a Writer. Nothing is sent until the first call.
func New(cfg Config) (*Writer, error) {
	if cfg.AppID <= 0 || cfg.InstallationID <= 0 {
		return nil, errors.New("ghsecrets: the App id and the installation id must be more than 0")
	}
	if cfg.PrivateKey == nil {
		return nil, errors.New("ghsecrets: no private key")
	}
	if cfg.BaseURL == "" {
		cfg.BaseURL = DefaultBaseURL
	}
	cfg.BaseURL = strings.TrimRight(cfg.BaseURL, "/")
	if cfg.Transport == nil {
		cfg.Transport = http.DefaultTransport
	}
	var transport http.RoundTripper = noTraceContext{cfg.Transport}
	if cfg.WrapTransport != nil {
		transport = cfg.WrapTransport(transport)
	}
	if cfg.Now == nil {
		cfg.Now = time.Now
	}
	log := cfg.Logger
	if log == nil {
		log = slog.Default()
	}
	return &Writer{
		cfg: cfg,
		// No redirects: a token must never follow one to another host.
		http: &http.Client{Timeout: requestTimeout, Transport: transport,
			CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }},
		log: log,
	}, nil
}

// noTraceContext sends each request with no trace context: GitHub is not ours, so it gets
// no traceparent, tracestate or baggage (the W3C headers telemetry's propagator writes).
type noTraceContext struct{ base http.RoundTripper }

func (n noTraceContext) RoundTrip(r *http.Request) (*http.Response, error) {
	r = r.Clone(r.Context())
	for _, h := range []string{"Traceparent", "Tracestate", "Baggage"} {
		r.Header.Del(h)
	}
	return n.base.RoundTrip(r)
}

// ParsePrivateKey reads the App's private key: PEM, PKCS #1 (what GitHub gives) or
// PKCS #8.
func ParsePrivateKey(data []byte) (*rsa.PrivateKey, error) {
	block, _ := pem.Decode(data)
	if block == nil {
		return nil, errors.New("ghsecrets: the private key is not PEM")
	}
	if k, err := x509.ParsePKCS1PrivateKey(block.Bytes); err == nil {
		return k, nil
	}
	k, err := x509.ParsePKCS8PrivateKey(block.Bytes)
	if err != nil {
		return nil, errors.New("ghsecrets: the private key is neither PKCS #1 nor PKCS #8 RSA")
	}
	rk, ok := k.(*rsa.PrivateKey)
	if !ok {
		return nil, errors.New("ghsecrets: the private key is not RSA")
	}
	return rk, nil
}

// Set seals value with the target's public key and writes it as the target's secret.
func (w *Writer) Set(ctx context.Context, t Target, value []byte) error {
	if err := t.check(); err != nil {
		return err
	}
	token, err := w.installationToken(ctx, t, "write")
	if err != nil {
		return err
	}
	var key struct {
		KeyID string `json:"key_id"`
		Key   string `json:"key"`
	}
	if err := w.do(ctx, token, http.MethodGet, t.base()+"/public-key", nil, &key, http.StatusOK); err != nil {
		return err
	}
	raw, err := base64.StdEncoding.DecodeString(key.Key)
	if err != nil || len(raw) != 32 || key.KeyID == "" {
		return fmt.Errorf("ghsecrets: %s: GitHub's public key is not a 32-byte key with an id", t)
	}
	var pub [32]byte
	copy(pub[:], raw)
	sealed, err := box.SealAnonymous(nil, value, &pub, rand.Reader)
	if err != nil {
		return fmt.Errorf("ghsecrets: %s: seal: %w", t, err)
	}
	body := map[string]any{
		"encrypted_value": base64.StdEncoding.EncodeToString(sealed),
		"key_id":          key.KeyID,
	}
	if t.org() {
		vis := t.Visibility
		if vis == "" {
			vis = "private"
		}
		body["visibility"] = vis
		if vis == "selected" {
			ids := t.SelectedRepositoryIDs
			if ids == nil {
				ids = []int64{}
			}
			body["selected_repository_ids"] = ids
		}
	}
	if err := w.do(ctx, token, http.MethodPut, t.base()+"/"+url.PathEscape(t.Name), body, nil, http.StatusCreated, http.StatusNoContent); err != nil {
		return err
	}
	w.log.InfoContext(ctx, "github secret set", "owner", t.Owner, "repo", t.Repo, "name", t.Name, "key_id", key.KeyID)
	return nil
}

// ErrNotFound is the answer for a secret (or a target) that does not exist.
var ErrNotFound = errors.New("ghsecrets: not found")

// UpdatedAt reads when the target's secret was last written. A secret that does not exist
// is ErrNotFound.
func (w *Writer) UpdatedAt(ctx context.Context, t Target) (time.Time, error) {
	if err := t.check(); err != nil {
		return time.Time{}, err
	}
	token, err := w.installationToken(ctx, t, "read")
	if err != nil {
		return time.Time{}, err
	}
	var s struct {
		UpdatedAt time.Time `json:"updated_at"`
	}
	if err := w.do(ctx, token, http.MethodGet, t.base()+"/"+url.PathEscape(t.Name), nil, &s, http.StatusOK); err != nil {
		return time.Time{}, err
	}
	if s.UpdatedAt.IsZero() {
		return time.Time{}, fmt.Errorf("ghsecrets: %s: GitHub's answer has no updated_at", t)
	}
	return s.UpdatedAt, nil
}

// installationToken mints a token for this one call: only the secrets permission the
// target needs, at level ("read" or "write"), and for a repository target only that
// repository. An organisation permission grants no repository's contents, so an
// organisation target's token names no repository.
// https://docs.github.com/en/rest/apps/apps#create-an-installation-access-token-for-an-app
func (w *Writer) installationToken(ctx context.Context, t Target, level string) (string, error) {
	jwt, err := w.appJWT()
	if err != nil {
		return "", err
	}
	body := map[string]any{}
	if t.org() {
		body["permissions"] = map[string]string{"organization_secrets": level}
	} else {
		body["permissions"] = map[string]string{"secrets": level}
		body["repositories"] = []string{t.Repo}
	}
	var answer struct {
		Token        string            `json:"token"`
		Permissions  map[string]string `json:"permissions"`
		Repositories []struct {
			Name string `json:"name"`
		} `json:"repositories"`
	}
	path := "/app/installations/" + strconv.FormatInt(w.cfg.InstallationID, 10) + "/access_tokens"
	if err := w.do(ctx, jwt, http.MethodPost, path, body, &answer, http.StatusCreated); err != nil {
		return "", err
	}
	if answer.Token == "" {
		return "", errors.New("ghsecrets: GitHub's token answer has no token")
	}
	// Refuse a token that holds more than was asked (GitHub can add metadata at read).
	asked := "secrets"
	if t.org() {
		asked = "organization_secrets"
	}
	for p, l := range answer.Permissions {
		switch {
		case p == "metadata" && l == "read":
		case p == asked && l == level:
		default:
			return "", fmt.Errorf("ghsecrets: GitHub's token holds the permission %q at %q, which was not asked for", p, l)
		}
	}
	if !t.org() && (len(answer.Repositories) != 1 || !strings.EqualFold(answer.Repositories[0].Name, t.Repo)) {
		return "", fmt.Errorf("ghsecrets: GitHub's token names %d repositories, not only the one asked for", len(answer.Repositories))
	}
	return answer.Token, nil
}

// appJWT is the App's JWT: RS256 over iat, exp and iss.
// https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-json-web-token-jwt-for-a-github-app
func (w *Writer) appJWT() (string, error) {
	now := w.cfg.Now()
	header := base64.RawURLEncoding.EncodeToString([]byte(`{"alg":"RS256","typ":"JWT"}`))
	claims, err := json.Marshal(map[string]any{
		"iat": now.Add(-jwtBackdate).Unix(),
		"exp": now.Add(jwtLifetime).Unix(),
		"iss": strconv.FormatInt(w.cfg.AppID, 10),
	})
	if err != nil {
		return "", err
	}
	signed := header + "." + base64.RawURLEncoding.EncodeToString(claims)
	sum := sha256.Sum256([]byte(signed))
	sig, err := rsa.SignPKCS1v15(rand.Reader, w.cfg.PrivateKey, crypto.SHA256, sum[:])
	if err != nil {
		return "", fmt.Errorf("ghsecrets: sign the App's JWT: %w", err)
	}
	return signed + "." + base64.RawURLEncoding.EncodeToString(sig), nil
}

// do sends one request with bearer and reads a JSON answer into out (nil: none). An error
// names the method, the path and the status, and GitHub's message; never a header or a
// body that was sent.
func (w *Writer) do(ctx context.Context, bearer, method, path string, in, out any, ok ...int) error {
	var body io.Reader
	if in != nil {
		data, err := json.Marshal(in)
		if err != nil {
			return err
		}
		body = bytes.NewReader(data)
	}
	req, err := http.NewRequestWithContext(ctx, method, w.cfg.BaseURL+path, body)
	if err != nil {
		return fmt.Errorf("ghsecrets: %s %s: %w", method, path, err)
	}
	req.Header.Set("Authorization", "Bearer "+bearer)
	req.Header.Set("Accept", "application/vnd.github+json")
	req.Header.Set("X-GitHub-Api-Version", apiVersion)
	if in != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := w.http.Do(req)
	if err != nil {
		// A *url.Error names the URL only; the request's headers stay out of it.
		return fmt.Errorf("ghsecrets: %w", err)
	}
	defer func() { _ = resp.Body.Close() }()
	data, err := io.ReadAll(io.LimitReader(resp.Body, maxAnswer))
	if err != nil {
		return fmt.Errorf("ghsecrets: %s %s: read the answer: %w", method, path, err)
	}
	for _, code := range ok {
		if resp.StatusCode == code {
			if out == nil {
				return nil
			}
			if err := json.Unmarshal(data, out); err != nil {
				return fmt.Errorf("ghsecrets: %s %s: the answer is not the JSON expected", method, path)
			}
			return nil
		}
	}
	var problem struct {
		Message string `json:"message"`
	}
	_ = json.Unmarshal(data, &problem)
	msg := problem.Message
	if len(msg) > 200 {
		// Cut at a rune's start, so no UTF-8 rune is split.
		i := 200
		for i > 0 && !utf8.RuneStart(msg[i]) {
			i--
		}
		msg = msg[:i]
	}
	err = fmt.Errorf("ghsecrets: %s %s: GitHub answered %d %q", method, path, resp.StatusCode, msg)
	if resp.StatusCode == http.StatusNotFound {
		return errors.Join(ErrNotFound, err)
	}
	return err
}
