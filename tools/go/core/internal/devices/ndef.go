package devices

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"unicode/utf8"

	"golang.org/x/net/idna"

	"github.com/eh-homelab/ScadBuddy/tools/go/core/internal/httpx"
)

// The records nfc.write takes are the app's wire records (app/ScadBuddy/Platforms/Android/
// NdefWire.cs `Build`, Web NFC's record shapes), a strict subset of them:
//
//	{"recordType": "text", "text", "lang"?, "encoding"?}   lang a language tag; encoding utf-8 or utf-16
//	{"recordType": "url", "text"}                           an absolute URI
//	{"recordType": "absolute-url", "text"}                  an absolute URI
//	{"recordType": "mime", "mediaType", "text" | "hex"}     exactly one of text and hex
//	{"recordType": "<domain>:<type>", "text" | "hex"}       an external type; exactly one of text and hex
//	{"recordType": "empty"}
//
// Any other key, type or shape is refused. Core parses the records, sends the device the
// parsed form (normalised: the keys above only, in that order), and builds the approval's
// summary from that same form, so what a person approves is what the device writes.

const (
	recordsMax = 16
	// pathShown is how much of a URI's path, query and fragment an approval shows.
	pathShown = 80
	// summaryMax is the most an approval shows; a write that does not fit is refused.
	summaryMax = 2000
)

var (
	langPattern     = regexp.MustCompile(`^[A-Za-z]{2,8}(-[A-Za-z0-9]{1,8}){0,4}$`)
	mediaPattern    = regexp.MustCompile(`^[a-z0-9][a-z0-9!#$&^_.+-]{0,63}/[a-z0-9][a-z0-9!#$&^_.+-]{0,63}$`)
	externalPattern = regexp.MustCompile(`^[a-z0-9][a-z0-9.-]{0,62}:[A-Za-z0-9_.-]{1,63}$`)
	hexPattern      = regexp.MustCompile(`^(?:[0-9a-f]{2})*$`)
)

// record is one parsed NDEF record, in the order its JSON is written.
type record struct {
	RecordType string  `json:"recordType"`
	MediaType  *string `json:"mediaType,omitempty"`
	Lang       *string `json:"lang,omitempty"`
	Encoding   *string `json:"encoding,omitempty"`
	Text       *string `json:"text,omitempty"`
	Hex        *string `json:"hex,omitempty"`
}

// allowed are each record type's keys besides recordType: required, then optional.
func allowedKeys(t string) (required, optional []string, ok bool) {
	switch {
	case t == "text":
		return []string{"text"}, []string{"lang", "encoding"}, true
	case t == "url", t == "absolute-url":
		return []string{"text"}, nil, true
	case t == "mime":
		return []string{"mediaType"}, nil, true
	case t == "empty":
		return nil, nil, true
	case externalPattern.MatchString(t):
		return nil, nil, true
	}
	return nil, nil, false
}

// argsTooLarge is the 422 of args that are too large: over argsMax (checkArgs), or too large
// for the call's device.call_requested event to fit bus.PayloadMax (bus.Fits in call).
func argsTooLarge() error {
	return httpx.WithReason(http.StatusUnprocessableEntity, "The call's args are too large.", "args_too_large")
}

func argsIssue(msg string, at ...string) error {
	return httpx.Invalid(httpx.Issue{Loc: append([]string{"body", "args"}, at...), Msg: msg})
}

