// Package update makes fleetd replace itself with the newest signed release (#8588).
//
// fleetd-release.yml publishes each build of main as the GitHub release `fleetd-<sha7>`,
// with one binary per platform, SHA256SUMS and SHA256SUMS.sig (an ed25519 signature of
// SHA256SUMS, base64). The updater takes the newest `fleetd-` release, asks GitHub's
// compare API whether its commit is ahead of the running one, verifies the signature with
// the key compiled in here, then the binary's sha256, runs `<new> version`, keeps the
// running binary as `<name>.old` and moves the new one into its place.
package update

import (
	"bufio"
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"
)

// publicKey is the release signing key's public half: raw 32 bytes, base64. Its private
// half is the repo secret FLEETD_RELEASE_SIGNING_KEY; fleetd-release.yml checks the two
// match before it signs.
const publicKey = "XLVoFjOOmaMxLpAg41zfmZ661BVZkwb3MSOMTGXP0h0="

const (
	// DefaultAPI and DefaultRepo are where releases are published.
	DefaultAPI  = "https://api.github.com"
	DefaultRepo = "eh-homelab/ScadBuddy"
	tagPrefix   = "fleetd-"
	sumsName    = "SHA256SUMS"
	sigName     = "SHA256SUMS.sig"

	maxJSON   = 32 << 20 // a compare answer lists up to 300 files with their patches
	maxSums   = 64 << 10
	maxSig    = 1 << 10
	maxBinary = 256 << 20

	smokeTimeout = 10 * time.Second
	retries      = 3
)

var (
	// ErrDevBuild: the running fleetd has no release version, so it never updates.
	ErrDevBuild = errors.New("not a release build (version is not a commit sha); auto-update is off")
	// ErrSignature: SHA256SUMS.sig is not a valid signature of SHA256SUMS by the release key.
	ErrSignature = errors.New("SHA256SUMS signature does not verify")
	// ErrChecksum: the binary's sha256 is not the signed one.
	ErrChecksum = errors.New("binary checksum does not match SHA256SUMS")
	// ErrSmokeTest: `<new> version` failed or printed another version.
	ErrSmokeTest = errors.New("new binary failed its smoke test")

	shaRE = regexp.MustCompile(`^[0-9a-f]{40}$`)
)

// PublicKey is the release signing key fleetd trusts.
func PublicKey() ed25519.PublicKey {
	b, err := base64.StdEncoding.DecodeString(publicKey)
	if err != nil || len(b) != ed25519.PublicKeySize {
		panic("update: bad compiled-in public key")
	}
	return ed25519.PublicKey(b)
}

// IsRelease reports whether v is a release version: a full commit sha.
func IsRelease(v string) bool { return shaRE.MatchString(v) }

// DefaultAllowHost names the hosts a release download may reach, redirects included.
func DefaultAllowHost(host string) bool {
	switch host {
	case "github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com":
		return true
	}
	return false
}

// Config is the updater's settings. Zero fields take the defaults.
type Config struct {
	Current string // the running version
	Exe     string // the running binary's path, symlinks resolved, read before any swap
	API     string
	Repo    string // owner/name
	// PublicKey defaults to the compiled-in key.
	PublicKey ed25519.PublicKey
	// HTTP must not add trace headers; the default is a plain client.
	HTTP *http.Client
	// AllowHost decides which hosts asset downloads (and their redirects) may reach.
	AllowHost func(host string) bool
	// RunVersion runs `<path> version` and returns its stdout.
	RunVersion func(ctx context.Context, path string) (string, error)
	GOOS       string
	GOARCH     string
	// Backoff is the first wait between retries of a transient failure; it doubles.
	Backoff time.Duration
	Log     *slog.Logger
	Now     func() time.Time
}

// Updater checks for and installs new releases. It is not safe for concurrent use.
type Updater struct {
	cfg        Config
	notBefore  time.Time // GitHub's rate limit resets then
	lastLogged string
}

