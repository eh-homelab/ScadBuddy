// Package front is what the Go front does for every request it forwards to Python: the
// client address (backend/scadbuddy/core/proxies.py) and the CRUD port's path filter
// (backend/scadbuddy/crud/listener.py).
package front

import (
	"fmt"
	"net"
	"net/http"
	"net/netip"
	"path"
	"strconv"
	"strings"
)

// Networks are the proxies whose X-Forwarded-For is believed.
type Networks []netip.Prefix

// ParseNetworks reads "10.0.0.0/8, fd00::/8, 192.168.1.10" as proxies.py parse_cidr_list.
func ParseNetworks(raw string) (Networks, error) {
	var out Networks
	for entry := range strings.SplitSeq(raw, ",") {
		entry = strings.TrimSpace(entry)
		if entry == "" {
			continue
		}
		prefix, ok := network(entry)
		if !ok {
			return nil, fmt.Errorf("%q is not an IP address or CIDR range", entry)
		}
		out = append(out, prefix)
	}
	return out, nil
}

func network(entry string) (netip.Prefix, bool) {
	address, bits, slash := strings.Cut(entry, "/")
	ip, err := netip.ParseAddr(address)
	if err != nil || ip.Zone() != "" {
		return netip.Prefix{}, false
	}
	n := ip.BitLen()
	if slash {
		v, err := strconv.Atoi(bits)
		if err != nil || bits == "" || strings.ContainsAny(bits, "+-") || v > ip.BitLen() {
			return netip.Prefix{}, false
		}
		n = v
	}
	if ip.Is4In6() {
		// Peers are unwrapped before matching, so a mapped entry names an IPv4 network.
		if n < 96 {
			return netip.Prefix{}, false
		}
		ip, n = ip.Unmap(), n-96
	}
	return netip.PrefixFrom(ip, n).Masked(), true
}

func (ns Networks) contains(address string) bool {
	ip, err := netip.ParseAddr(address)
	if err != nil {
		return false
	}
	ip = ip.Unmap()
	for _, n := range ns {
		if n.Contains(ip) {
			return true
		}
	}
	return false
}

// Client is the request's client: the last X-Forwarded-For value when the peer is a trusted
// proxy and the value is an IP address, else the peer (proxies.py client_address).
func (ns Networks) Client(r *http.Request) string {
	peer, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil {
		peer = r.RemoteAddr
	}
	if ns.contains(peer) {
		values := r.Header.Values("X-Forwarded-For")
		if len(values) > 0 {
			parts := strings.Split(values[len(values)-1], ",")
			last := strings.TrimSpace(parts[len(parts)-1])
			if ip, err := netip.ParseAddr(last); err == nil && last != "" {
				return ip.Unmap().String()
			}
		}
	}
	if ip, err := netip.ParseAddr(peer); err == nil {
		return ip.Unmap().String()
	}
	return peer
}

// CRUDOnly passes only what the CRUD port serves: the CRUD routes, the service-token
// exchange and /healthz, each at a path already normal. Anything else is a plain 404.
func CRUDOnly(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		p := r.URL.Path
		if path.Clean(p) == p && (p == "/api/v1/auth/service-token" || p == "/healthz" ||
			strings.HasPrefix(p, "/api/v1/internal/crud/")) && !isUpgrade(r) {
			next.ServeHTTP(w, r)
			return
		}
		w.Header().Set("Content-Type", "text/plain; charset=utf-8")
		w.WriteHeader(http.StatusNotFound)
		_, _ = w.Write([]byte("Not Found"))
	})
}

func isUpgrade(r *http.Request) bool { return r.Header.Get("Upgrade") != "" }