// checkArgs is a method's args, parsed and normalised, or the 422 of args it does not take.
// nfc.write takes {records} only, and no raw block, sector or key: core never asks a device
// to write a MIFARE sector, so it never writes a Bambu tag (the app refuses one too). The
// other methods take {}.
func checkArgs(method string, args json.RawMessage) (json.RawMessage, error) {
	if len(args) > argsMax {
		return nil, argsTooLarge()
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(args, &fields); err != nil || fields == nil {
		return nil, argsIssue("Input should be a valid dictionary")
	}
	for k := range fields {
		if method != "nfc.write" || k != "records" {
			return nil, argsIssue("Extra inputs are not permitted", k)
		}
	}
	if method != "nfc.write" {
		return json.RawMessage(`{}`), nil
	}
	var raw []map[string]json.RawMessage
	if err := json.Unmarshal(fields["records"], &raw); err != nil || len(raw) < 1 || len(raw) > recordsMax {
		return nil, argsIssue(fmt.Sprintf("a list of 1 to %d NDEF records", recordsMax), "records")
	}
	out := make([]record, len(raw))
	for i, r := range raw {
		at := []string{"records", strconv.Itoa(i)}
		rec, err := parseRecord(r)
		if err != "" {
			return nil, argsIssue(err, at...)
		}
		out[i] = rec
	}
	normal, err := json.Marshal(map[string]any{"records": out})
	if err != nil {
		return nil, err
	}
	return normal, nil
}

// parseRecord is one wire record, or why it is refused.
func parseRecord(r map[string]json.RawMessage) (record, string) {
	var rec record
	if json.Unmarshal(r["recordType"], &rec.RecordType) != nil || rec.RecordType == "" {
		return rec, "a record needs a recordType string"
	}
	required, optional, ok := allowedKeys(rec.RecordType)
	if !ok {
		return rec, fmt.Sprintf("record type %q is not one the device writes", rec.RecordType)
	}
	data := rec.RecordType == "mime" || externalPattern.MatchString(rec.RecordType)
	for k := range r {
		known := k == "recordType" || contains(required, k) || contains(optional, k) || data && (k == "text" || k == "hex")
		if !known {
			return rec, fmt.Sprintf("a %s record takes no key %q", rec.RecordType, k)
		}
	}
	str := func(k string) (*string, string) {
		raw, given := r[k]
		if !given {
			return nil, ""
		}
		var s string
		if json.Unmarshal(raw, &s) != nil {
			return nil, k + " must be a string"
		}
		return &s, ""
	}
	var bad string
	for _, p := range []struct {
		key  string
		into **string
	}{{"text", &rec.Text}, {"hex", &rec.Hex}, {"lang", &rec.Lang}, {"encoding", &rec.Encoding}, {"mediaType", &rec.MediaType}} {
		if *p.into, bad = str(p.key); bad != "" {
			return rec, bad
		}
	}
	for _, k := range required {
		if _, given := r[k]; !given {
			return rec, fmt.Sprintf("a %s record needs %s", rec.RecordType, k)
		}
	}
	if data && (rec.Text == nil) == (rec.Hex == nil) {
		return rec, fmt.Sprintf("a %s record holds exactly one of text and hex", rec.RecordType)
	}
	switch {
	case rec.Hex != nil && !hexPattern.MatchString(*rec.Hex):
		return rec, "hex is lower-case hex bytes"
	case rec.Text != nil && strings.ContainsRune(*rec.Text, 0):
		return rec, "text holds no NUL"
	case rec.Lang != nil && !langPattern.MatchString(*rec.Lang):
		return rec, "lang is a language tag, such as en or en-US"
	case rec.Encoding != nil && *rec.Encoding != "utf-8" && *rec.Encoding != "utf-16":
		return rec, "encoding is utf-8 or utf-16"
	case rec.MediaType != nil && !mediaPattern.MatchString(*rec.MediaType):
		return rec, "mediaType is a media type, such as text/plain"
	case (rec.RecordType == "url" || rec.RecordType == "absolute-url") && !absoluteURI(*rec.Text):
		return rec, "a URI record holds an absolute URI"
	}
	return rec, ""
}

func contains(items []string, s string) bool {
	for _, item := range items {
		if item == s {
			return true
		}
	}
	return false
}

// schemePattern is a URI scheme as a URI record holds it: lower case.
var schemePattern = regexp.MustCompile(`^[a-z][a-z0-9+.-]*$`)

// lookup turns a host into its ASCII (punycode) form, as the approval shows it.
var lookup = idna.New(idna.MapForLookup(), idna.BidiRule(), idna.ValidateLabels(true))

// absoluteURI says whether s is a URI an approval can show truly: scheme://host[:port] then
// the rest, with a host, no userinfo (scadbuddy.example@evil.example names evil.example) and
// no opaque part (https:evil.example), its authority spelt exactly as parsed and its host one
// that turns into punycode. So the head an approval shows in full is the host it names.
func absoluteURI(s string) bool {
	if !utf8.ValidString(s) || strings.ContainsAny(s, " \t\r\n\\") {
		return false
	}
	u, err := url.Parse(s)
	if err != nil || !schemePattern.MatchString(u.Scheme) || u.Opaque != "" || u.User != nil ||
		u.Hostname() == "" || strings.ContainsAny(u.Host, "%@") {
		return false
	}
	if !strings.HasPrefix(s, u.Scheme+"://"+u.Host) {
		return false
	}
	rest := strings.TrimPrefix(s, u.Scheme+"://"+u.Host)
	if rest != "" && !strings.ContainsRune("/?#", rune(rest[0])) {
		return false
	}
	_, err = lookup.ToASCII(u.Hostname())
	return err == nil
}

// summary is what a person approves for an nfc.write: the device, the record count, and every
// record's type and content in full. Only a URI's path, query and fragment are cut, at
// pathShown, and never its scheme or host. A write whose summary does not fit in summaryMax is
// refused (422 write_too_large_to_approve), never cut. args are checkArgs's normalised form.
func summary(method, deviceName string, args json.RawMessage) (string, error) {
	var parsed struct {
		Records []record `json:"records"`
	}
	if err := json.Unmarshal(args, &parsed); err != nil {
		return "", err
	}
	var b strings.Builder
	n := len(parsed.Records)
	fmt.Fprintf(&b, "%s on %s: %d record", method, strconv.Quote(deviceName), n)
	if n != 1 {
		b.WriteString("s")
	}
	for i, r := range parsed.Records {
		fmt.Fprintf(&b, "; %d. %s", i+1, r.RecordType)
		switch {
		case r.RecordType == "url" || r.RecordType == "absolute-url":
			b.WriteString(" " + shownURI(*r.Text))
		case r.Text != nil:
			b.WriteString(" " + strconv.Quote(*r.Text))
		case r.Hex != nil:
			b.WriteString(" hex " + *r.Hex)
		}
		if r.MediaType != nil {
			b.WriteString(" (" + *r.MediaType + ")")
		}
		if r.Lang != nil {
			b.WriteString(" lang " + *r.Lang)
		}
		if r.Encoding != nil {
			b.WriteString(" " + *r.Encoding)
		}
	}
	out := b.String()
	if utf8.RuneCountInString(out) > summaryMax {
		return "", httpx.WithReason(http.StatusUnprocessableEntity, fmt.Sprintf(
			"An approval shows all of what is written, and this write needs more than %d characters to show. "+
				"Write less, or write it from a page.", summaryMax), "write_too_large_to_approve")
	}
	return out, nil
}

// shownURI is a URI with its scheme and host in full and the rest cut at pathShown runes,
// quoted so a control character shows.
func shownURI(s string) string {
	// absoluteURI took s: scheme://host[:port] exactly, then the rest.
	u, _ := url.Parse(s)
	rest := strings.TrimPrefix(s, u.Scheme+"://"+u.Host)
	host, _ := lookup.ToASCII(u.Hostname())
	if strings.Contains(host, ":") {
		host = "[" + host + "]"
	}
	head := u.Scheme + "://" + host
	if port := u.Port(); port != "" {
		head += ":" + port
	}
	if utf8.RuneCountInString(rest) > pathShown {
		rest = string([]rune(rest)[:pathShown]) + "…"
	}
	return strconv.Quote(head + rest)
}