// New returns an updater, or ErrDevBuild when cfg.Current is not a release version.
func New(cfg Config) (*Updater, error) {
	if !IsRelease(cfg.Current) {
		return nil, ErrDevBuild
	}
	if cfg.Exe == "" {
		return nil, errors.New("update: no executable path")
	}
	if cfg.API == "" {
		cfg.API = DefaultAPI
	}
	if cfg.Repo == "" {
		cfg.Repo = DefaultRepo
	}
	if cfg.PublicKey == nil {
		cfg.PublicKey = PublicKey()
	}
	if cfg.AllowHost == nil {
		cfg.AllowHost = DefaultAllowHost
	}
	if cfg.HTTP == nil {
		cfg.HTTP = &http.Client{Transport: http.DefaultTransport.(*http.Transport).Clone()}
	}
	// Every redirect is held to the same hosts, over https.
	client := *cfg.HTTP
	allow := cfg.AllowHost
	api, err := url.Parse(cfg.API)
	if err != nil {
		return nil, err
	}
	client.CheckRedirect = func(req *http.Request, via []*http.Request) error {
		if len(via) >= 5 {
			return errors.New("too many redirects")
		}
		if req.URL.Scheme != "https" || (!allow(req.URL.Hostname()) && req.URL.Host != api.Host) {
			return fmt.Errorf("redirect to %s is not allowed", req.URL.Host)
		}
		return nil
	}
	cfg.HTTP = &client
	if cfg.RunVersion == nil {
		cfg.RunVersion = runVersion
	}
	if cfg.Backoff == 0 {
		cfg.Backoff = 2 * time.Second
	}
	if cfg.Log == nil {
		cfg.Log = slog.Default()
	}
	if cfg.Now == nil {
		cfg.Now = time.Now
	}
	return &Updater{cfg: cfg}, nil
}

func runVersion(ctx context.Context, path string) (string, error) {
	out, err := exec.CommandContext(ctx, path, "version").Output()
	return string(out), err
}

// Loop checks at once and then every interval until ctx ends or a new binary is in
// place. It returns true when the caller should re-exec. A failure is logged once per
// release and reason; the next tick tries again.
func (u *Updater) Loop(ctx context.Context, interval time.Duration) bool {
	t := time.NewTimer(0)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return false
		case <-t.C:
		}
		done, err := u.Check(ctx)
		if done {
			return true
		}
		if err != nil && ctx.Err() == nil {
			u.once(slog.LevelWarn, "fleetd: auto-update failed; keeping the running binary", err.Error())
		}
		t.Reset(interval)
	}
}

// once logs msg unless the last line logged had the same key.
func (u *Updater) once(level slog.Level, msg, key string, attrs ...any) {
	if key == u.lastLogged {
		return
	}
	u.lastLogged = key
	u.cfg.Log.Log(context.Background(), level, msg, append([]any{"reason", key}, attrs...)...)
}

type release struct {
	Tag       string    `json:"tag_name"`
	Target    string    `json:"target_commitish"`
	CreatedAt time.Time `json:"created_at"`
	Draft     bool      `json:"draft"`
	Pre       bool      `json:"prerelease"`
	Assets    []struct {
		Name string `json:"name"`
		URL  string `json:"browser_download_url"`
	} `json:"assets"`
}

func (r release) asset(name string) (string, error) {
	for _, a := range r.Assets {
		if a.Name == name {
			return a.URL, nil
		}
	}
	return "", fmt.Errorf("release %s has no asset %s", r.Tag, name)
}

// Check looks once for a newer release and installs it. It returns true when the new
// binary is in place at cfg.Exe.
func (u *Updater) Check(ctx context.Context) (bool, error) {
	if now := u.cfg.Now(); now.Before(u.notBefore) {
		return false, nil
	}
	rel, err := u.latest(ctx)
	if err != nil || rel == nil {
		return false, err
	}
	if rel.Target == u.cfg.Current {
		return false, nil
	}
	ahead, err := u.ahead(ctx, rel.Target)
	if err != nil {
		return false, fmt.Errorf("%s: %w", rel.Tag, err)
	}
	if !ahead {
		u.once(slog.LevelInfo, "fleetd: the newest release is not ahead of this one; staying", rel.Tag+": not ahead")
		return false, nil
	}
	if err := u.install(ctx, *rel); err != nil {
		return false, fmt.Errorf("%s: %w", rel.Tag, err)
	}
	u.cfg.Log.Info("fleetd: installed a new release; restarting", "release", rel.Tag)
	return true, nil
}

