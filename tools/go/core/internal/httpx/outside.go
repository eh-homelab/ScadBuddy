package httpx

import "net/http"

// Outside is base for requests that leave ScadBuddy (the blob store's S3 endpoint): it
// drops the W3C trace context headers telemetry's Transport injects, so the request's
// CLIENT span is kept and trace context never leaves ScadBuddy (spec 2026-10-01 §4;
// backend/scadbuddy/store/s3.py `_request` makes no header for the same reason). Wrap it
// in tel.Transport: tel.Transport(Outside(nil), peer).
func Outside(base http.RoundTripper) http.RoundTripper {
	if base == nil {
		base = http.DefaultTransport
	}
	return outside{base}
}

type outside struct{ base http.RoundTripper }

func (o outside) RoundTrip(req *http.Request) (*http.Response, error) {
	if req.Header.Get("traceparent") != "" || req.Header.Get("tracestate") != "" || req.Header.Get("baggage") != "" {
		req = req.Clone(req.Context())
		req.Header.Del("traceparent")
		req.Header.Del("tracestate")
		req.Header.Del("baggage")
	}
	return o.base.RoundTrip(req)
}
