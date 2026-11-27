package claudecreds

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func fixture(t *testing.T, name string) []byte {
	t.Helper()
	b, err := os.ReadFile(filepath.Join("testdata", name))
	if err != nil {
		t.Fatal(err)
	}
	return []byte(strings.TrimRight(string(b), "\n"))
}

const fixtureSession = "11111111-1111-4111-8111-111111111111"

func TestParseEntryFixtures(t *testing.T) {
	cases := []struct {
		file string
		ok   bool
		want Event
	}{
		{"rate_limit_usage_limit.jsonl", true, Event{
			Kind: KindRateLimit, Category: "rate_limit", APIError: "usage_limit_reached",
			Reset: time.Unix(1792884600, 0).UTC(), ResetSource: ResetEpoch, LimitType: "five_hour",
			UUID: "00000000-0000-4000-8000-000000000001", SessionID: fixtureSession,
			Timestamp: time.Date(2026, 10, 24, 21, 59, 11, 419e6, time.UTC),
			Text:      "You've hit your session limit · resets 7:30pm (America/New_York)",
		}},
		{"rate_limit_plain_429.jsonl", true, Event{
			Kind: KindRateLimit, Category: "rate_limit", ResetSource: ResetDefault,
			UUID: "00000000-0000-4000-8000-000000000002", SessionID: fixtureSession,
			Timestamp: time.Date(2026, 9, 24, 7, 33, 20, 410e6, time.UTC),
			Text:      "API Error: Request rejected (429) · This request would exceed your account's rate limit. Please try again later.",
		}},
		{"billing_error.jsonl", true, Event{
			Kind: KindPermanent, Category: "billing_error",
			UUID: "00000000-0000-4000-8000-000000000003", SessionID: fixtureSession,
			Timestamp: time.Date(2026, 10, 26, 20, 54, 31, 262e6, time.UTC),
			Text:      "Credit balance is too low",
		}},
		{"server_error.jsonl", true, Event{
			Kind: KindTransient, Category: "server_error",
			UUID: "00000000-0000-4000-8000-000000000004", SessionID: fixtureSession,
			Timestamp: time.Date(2026, 8, 13, 3, 11, 24, 944e6, time.UTC),
			Text:      "API Error: The response stopped arriving. The response above may be incomplete.",
		}},
		{"assistant.jsonl", false, Event{}},
		{"user.jsonl", false, Event{}},
	}
	for _, c := range cases {
		t.Run(c.file, func(t *testing.T) {
			got, ok := ParseEntry(fixture(t, c.file))
			if ok != c.ok || got != c.want {
				t.Errorf("ParseEntry = %+v, %v; want %+v, %v", got, ok, c.want, c.ok)
			}
		})
	}
}

