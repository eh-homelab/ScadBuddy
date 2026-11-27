package fetchgrants

import (
	"fmt"
	"net/netip"
	"regexp"
	"strings"
	"unicode"
	"unicode/utf8"

	"golang.org/x/net/idna"
)

var (
	// ambiguous is asset_fetch.py _AMBIGUOUS: where URL parsers disagree on the host (#959).
	ambiguous = regexp.MustCompile(`[\\\x00-\x1f\x7f]`)
	// urlScheme, with the authority after it, is httpx's URL_REGEX up to its path.
	urlScheme    = regexp.MustCompile(`^(?:([a-zA-Z][a-zA-Z0-9+.-]*)?:)?`)
	ipv4Style    = regexp.MustCompile(`^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$`)
	registration = idna.New(idna.ValidateForRegistration())
)

// unreserved and hostSafe are httpx _urlparse.py's UNRESERVED_CHARACTERS, and the SUB_DELIMS
// with the WHATWG_SAFE that encode_host keeps in an ASCII host.
const (
	unreserved = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
	hostSafe   = "!$&'()*+,;=" + "\"`{}%|\\"
)

// URLHost is ascii_host(parse_fetch_url(pasted)): the host a fetch of pasted is held to (IDNA,
// lower case, no trailing dot), or why the URL is refused. A refusal's words may differ from
// Python's; whether it is refused does not.
func URLHost(pasted string) (host, refusal string) {
	stripped := strings.TrimFunc(pasted, pySpace)
	if ambiguous.MatchString(stripped) {
		return "", fmt.Sprintf("%s holds a backslash or a control character, which parsers read "+
			"different hosts from; give the plain https URL", pyRepr(pasted))
	}
	notURL := fmt.Sprintf("%s is not a URL", pyRepr(pasted))
	scheme := urlScheme.FindStringSubmatch(stripped)
	rest := stripped[len(scheme[0]):]
	var authority string
	hasAuthority := strings.HasPrefix(rest, "//")
	if hasAuthority {
		authority = rest[2:]
		if end := strings.IndexAny(authority, "/?#"); end >= 0 {
			authority = authority[:end]
		}
	}
	userinfo, hostPort := "", authority
	if at := strings.LastIndexByte(authority, '@'); at >= 0 {
		userinfo, hostPort = authority[:at], authority[at+1:]
	}
	var rawHost, port string
	if close := strings.LastIndexByte(hostPort, ']'); strings.HasPrefix(hostPort, "[") && close >= 0 {
		rawHost, port = hostPort[:close+1], hostPort[close+1:]
	} else if colon := strings.IndexByte(hostPort, ':'); colon >= 0 {
		rawHost, port = hostPort[:colon], hostPort[colon:]
	} else {
		rawHost = hostPort
	}
	port = strings.TrimPrefix(port, ":")
	encoded, ok := encodeHost(rawHost)
	if !ok || (port != "" && !pyInt(port)) {
		return "", notURL
	}
	if strings.ToLower(scheme[1]) != "https" {
		return "", fmt.Sprintf("only https URLs can be fetched, and %s is not one", pyRepr(stripped))
	}
	if encoded == "" {
		return "", fmt.Sprintf("%s names no host", pyRepr(pasted))
	}
	if strings.HasPrefix(encoded, "xn--") {
		// httpx's URL.host decodes such a host, and idna's refusal is a ValueError too.
		if _, err := registration.ToUnicode(strings.TrimSuffix(encoded, ".")); err != nil {
			return "", fmt.Sprintf("Invalid IDNA hostname: %s", pyRepr(encoded))
		}
	}
	if userinfo != "" {
		return "", "the URL names a user or password before its host, which is not fetched; " +
			"give the plain https URL"
	}
	return strings.TrimRight(strings.ToLower(encoded), "."), ""
}

// encodeHost is httpx _urlparse.py encode_host: false where it raises InvalidURL.
func encodeHost(host string) (string, bool) {
	switch {
	case host == "":
		return "", true
	case ipv4Style.MatchString(host):
		addr, err := netip.ParseAddr(host)
		return host, err == nil && addr.Is4()
	case strings.HasPrefix(host, "[") && strings.HasSuffix(host, "]") && len(host) >= 2:
		addr, err := netip.ParseAddr(host[1 : len(host)-1])
		return host[1 : len(host)-1], err == nil && addr.Is6()
	case isASCII(host):
		return quote(strings.ToLower(host)), true
	}
	lowered := strings.ToLower(host)
	trimmed := strings.TrimSuffix(lowered, ".")
	encoded, err := registration.ToASCII(trimmed)
	if err != nil || trimmed == "" {
		return "", false
	}
	if trimmed != lowered {
		encoded += "."
	}
	return encoded, true
}

func isASCII(s string) bool {
	for i := 0; i < len(s); i++ {
		if s[i] >= utf8.RuneSelf {
			return false
		}
	}
	return true
}

// quote is httpx _urlparse.py quote with encode_host's safe set: a %XX escape is kept, any
// other character not unreserved or safe is percent-encoded, upper-case.
func quote(s string) string {
	var b strings.Builder
	for i := 0; i < len(s); i++ {
		c := s[i]
		switch {
		case c == '%' && i+2 < len(s) && isHex(s[i+1]) && isHex(s[i+2]):
			b.WriteString(s[i : i+3])
			i += 2
		case strings.IndexByte(unreserved+hostSafe, c) >= 0:
			b.WriteByte(c)
		default:
			fmt.Fprintf(&b, "%%%02X", c)
		}
	}
	return b.String()
}

func isHex(c byte) bool {
	return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F')
}

// pyInt says whether Python's int() reads s: surrounding white space, a sign, decimal digits
// of any script, and single underscores between digits.
func pyInt(s string) bool {
	s = strings.TrimFunc(s, pySpace)
	if s != "" && (s[0] == '+' || s[0] == '-') {
		s = s[1:]
	}
	if s == "" {
		return false
	}
	previous := '_'
	for _, r := range s {
		switch {
		case unicode.Is(unicode.Nd, r):
		case r == '_' && previous != '_':
		default:
			return false
		}
		previous = r
	}
	return previous != '_'
}

// pySpace is str.isspace: Unicode White_Space with the information separators U+001C..U+001F.
func pySpace(r rune) bool { return unicode.IsSpace(r) || (r >= 0x1c && r <= 0x1f) }

// pyRepr is repr of a str, for the refusals' words.
func pyRepr(s string) string {
	quote := "'"
	if strings.Contains(s, "'") && !strings.Contains(s, `"`) {
		quote = `"`
	}
	var b strings.Builder
	b.WriteString(quote)
	for _, r := range s {
		switch {
		case r == '\\':
			b.WriteString(`\\`)
		case string(r) == quote:
			b.WriteString(`\` + quote)
		case r == '\n':
			b.WriteString(`\n`)
		case r == '\r':
			b.WriteString(`\r`)
		case r == '\t':
			b.WriteString(`\t`)
		case r < 0x20 || r == 0x7f:
			fmt.Fprintf(&b, `\x%02x`, r)
		default:
			b.WriteRune(r)
		}
	}
	b.WriteString(quote)
	return b.String()
}
