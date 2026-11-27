package devices

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"errors"
	"net/http"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
)

// codePattern is a pairing code: 16 random bytes, base64url without padding.
var codePattern = regexp.MustCompile(`^[A-Za-z0-9_-]{22}$`)

// secret is n random bytes, base64url without padding, and its SHA-256.
func secret(n int) (string, []byte) {
	b := make([]byte, n)
	_, _ = rand.Read(b)
	text := base64.RawURLEncoding.EncodeToString(b)
	return text, hash(text)
}

func hash(text string) []byte {
	sum := sha256.Sum256([]byte(text))
	return sum[:]
}

// Pairing is the answer to POST /api/v1/devices/pairings.
type Pairing struct {
	CodeURL   string    `json:"code_url"`
	ExpiresAt crud.Time `json:"expires_at"`
}

// pairing makes a one-time code for the caller. Its URL names the host the person is on, so
// a phone paired on the SSO host works away from home.
func (s *Store) pairing(r *http.Request) (any, error) {
	code, codeHash := secret(16)
	now := s.now()
	expires := now.Add(CodeTTL)
	if _, err := s.Pool.Exec(r.Context(), `INSERT INTO device_pairings (code_hash, owner, created_at, expires_at)
		VALUES ($1, $2, $3, $4)`, codeHash, principal(r.Context()), now, expires); err != nil {
		return nil, err
	}
	return Pairing{CodeURL: origin(r) + "/api/v1/devices/pairings/" + code + "/claim", ExpiresAt: crud.Time(expires)}, nil
}

// origin is the request's own origin: https, but plain http for a loopback host (a dev server).
func origin(r *http.Request) string {
	host := r.Host
	name := host
	if i := strings.LastIndex(host, ":"); i >= 0 && !strings.HasSuffix(host, "]") {
		name = host[:i]
	}
	if name == "localhost" || name == "127.0.0.1" || name == "[::1]" {
		return "http://" + host
	}
	return "https://" + host
}

// Claimed is the answer to a claim. The token is shown once: only its hash is stored.
type Claimed struct {
	DeviceID  string    `json:"device_id"`
	Token     string    `json:"token"`
	ExpiresAt crud.Time `json:"expires_at"`
	HardCapAt crud.Time `json:"hard_cap_at"`
}

// claim spends a pairing code: the app's POST to the code's URL. The code is the credential:
// an unknown code is a 401, one spent or expired a 410, and neither makes a device.
func (s *Store) claim(r *http.Request) (any, error) {
	code := r.PathValue("code")
	if !codePattern.MatchString(code) {
		return nil, unauthorized(ErrUnauthorized)
	}
	b, err := crud.ReadBody(r, "name", "model", "app_version", "capabilities")
	if err != nil {
		return nil, err
	}
	name := b.Text("name", 1, 100)
	model := b.TextOr("model", "", 0, 100)
	version := b.TextOr("app_version", "", 0, 50)
	capabilities := b.Strings("capabilities", false, 1, len(Methods), 1, 40)
	for i, c := range capabilities {
		if !slices.Contains(Methods, c) {
			b.Fail([]string{"capabilities", strconv.Itoa(i)}, "not one of "+strings.Join(Methods, ", "))
		}
	}
	if err := b.Err(); err != nil {
		return nil, err
	}
	slices.Sort(capabilities)
	capabilities = slices.Compact(capabilities)
	raw, _ := secret(32)
	token := TokenPrefix + raw
	tokenHash := hash(token)
	now := s.now()
	out := Claimed{Token: token, ExpiresAt: crud.Time(minTime(now.Add(s.Idle), now.Add(s.HardCap))),
		HardCapAt: crud.Time(now.Add(s.HardCap))}
	gone := httpx.WithReason(http.StatusGone, "That pairing code was used already, or it expired. Make a new one.", "code_gone")
	err = pgx.BeginFunc(r.Context(), s.Pool, func(tx pgx.Tx) error {
		var owner string
		var expires time.Time
		var spent *time.Time
		err := tx.QueryRow(r.Context(), `SELECT owner, expires_at, spent_at FROM device_pairings
			WHERE code_hash = $1 FOR UPDATE`, hash(code)).Scan(&owner, &expires, &spent)
		if errors.Is(err, pgx.ErrNoRows) {
			return unauthorized(ErrUnauthorized)
		}
		if err != nil {
			return err
		}
		if spent != nil || !now.Before(expires) {
			return gone
		}
		if err := tx.QueryRow(r.Context(), `INSERT INTO devices (owner, name, model, app_version, capabilities,
			created_at, last_seen_at) VALUES ($1, $2, $3, $4, $5, $6, $6) RETURNING id::text`,
			owner, name, model, version, capabilities, now).Scan(&out.DeviceID); err != nil {
			return err
		}
		if _, err := tx.Exec(r.Context(), `UPDATE device_pairings SET spent_at = $2, device_id = $3
			WHERE code_hash = $1`, hash(code), now, out.DeviceID); err != nil {
			return err
		}
		_, err = tx.Exec(r.Context(), `INSERT INTO device_tokens (token_hash, device_id, created_at, expires_at, hard_cap_at)
			VALUES ($1, $2, $3, $4, $5)`, tokenHash, out.DeviceID, now, time.Time(out.ExpiresAt), time.Time(out.HardCapAt))
		return err
	})
	if err != nil {
		return nil, err
	}
	return out, nil
}

func minTime(a, b time.Time) time.Time {
	if a.Before(b) {
		return a
	}
	return b
}
