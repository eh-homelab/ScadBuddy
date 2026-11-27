package auth

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
)

// SessionCookie is the session cookie's name (auth/gate.py SESSION_COOKIE).
const SessionCookie = "__Host-scadbuddy_session"

// IdleTimeout is how long a session lives unused (auth/models.py IDLE_TIMEOUT).
const IdleTimeout = 14 * 24 * time.Hour

// slideInterval is how old a session's last use may be before its idle deadline moves
// (auth/models.py SLIDE_INTERVAL).
const slideInterval = time.Minute

// failureLogInterval is how often a failed lookup is logged at most (auth/gate.py).
const failureLogInterval = time.Minute

// The bearer prefixes auth/gate.py reads.
const (
	servicePrefix = "sbsvc_"
	tokenPrefix   = "sbpat_"
)

var (
	sessionPattern = regexp.MustCompile(`^[A-Za-z0-9_-]{43}$`)
	patPattern     = regexp.MustCompile(`^sbpat_[0-9a-f]{16}\.[A-Za-z0-9_-]{43}$`)
)

// Kind is what a request's caller is.
type Kind string

// The kinds of caller (auth/models.py Caller).
const (
	KindAnonymous Kind = "anonymous"
	// KindPerson is a person with a live session cookie.
	KindPerson Kind = "person"
	// KindToken is a person's access token, sbpat_.
	KindToken Kind = "token"
	// KindService is a service credential, sbsvc_.
	KindService Kind = "service"
)

// Caller is who a request is from, as auth/gate.py AuthGate resolves it. It holds no
// credential: never a token, a cookie value or a hash.
type Caller struct {
	Kind Kind
	// Principal is the person, for KindPerson and KindToken: "user:<issuer>#<subject>".
	Principal string
	// SessionKind is a person's session kind: "login", "delegated" or "break_glass".
	SessionKind string
	// TokenID is an access token's id, TokenScope its scope: "read" or "write".
	TokenID, TokenScope string
	// Service is the credential's caller and scopes, for KindService.
	Service *Service
}

// Scopes are the caller's scopes: a service credential's, or an access token's one scope.
// A session has none: a person's routes take the person.
func (c Caller) Scopes() []string {
	switch c.Kind {
	case KindService:
		return c.Service.Scopes
	case KindToken:
		return []string{c.TokenScope}
	}
	return nil
}

// Caller is who the request headers name, as auth/gate.py AuthGate._caller decides. A
// bearer sbsvc_ or sbpat_ is looked up and the cookie is not; else the session cookie is.
// A lookup that fails (the database out of reach) is anonymous, and logged at most once a
// minute with the error's type only.
func (r *Resolver) Caller(ctx context.Context, h http.Header) Caller {
	scheme, bearer, _ := strings.Cut(h.Get("Authorization"), " ")
	isBearer := strings.ToLower(scheme) == "bearer"
	switch {
	case isBearer && strings.HasPrefix(bearer, servicePrefix):
		s, err := r.service(ctx, pyStrip(bearer))
		if err != nil || s == nil {
			r.failed(ctx, err)
			return Caller{Kind: KindAnonymous}
		}
		return Caller{Kind: KindService, Service: s}
	case isBearer && strings.HasPrefix(bearer, tokenPrefix):
		c, err := r.personalToken(ctx, pyStrip(bearer))
		if err != nil || c == nil {
			r.failed(ctx, err)
			return Caller{Kind: KindAnonymous}
		}
		return *c
	}
	cookie := h.Get("Cookie")
	if cookie == "" {
		return Caller{Kind: KindAnonymous}
	}
	token := parseCookies(cookie)[SessionCookie]
	if token == "" {
		return Caller{Kind: KindAnonymous}
	}
	c, err := r.session(ctx, token)
	if err != nil || c == nil {
		r.failed(ctx, err)
		return Caller{Kind: KindAnonymous}
	}
	return *c
}

// SessionID is the hex SHA-256 of the session cookie Caller reads from h (Python's
// session.id_hash.hex()), "" when h carries none. A KindPerson caller's session is this one:
// what an auth.session_revoked names. It is an id, never logged.
func SessionID(h http.Header) string {
	token := parseCookies(h.Get("Cookie"))[SessionCookie]
	if token == "" {
		return ""
	}
	sum := sha256.Sum256([]byte(token))
	return hex.EncodeToString(sum[:])
}

func (r *Resolver) now() time.Time {
	if r.Now != nil {
		return r.Now().UTC()
	}
	return time.Now().UTC()
}

// failed logs a failed lookup at most once a minute (auth/gate.py _log_failure): the
// error's type only, as a driver's message can name the connection's host and user.
func (r *Resolver) failed(ctx context.Context, err error) {
	if err == nil {
		return
	}
	r.mu.Lock()
	now := time.Now()
	if !r.loggedAt.IsZero() && now.Sub(r.loggedAt) < failureLogInterval {
		r.unlogged++
		r.mu.Unlock()
		return
	}
	since := r.unlogged
	r.loggedAt, r.unlogged = now, 0
	r.mu.Unlock()
	slog.WarnContext(ctx, "session lookup failed ("+errorType(err)+"); serving the request as anonymous",
		"failures_since_last_logged", since)
}

