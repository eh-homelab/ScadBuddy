package plugins

import (
	"fmt"
	"strings"
)

// The AADs of the agent's plugin secrets and sign-ins: agent_data/plugins.py, which is the
// agent's registry.ts pluginAad and oauthStore.ts grantAad and flowAad, byte for byte, so a
// row sealed on any side opens on the others.

// pair is one member of a JSON object written in the order given.
type pair struct {
	key   string
	value *string
}

func some(s string) *string { return &s }

// jsJSON is plugins.py js_json for an object of strings and nulls: json.dumps with no
// spaces, the keys in the order given, non-ASCII text as it is.
func jsJSON(pairs ...pair) string {
	var b strings.Builder
	b.WriteByte('{')
	for i, p := range pairs {
		if i > 0 {
			b.WriteByte(',')
		}
		writeString(&b, p.key)
		b.WriteByte(':')
		if p.value == nil {
			b.WriteString("null")
		} else {
			writeString(&b, *p.value)
		}
	}
	b.WriteByte('}')
	return b.String()
}

// writeString is json.dumps(s, ensure_ascii=False) for a string.
func writeString(b *strings.Builder, s string) {
	b.WriteByte('"')
	for _, r := range s {
		switch r {
		case '"':
			b.WriteString(`\"`)
		case '\\':
			b.WriteString(`\\`)
		case '\n':
			b.WriteString(`\n`)
		case '\r':
			b.WriteString(`\r`)
		case '\t':
			b.WriteString(`\t`)
		case '\b':
			b.WriteString(`\b`)
		case '\f':
			b.WriteString(`\f`)
		default:
			if r < 0x20 {
				fmt.Fprintf(b, `\u%04x`, r)
			} else {
				b.WriteRune(r)
			}
		}
	}
	b.WriteByte('"')
}

func pluginAAD(name, url, header string) string {
	return "ai_plugins:" + jsJSON(pair{"name", some(name)}, pair{"url", some(url)},
		pair{"auth_header", some(header)})
}

// grantKey is what a sign-in's AAD binds.
type grantKey struct {
	Plugin, PluginURL, Resource, Issuer, TokenEndpoint, ClientID string
}

func grantAAD(g grantKey) string {
	return "ai_plugin_oauth:" + jsJSON(
		pair{"plugin", some(g.Plugin)}, pair{"plugin_url", some(g.PluginURL)},
		pair{"resource", some(g.Resource)}, pair{"issuer", some(g.Issuer)},
		pair{"token_endpoint", some(g.TokenEndpoint)}, pair{"client_id", some(g.ClientID)})
}

// flowKey is what a sign-in under way's AAD binds from its meta.
type flowKey struct {
	Issuer        string `json:"issuer"`
	TokenEndpoint string `json:"token_endpoint"`
	ClientID      string `json:"client_id"`
	RedirectURI   string `json:"redirect_uri"`
}

func flowAAD(stateHash, plugin, pluginURL string, meta flowKey) string {
	return "ai_plugin_oauth_flows:" + jsJSON(
		pair{"state_hash", some(stateHash)}, pair{"plugin", some(plugin)},
		pair{"plugin_url", some(pluginURL)}, pair{"issuer", some(meta.Issuer)},
		pair{"token_endpoint", some(meta.TokenEndpoint)}, pair{"client_id", some(meta.ClientID)},
		pair{"redirect_uri", some(meta.RedirectURI)})
}

// flowSecret is oauthStore.ts's {state, verifier, client_secret?}.
type flowSecret struct {
	State        string  `json:"state"`
	Verifier     string  `json:"verifier"`
	ClientSecret *string `json:"client_secret"`
}

// text is the plaintext the agent seals (_flow_text), in its key order.
func (s flowSecret) text() string {
	pairs := []pair{{"state", some(s.State)}, {"verifier", some(s.Verifier)}}
	if s.ClientSecret != nil && *s.ClientSecret != "" {
		pairs = append(pairs, pair{"client_secret", s.ClientSecret})
	}
	return jsJSON(pairs...)
}

// grantSecret is oauthStore.ts GrantSecret.
type grantSecret struct {
	AccessToken  string  `json:"access_token"`
	RefreshToken *string `json:"refresh_token"`
	ClientSecret *string `json:"client_secret"`
}

// text is the plaintext the agent seals (sealGrant, _grant_text), in its key order.
func (s grantSecret) text() string {
	pairs := []pair{{"access_token", some(s.AccessToken)}}
	if s.RefreshToken != nil && *s.RefreshToken != "" {
		pairs = append(pairs, pair{"refresh_token", s.RefreshToken})
	}
	if s.ClientSecret != nil && *s.ClientSecret != "" {
		pairs = append(pairs, pair{"client_secret", s.ClientSecret})
	}
	return jsJSON(pairs...)
}