// latest is the newest published `fleetd-` release by created_at, or nil.
func (u *Updater) latest(ctx context.Context) (*release, error) {
	body, err := u.get(ctx, u.cfg.API+"/repos/"+u.cfg.Repo+"/releases?per_page=100", true, maxJSON)
	if err != nil {
		return nil, fmt.Errorf("listing releases: %w", err)
	}
	var list []release
	if err := json.Unmarshal(body, &list); err != nil {
		return nil, fmt.Errorf("listing releases: %w", err)
	}
	var best *release
	for i := range list {
		r := &list[i]
		if r.Draft || r.Pre || !strings.HasPrefix(r.Tag, tagPrefix) || !IsRelease(r.Target) || r.Tag != tagPrefix+r.Target[:7] {
			continue
		}
		if best == nil || r.CreatedAt.After(best.CreatedAt) {
			best = r
		}
	}
	return best, nil
}

// ahead asks GitHub whether target is a descendant of the running commit. A release cut
// from an older commit, or from another branch, is never taken.
func (u *Updater) ahead(ctx context.Context, target string) (bool, error) {
	body, err := u.get(ctx, u.cfg.API+"/repos/"+u.cfg.Repo+"/compare/"+u.cfg.Current+"..."+target+"?per_page=1", true, maxJSON)
	if err != nil {
		return false, fmt.Errorf("comparing commits: %w", err)
	}
	var cmp struct {
		Status string `json:"status"`
	}
	if err := json.Unmarshal(body, &cmp); err != nil {
		return false, fmt.Errorf("comparing commits: %w", err)
	}
	return cmp.Status == "ahead", nil
}