func TestParseEntry(t *testing.T) {
	const ts = `"timestamp":"2026-11-26T12:00:00Z"`
	textReset := time.Date(2026, 11, 26, 18, 30, 0, 0, time.UTC) // 1:30pm New York (EST)
	cases := []struct {
		name   string
		line   string
		ok     bool
		kind   Kind
		reset  time.Time
		source ResetSource
	}{
		{"apiErrorParams before quotaLimits and text",
			`{"isApiErrorMessage":true,"error":"rate_limit","apiErrorParams":{"rate_limit_info":{"resetsAt":1800000000}},"quotaLimits":{"resetsAt":1700000000},"message":{"content":[{"text":"resets 1:30pm (America/New_York)"}]},` + ts + `}`,
			true, KindRateLimit, time.Unix(1800000000, 0).UTC(), ResetEpoch},
		{"quotaLimits before text",
			`{"isApiErrorMessage":true,"error":"rate_limit","quotaLimits":{"resetsAt":1700000000},"message":{"content":[{"text":"resets 1:30pm (America/New_York)"}]},` + ts + `}`,
			true, KindRateLimit, time.Unix(1700000000, 0).UTC(), ResetEpoch},
		{"text at the entry's time",
			`{"isApiErrorMessage":true,"error":"rate_limit","message":{"content":[{"text":"You've hit your session limit · resets 1:30pm (America/New_York)"}]},` + ts + `}`,
			true, KindRateLimit, textReset, ResetText},
		{"text with no timestamp is the default",
			`{"isApiErrorMessage":true,"error":"rate_limit","message":{"content":[{"text":"resets 1:30pm (America/New_York)"}]}}`,
			true, KindRateLimit, time.Time{}, ResetDefault},
		{"text that names no reset is the default",
			`{"isApiErrorMessage":true,"error":"rate_limit","message":{"content":[{"text":"API Error: 429"}]},` + ts + `}`,
			true, KindRateLimit, time.Time{}, ResetDefault},
		{"string content",
			`{"isApiErrorMessage":true,"error":"rate_limit","message":{"content":"resets 1:30pm (America/New_York)"},` + ts + `}`,
			true, KindRateLimit, textReset, ResetText},
		{"authentication_failed", `{"isApiErrorMessage":true,"error":"authentication_failed"}`, true, KindPermanent, time.Time{}, ""},
		{"oauth_org_not_allowed", `{"isApiErrorMessage":true,"error":"oauth_org_not_allowed"}`, true, KindPermanent, time.Time{}, ""},
		{"account_on_hold", `{"isApiErrorMessage":true,"error":"account_on_hold"}`, true, KindPermanent, time.Time{}, ""},
		{"verification_required", `{"isApiErrorMessage":true,"error":"verification_required"}`, true, KindPermanent, time.Time{}, ""},
		{"cloud_credential_error", `{"isApiErrorMessage":true,"error":"cloud_credential_error"}`, true, KindPermanent, time.Time{}, ""},
		{"invalid_request about credit",
			`{"isApiErrorMessage":true,"error":"invalid_request","message":{"content":[{"text":"Your credit balance is too low"}]}}`,
			true, KindPermanent, time.Time{}, ""},
		{"invalid_request", `{"isApiErrorMessage":true,"error":"invalid_request","message":{"content":[{"text":"prompt is too long"}]}}`, true, KindTransient, time.Time{}, ""},
		{"overloaded", `{"isApiErrorMessage":true,"error":"overloaded"}`, true, KindTransient, time.Time{}, ""},
		{"unknown", `{"isApiErrorMessage":true,"error":"unknown"}`, true, KindTransient, time.Time{}, ""},
		{"no error", `{"isApiErrorMessage":true}`, false, "", time.Time{}, ""},
		{"empty error", `{"isApiErrorMessage":true,"error":""}`, false, "", time.Time{}, ""},
		{"marker only in text", `{"type":"user","message":{"content":"\"isApiErrorMessage\":true"},"error":"rate_limit"}`, false, "", time.Time{}, ""},
		{"spaced marker is not read", `{"isApiErrorMessage": true,"error":"rate_limit"}`, false, "", time.Time{}, ""},
		{"not JSON", `"isApiErrorMessage":true`, false, "", time.Time{}, ""},
		{"empty", ``, false, "", time.Time{}, ""},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got, ok := ParseEntry([]byte(c.line))
			if ok != c.ok || got.Kind != c.kind || !got.Reset.Equal(c.reset) || got.ResetSource != c.source {
				t.Errorf("ParseEntry = %+v, %v; want kind %q reset %v source %q, %v", got, ok, c.kind, c.reset, c.source, c.ok)
			}
		})
	}
}

func TestParseEntryTextCap(t *testing.T) {
	long := strings.Repeat("a", TextLimit-1) + "·" + "tail"
	got, ok := ParseEntry([]byte(`{"isApiErrorMessage":true,"error":"server_error","message":{"content":[{"text":"` + long + `"}]}}`))
	if !ok {
		t.Fatal("not an event")
	}
	if want := strings.Repeat("a", TextLimit-1); got.Text != want {
		t.Errorf("Text has %d bytes, want %d (cut before the split rune)", len(got.Text), len(want))
	}
}
