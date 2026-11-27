package filamentpresets

import (
	"strings"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/jsonv"
)

// credentialKeys and credentialParts are core/credentials.py's CREDENTIAL_KEYS and
// _CREDENTIAL_PARTS: Bambu Studio's print-host credential keys, and the parts of a key's
// name that make it one too.
var (
	credentialKeys  = map[string]bool{"printhost_apikey": true, "printhost_password": true, "printhost_user": true}
	credentialParts = []string{"apikey", "password", "secret", "token"}
)

// IsCredential is core/credentials.py is_credential: whether key names a print-host
// credential, matched on Python's key.lower().
//
// Python's str.lower() is the full Unicode mapping. For U+0130 (İ) it gives "i" and a
// combining dot above (U+0307, SpecialCasing.txt), the one unconditional lowercase mapping
// longer than one rune. Go's strings.ToLower gives the simple mapping, a plain "i", so
// "PRİNTHOST_APİKEY" would hold "apikey" in Go and not in Python. U+0130 is spelled as
// Python lowers it first; every other rune lowers alike in both.
func IsCredential(key string) bool {
	lowered := strings.ToLower(strings.ReplaceAll(key, "İ", "i̇"))
	if credentialKeys[lowered] {
		return true
	}
	for _, part := range credentialParts {
		if strings.Contains(lowered, part) {
			return true
		}
	}
	return false
}

// StripCredentials is core/credentials.py strip_credentials: doc without its credential
// keys, in nested objects and in lists too, with the order and every other value kept.
func StripCredentials(doc jsonv.Object) jsonv.Object {
	out := jsonv.Object{}
	for _, m := range doc {
		if !IsCredential(m.Key) {
			out = append(out, jsonv.Member{Key: m.Key, Value: stripped(m.Value)})
		}
	}
	return out
}

func stripped(v any) any {
	switch v := v.(type) {
	case jsonv.Object:
		return StripCredentials(v)
	case []any:
		out := make([]any, len(v))
		for i, item := range v {
			out[i] = stripped(item)
		}
		return out
	}
	return v
}