func (u *Updater) install(ctx context.Context, rel release) error {
	name := "fleetd-" + u.cfg.GOOS + "-" + u.cfg.GOARCH
	sumsURL, err := rel.asset(sumsName)
	if err != nil {
		return err
	}
	sigURL, err := rel.asset(sigName)
	if err != nil {
		return err
	}
	binURL, err := rel.asset(name)
	if err != nil {
		return err
	}
	sums, err := u.get(ctx, sumsURL, false, maxSums)
	if err != nil {
		return fmt.Errorf("downloading %s: %w", sumsName, err)
	}
	sigText, err := u.get(ctx, sigURL, false, maxSig)
	if err != nil {
		return fmt.Errorf("downloading %s: %w", sigName, err)
	}
	sig, err := base64.StdEncoding.DecodeString(strings.TrimSpace(string(sigText)))
	if err != nil || len(sig) != ed25519.SignatureSize || !ed25519.Verify(u.cfg.PublicKey, sums, sig) {
		return ErrSignature
	}
	want, err := lookupSum(sums, name)
	if err != nil {
		return err
	}

	dir, base := filepath.Split(u.cfg.Exe)
	tmp, err := os.CreateTemp(dir, "."+base+".new-*")
	if err != nil {
		return fmt.Errorf("writing the new binary: %w", err)
	}
	keep := false
	defer func() {
		if !keep {
			_ = tmp.Close()
			_ = os.Remove(tmp.Name())
		}
	}()
	if err := u.download(ctx, binURL, tmp, want); err != nil {
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	if err := os.Chmod(tmp.Name(), 0o755); err != nil {
		return err
	}

	sctx, cancel := context.WithTimeout(ctx, smokeTimeout)
	out, err := u.cfg.RunVersion(sctx, tmp.Name())
	cancel()
	if err != nil {
		return fmt.Errorf("%w: %v", ErrSmokeTest, err)
	}
	if got := strings.TrimSpace(out); got != rel.Target {
		return fmt.Errorf("%w: it reports version %q", ErrSmokeTest, got)
	}

	old := u.cfg.Exe + ".old"
	if err := os.Rename(u.cfg.Exe, old); err != nil {
		return fmt.Errorf("keeping the running binary as .old: %w", err)
	}
	if err := os.Rename(tmp.Name(), u.cfg.Exe); err != nil {
		if rerr := os.Rename(old, u.cfg.Exe); rerr != nil {
			return fmt.Errorf("moving the new binary in: %v; putting the old one back: %w", err, rerr)
		}
		return fmt.Errorf("moving the new binary in: %w", err)
	}
	keep = true
	return nil
}

// lookupSum finds name's sha256 in a sha256sum listing.
func lookupSum(sums []byte, name string) ([]byte, error) {
	sc := bufio.NewScanner(bytes.NewReader(sums))
	for sc.Scan() {
		f := strings.Fields(sc.Text())
		if len(f) == 2 && strings.TrimPrefix(f[1], "*") == name {
			b, err := hex.DecodeString(f[0])
			if err != nil || len(b) != sha256.Size {
				return nil, fmt.Errorf("%s: bad checksum line for %s", sumsName, name)
			}
			return b, nil
		}
	}
	return nil, fmt.Errorf("%s lists no %s", sumsName, name)
}

// download streams url into w and checks its sha256.
func (u *Updater) download(ctx context.Context, rawURL string, w io.Writer, want []byte) error {
	resp, err := u.open(ctx, rawURL, false)
	if err != nil {
		return fmt.Errorf("downloading the binary: %w", err)
	}
	defer resp.Body.Close()
	h := sha256.New()
	n, err := io.Copy(io.MultiWriter(w, h), io.LimitReader(resp.Body, maxBinary+1))
	if err != nil {
		return fmt.Errorf("downloading the binary: %w", err)
	}
	if n > maxBinary {
		return errors.New("the binary is too large")
	}
	if !bytes.Equal(h.Sum(nil), want) {
		return ErrChecksum
	}
	return nil
}

func (u *Updater) get(ctx context.Context, rawURL string, api bool, limit int64) ([]byte, error) {
	resp, err := u.open(ctx, rawURL, api)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	b, err := io.ReadAll(io.LimitReader(resp.Body, limit+1))
	if err != nil {
		return nil, err
	}
	if int64(len(b)) > limit {
		return nil, errors.New("answer too large")
	}
	return b, nil
}

// rateLimited is GitHub's rate limit; the updater waits until it resets.
type rateLimited struct{ until time.Time }

func (e rateLimited) Error() string {
	return "GitHub rate limit; waiting until " + e.until.UTC().Format(time.RFC3339)
}

// open sends a GET and returns a 200 answer. Network errors and 5xx retry with backoff;
// a rate limit stops every call until it resets.
func (u *Updater) open(ctx context.Context, rawURL string, api bool) (*http.Response, error) {
	target, err := url.Parse(rawURL)
	if err != nil {
		return nil, err
	}
	if target.Scheme != "https" {
		return nil, fmt.Errorf("%s is not https", target.Host)
	}
	if !api && !u.cfg.AllowHost(target.Hostname()) {
		return nil, fmt.Errorf("download from %s is not allowed", target.Host)
	}
	wait := u.cfg.Backoff
	var last error
	for attempt := 0; attempt < retries; attempt++ {
		if attempt > 0 {
			select {
			case <-ctx.Done():
				return nil, ctx.Err()
			case <-time.After(wait):
			}
			wait *= 2
		}
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, rawURL, nil)
		if err != nil {
			return nil, err
		}
		req.Header.Set("User-Agent", "fleetd/"+u.cfg.Current[:7])
		if api {
			req.Header.Set("Accept", "application/vnd.github+json")
			req.Header.Set("X-GitHub-Api-Version", "2022-11-28")
		} else {
			req.Header.Set("Accept", "application/octet-stream")
		}
		resp, err := u.cfg.HTTP.Do(req)
		if err != nil {
			if ctx.Err() != nil {
				return nil, ctx.Err()
			}
			var uerr *url.Error
			if errors.As(err, &uerr) && strings.Contains(uerr.Err.Error(), "not allowed") {
				return nil, err
			}
			last = err
			continue
		}
		if resp.StatusCode == http.StatusOK {
			return resp, nil
		}
		_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 64<<10))
		resp.Body.Close()
		if until, ok := u.limitedUntil(resp); ok {
			u.notBefore = until
			return nil, rateLimited{until}
		}
		last = fmt.Errorf("%s answered %s", target.Host, resp.Status)
		if resp.StatusCode < 500 {
			return nil, last
		}
	}
	return nil, last
}

// limitedUntil reads GitHub's rate-limit answer: Retry-After, else X-RateLimit-Reset
// when X-RateLimit-Remaining is 0.
func (u *Updater) limitedUntil(resp *http.Response) (time.Time, bool) {
	if resp.StatusCode != http.StatusForbidden && resp.StatusCode != http.StatusTooManyRequests {
		return time.Time{}, false
	}
	if s, err := strconv.Atoi(resp.Header.Get("Retry-After")); err == nil && s >= 0 {
		return u.cfg.Now().Add(time.Duration(s) * time.Second), true
	}
	if resp.Header.Get("X-RateLimit-Remaining") == "0" {
		if s, err := strconv.ParseInt(resp.Header.Get("X-RateLimit-Reset"), 10, 64); err == nil {
			return time.Unix(s, 0), true
		}
		return u.cfg.Now().Add(time.Hour), true
	}
	return time.Time{}, false
}
