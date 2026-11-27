// Package fetchgrants is POST …/agent/fetch-grants of backend/scadbuddy/api/crud_agent_keys.py
// (area keys, with internal/secrets' GET /keys/input-hash): a grant for one fetch off the asset
// allowlist, minted under a key derived from the KEK (library/fetch_grants.py mint), for the
// agent's fetch_asset after its approval. The key never leaves the API, and nothing here logs a
// grant.
//
// The body's host must be the host the asset route reads from its url
// (library/asset_fetch.py parse_fetch_url and ascii_host, over httpx 0.28's URL parser):
// URLHost is that parse, pinned by backend/tests/fixtures/fetch-grant-vectors.json.
package fetchgrants

import (
	"crypto/hkdf"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"fmt"
	"net/http"
	"strconv"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/crud"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/seal"
)

const (
	// keyInfo is fetch_grants.py FETCH_GRANT_KEY_INFO.
	keyInfo = "scadbuddy:assets:fetch-once:v1"
	// ttl is FETCH_GRANT_TTL_SECONDS: the fetch is sent as soon as it is minted.
	ttl     = 600
	purpose = "asset-fetch-once"
	hostMax = 253
	urlMax  = 8192
	bodyMax = 8 * 1024 * 1024
)

// Areas are the package's routes: area keys' POST /fetch-grants (its GET is internal/secrets').
func Areas(_ *pgxpool.Pool, keys *seal.Keys) map[string][]crud.Route {
	g := &Grants{Keys: keys}
	return map[string][]crud.Route{
		"keys": {{Method: "POST", Path: "/fetch-grants", Handle: g.mint, MaxBody: bodyMax}},
	}
}

// Grants mints fetch grants under the current KEK.
type Grants struct{ Keys *seal.Keys }

// Key is fetch_grant_key: HKDF-SHA256 of the KEK with no salt, 32 bytes.
func Key(kek *seal.KEK) []byte {
	key, err := hkdf.Key(sha256.New, kek.Key(), nil, keyInfo, 32)
	if err != nil {
		panic(err)
	}
	return key
}

func b64(data []byte) string { return base64.RawURLEncoding.EncodeToString(data) }

// Mint is the grant: b64url("asset-fetch-once\n<host>\n<sha256 hex of url>\n<expiry>\n<nonce>")
// "." b64url(HMAC-SHA256), unpadded.
func Mint(key []byte, host, url string, expires int64, nonce string) string {
	digest := sha256.Sum256([]byte(url))
	body := []byte(purpose + "\n" + host + "\n" + hex.EncodeToString(digest[:]) + "\n" +
		strconv.FormatInt(expires, 10) + "\n" + nonce)
	mac := hmac.New(sha256.New, key)
	mac.Write(body)
	return b64(body) + "." + b64(mac.Sum(nil))
}

func (g *Grants) mint(r *http.Request, _ crud.Params) (any, error) {
	body, err := crud.ReadBody(r, "host", "url")
	if err != nil {
		return nil, err
	}
	host := body.Get("host").String(1, hostMax, nil)
	url := body.Get("url").String(1, urlMax, nil)
	if err := body.Err(); err != nil {
		return nil, err
	}
	// The model's own check, only once its fields are valid, as pydantic runs it.
	if parsed, refusal := URLHost(url); refusal != "" {
		body.Fail(nil, refusal)
	} else if parsed != host {
		body.Fail(nil, fmt.Sprintf("host %s is not the URL's host %s", pyRepr(host), pyRepr(parsed)))
	}
	if err := body.Err(); err != nil {
		return nil, err
	}
	if g.Keys == nil || g.Keys.Current == nil {
		return nil, httpx.WithReason(http.StatusServiceUnavailable,
			"The API has no key-encryption key (SCADBUDDY_SECRET_KEY_FILE).", "kek-missing")
	}
	nonce := make([]byte, 16)
	_, _ = rand.Read(nonce)
	grant := Mint(Key(g.Keys.Current), host, url, time.Now().Unix()+ttl, hex.EncodeToString(nonce))
	return struct {
		Grant string `json:"grant"`
	}{grant}, nil
}
