package claudecreds

import (
	"bytes"
	"encoding/json"
	"regexp"
	"time"
	"unicode/utf8"
)

// Kind is what a transcript event means for the login in use.
type Kind string

const (
	// KindRateLimit: the login is usable again at Event.Reset.
	KindRateLimit Kind = "rate_limit"
	// KindPermanent: the login itself is refused (revoked, no permission, no credit).
	KindPermanent Kind = "permanent"
	// KindTransient: the endpoint or the network failed; the login is fine.
	KindTransient Kind = "transient"
)

// ResetSource is where a rate limit's reset came from.
type ResetSource string

const (
	// ResetEpoch: apiErrorParams.rate_limit_info.resetsAt, else quotaLimits.resetsAt.
	ResetEpoch ResetSource = "epoch"
	// ResetText: ResetFromText on the message text.
	ResetText ResetSource = "text"
	// ResetDefault: no reset; the caller keeps its default cooldown.
	ResetDefault ResetSource = "default"
)

// TextLimit is the most bytes of the message text an Event keeps.
const TextLimit = 500

// Event is a credential event that one transcript entry records. It keeps only these
// fields of the entry.
type Event struct {
	Kind Kind
	// Category is the entry's raw `error` value, such as rate_limit or billing_error.
	Category string
	// APIError is the entry's `apiError`, such as usage_limit_reached, or empty.
	APIError string
	// Reset and ResetSource are set for KindRateLimit only. Reset is zero when
	// ResetSource is ResetDefault.
	Reset       time.Time
	ResetSource ResetSource
	// LimitType is quotaLimits.rateLimitType, such as five_hour, or empty.
	LimitType string
	UUID      string
	SessionID string
	// Timestamp is zero when the entry has none that parses.
	Timestamp time.Time
	// Text is the message text, cut to TextLimit bytes, for the operator.
	Text string
}

// permanentCategories are credentialErrors.ts classifyFailure's PERMANENT_CATEGORIES.
var permanentCategories = map[string]bool{
	"authentication_failed":  true,
	"oauth_org_not_allowed":  true,
	"account_on_hold":        true,
	"verification_required":  true,
	"billing_error":          true,
	"cloud_credential_error": true,
}

// billingMessage is classifyFailure's BILLING_MESSAGE: a request error that is about the
// account, not the request.
var billingMessage = regexp.MustCompile(`(?i)credit balance|billing|payment required|insufficient (?:funds|credit|quota)|quota exceeded`)

// classify maps an `error` category to a Kind as classifyFailure does. Its "other"
// (the request itself failed) is transient here: the login is fine.
func classify(category, text string) Kind {
	switch {
	case permanentCategories[category]:
		return KindPermanent
	case category == "rate_limit":
		return KindRateLimit
	case category == "invalid_request" || category == "model_not_found" || category == "max_output_tokens":
		if billingMessage.MatchString(text) {
			return KindPermanent
		}
	}
	return KindTransient
}

var apiErrorMarker = []byte(`"isApiErrorMessage":true`)

// entry is the part of a transcript line that ParseEntry reads. encoding/json drops
// every other field.
type entry struct {
	IsAPIErrorMessage bool   `json:"isApiErrorMessage"`
	Error             string `json:"error"`
	APIError          string `json:"apiError"`
	APIErrorParams    struct {
		RateLimitInfo struct {
			ResetsAt *float64 `json:"resetsAt"`
		} `json:"rate_limit_info"`
	} `json:"apiErrorParams"`
	QuotaLimits struct {
		ResetsAt      *float64 `json:"resetsAt"`
		RateLimitType string   `json:"rateLimitType"`
	} `json:"quotaLimits"`
	Message struct {
		Content json.RawMessage `json:"content"`
	} `json:"message"`
	UUID      string `json:"uuid"`
	SessionID string `json:"sessionId"`
	Timestamp string `json:"timestamp"`
}

// ParseEntry reads one transcript JSONL line. The line is an event when its
// isApiErrorMessage is true and its error is set. Every other line returns false, and a
// line without the text `"isApiErrorMessage":true` returns false before any JSON parse.
func ParseEntry(line []byte) (Event, bool) {
	if !bytes.Contains(line, apiErrorMarker) {
		return Event{}, false
	}
	var e entry
	if err := json.Unmarshal(line, &e); err != nil || !e.IsAPIErrorMessage || e.Error == "" {
		return Event{}, false
	}
	text := contentText(e.Message.Content)
	ev := Event{
		Kind:      classify(e.Error, text),
		Category:  e.Error,
		APIError:  e.APIError,
		LimitType: e.QuotaLimits.RateLimitType,
		UUID:      e.UUID,
		SessionID: e.SessionID,
		Text:      truncateUTF8(text, TextLimit),
	}
	if ts, err := time.Parse(time.RFC3339Nano, e.Timestamp); err == nil {
		ev.Timestamp = ts.UTC()
	}
	if ev.Kind == KindRateLimit {
		ev.Reset, ev.ResetSource = reset(e, text, ev.Timestamp)
	}
	return ev, true
}

// reset is the rate limit's reset: the epoch in apiErrorParams, else the one in
// quotaLimits, else the text read at the entry's own time, else none.
func reset(e entry, text string, at time.Time) (time.Time, ResetSource) {
	for _, s := range []*float64{e.APIErrorParams.RateLimitInfo.ResetsAt, e.QuotaLimits.ResetsAt} {
		if s != nil && *s > 0 {
			return time.Unix(int64(*s), 0).UTC(), ResetEpoch
		}
	}
	if !at.IsZero() {
		if t, ok := ResetFromText(text, at); ok {
			return t, ResetText
		}
	}
	return time.Time{}, ResetDefault
}

// contentText is message.content as text: a string, or the first text block of an array.
func contentText(raw json.RawMessage) string {
	var s string
	if json.Unmarshal(raw, &s) == nil {
		return s
	}
	var blocks []struct {
		Text *string `json:"text"`
	}
	if json.Unmarshal(raw, &blocks) == nil {
		for _, b := range blocks {
			if b.Text != nil {
				return *b.Text
			}
		}
	}
	return ""
}

// truncateUTF8 cuts s to at most n bytes, at a rune boundary.
func truncateUTF8(s string, n int) string {
	if len(s) <= n {
		return s
	}
	for n > 0 && !utf8.RuneStart(s[n]) {
		n--
	}
	return s[:n]
}