func errorType(err error) string {
	for {
		next := errors.Unwrap(err)
		if next == nil {
			return fmt.Sprintf("%T", err)
		}
		err = next
	}
}

// session is auth/store.py SessionStore.resolve: the live session token names, or nil. Its
// idle deadline slides when it was last seen a minute ago or more.
func (r *Resolver) session(ctx context.Context, token string) (*Caller, error) {
	if !sessionPattern.MatchString(token) {
		return nil, nil
	}
	hash := sha256.Sum256([]byte(token))
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	now := r.now()
	const live = `revoked_at IS NULL AND idle_expires_at > $2 AND absolute_expires_at > $2`
	c := Caller{Kind: KindPerson}
	var seen time.Time
	err := r.Pool.QueryRow(ctx, `SELECT principal, kind, last_seen_at FROM auth_sessions
		WHERE id_hash = $1 AND `+live, hash[:], now).Scan(&c.Principal, &c.SessionKind, &seen)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	if now.Sub(seen) < slideInterval {
		return &c, nil
	}
	// The update checks liveness again: a revoke between the two statements wins.
	err = r.Pool.QueryRow(ctx, `UPDATE auth_sessions SET last_seen_at = $2,
		idle_expires_at = LEAST($2 + make_interval(secs => $3), absolute_expires_at)
		WHERE id_hash = $1 AND `+live+` RETURNING principal, kind`,
		hash[:], now, IdleTimeout.Seconds()).Scan(&c.Principal, &c.SessionKind)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return &c, nil
}

// personalToken is auth/personal_tokens.py PersonalTokenStore.resolve: the live access
// token, or nil. Its last use is written at most once a minute.
func (r *Resolver) personalToken(ctx context.Context, token string) (*Caller, error) {
	if !patPattern.MatchString(token) {
		return nil, nil
	}
	hash := sha256.Sum256([]byte(token))
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	now := r.now()
	c := Caller{Kind: KindToken}
	var last *time.Time
	err := r.Pool.QueryRow(ctx, `SELECT id, principal, scope, last_used_at FROM personal_access_tokens
		WHERE token_hash = $1 AND revoked_at IS NULL AND expires_at > $2`,
		hash[:], now).Scan(&c.TokenID, &c.Principal, &c.TokenScope, &last)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	if last == nil || now.Sub(*last) >= touchInterval {
		if _, err := r.Pool.Exec(ctx,
			`UPDATE personal_access_tokens SET last_used_at = $1 WHERE id = $2`, now, c.TokenID); err != nil {
			return nil, err
		}
	}
	return &c, nil
}

// service is the live service credential token names, or nil.
func (r *Resolver) service(ctx context.Context, token string) (*Service, error) {
	if !tokenPattern.MatchString(token) {
		return nil, nil
	}
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	return r.lookup(ctx, sha256.Sum256([]byte(token)), r.now())
}

// pyIsSpace is Python's str.isspace over a Latin-1 character, as Starlette decodes a header.
func pyIsSpace(b byte) bool {
	switch b {
	case ' ', '\t', '\n', '\v', '\f', '\r', 0x1c, 0x1d, 0x1e, 0x1f, 0x85, 0xa0:
		return true
	}
	return false
}

// pyStrip is Python's str.strip() over a header value.
func pyStrip(s string) string {
	for len(s) > 0 && pyIsSpace(s[0]) {
		s = s[1:]
	}
	for len(s) > 0 && pyIsSpace(s[len(s)-1]) {
		s = s[:len(s)-1]
	}
	return s
}

// parseCookies is Starlette's cookie_parser: chunks split on ";", each name and value
// stripped, a quoted value unquoted as http.cookies._unquote does, and the last of two
// cookies of one name kept.
func parseCookies(header string) map[string]string {
	out := map[string]string{}
	for chunk := range strings.SplitSeq(header, ";") {
		key, val, found := strings.Cut(chunk, "=")
		if !found {
			key, val = "", chunk
		}
		key, val = pyStrip(key), pyStrip(val)
		if key != "" || val != "" {
			out[key] = unquote(val)
		}
	}
	return out
}

// unquote is Python's http.cookies._unquote: a value in double quotes loses them, and in
// it \ooo (octal, first digit 0-3) is that byte and \c is c.
func unquote(s string) string {
	if len(s) < 2 || s[0] != '"' || s[len(s)-1] != '"' {
		return s
	}
	s = s[1 : len(s)-1]
	var b strings.Builder
	for i := 0; i < len(s); i++ {
		if s[i] != '\\' || i+1 >= len(s) {
			b.WriteByte(s[i])
			continue
		}
		if i+4 <= len(s) && isOctal(s[i+1:i+4]) {
			n, _ := strconv.ParseUint(s[i+1:i+4], 8, 8)
			b.WriteRune(rune(n))
			i += 3
			continue
		}
		if s[i+1] == '\n' {
			// Python's "." does not match a newline: the backslash stays.
			b.WriteByte(s[i])
			continue
		}
		b.WriteByte(s[i+1])
		i++
	}
	return b.String()
}

func isOctal(s string) bool {
	return len(s) == 3 && s[0] >= '0' && s[0] <= '3' &&
		slices.IndexFunc([]byte(s[1:]), func(c byte) bool { return c < '0' || c > '7' }) < 0
}
